import base64
import hashlib
import json
import secrets
import stat
import urllib.parse

import conftest
import httpx
import pytest

import revtrend.cli as cli

URL = "http://testserver"


class AuthServer:
    """Authorization and token endpoints of the fake Entra issuer."""

    def __init__(self, issuer):
        self.issuer = issuer
        self.roles = ["writer", "reader"]
        self.error = None
        self.challenges = {}
        self.refresh_tokens = set()
        self.token_requests = []

    def authorize(self, url):
        """What the browser and the user do with the login URL."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if self.error:
            return {"state": query["state"], "error": self.error}
        code = secrets.token_urlsafe(16)
        self.challenges[code] = query["code_challenge"]
        return {"state": query["state"], "code": code}

    def handle(self, request):
        if str(request.url) != f"{self.issuer.url}/token":
            return self.issuer.handle(request) or httpx.Response(404)
        form = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.token_requests.append(form)
        if form["grant_type"] == "authorization_code":
            challenge = self.challenges.pop(form["code"], None)
            if challenge != s256(form["code_verifier"]):
                return httpx.Response(400, json={"error": "invalid_grant"})
        elif form.get("refresh_token") not in self.refresh_tokens:
            return httpx.Response(
                400,
                json={"error": "invalid_grant", "error_description": "token expired"},
            )
        self.refresh_tokens.discard(form.get("refresh_token"))
        refresh_token = secrets.token_urlsafe(16)
        self.refresh_tokens.add(refresh_token)
        access_token = self.issuer.token("client-id", subject="alice", roles=self.roles)
        return httpx.Response(
            200,
            json={
                "access_token": access_token,
                "expires_in": 3600,
                "refresh_token": refresh_token,
            },
        )


def s256(verifier):
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


@pytest.fixture
def auth_server(entra):
    return AuthServer(entra)


@pytest.fixture
def run(client, auth_server, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("REVTREND_TOKEN", raising=False)

    def browser(url):
        client.get("/auth/cli/callback", params=auth_server.authorize(url))

    monkeypatch.setattr(cli, "show_login_url", browser)

    def run(*argv):
        http = httpx.Client(
            mounts={
                URL: client._transport,
                "https://login.example": httpx.MockTransport(auth_server.handle),
            }
        )
        cli.main(["--url", URL, *argv], http=http)

    return run


def sessions(tmp_path):
    return json.loads((tmp_path / "cache/revtrend/sessions.json").read_text())


def test_login(run, auth_server, tmp_path):
    run("login")
    path = tmp_path / "cache/revtrend/sessions.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    session = sessions(tmp_path)[URL]
    assert session["refresh_token"] in auth_server.refresh_tokens
    assert session["token_endpoint"] == f"{auth_server.issuer.url}/token"
    (request,) = auth_server.token_requests
    assert request["client_id"] == "client-id"
    assert request["redirect_uri"] == "https://revtrend.example/auth/cli/callback"
    assert request["scope"] == "api://revtrend/Metrics.Write offline_access"


def test_login_denied(run, auth_server, capsys):
    auth_server.error = "access_denied"
    with pytest.raises(SystemExit) as e:
        run("login")
    assert e.value.code == 1
    assert "login failed: access_denied" in capsys.readouterr().err


def test_url_required(monkeypatch, capsys):
    monkeypatch.delenv("REVTREND_URL", raising=False)
    with pytest.raises(SystemExit):
        cli.main(["login"])
    assert "REVTREND_URL" in capsys.readouterr().err


def series(client, metric="image.size_bytes"):
    response = client.get(
        "/api/v1/projects/test/series",
        params={"metric": metric},
        headers=client.as_user("reader"),
    )
    return response.json()


METRICS = ["--label", "machine:amd64", "image.size_bytes=42"]


def test_push(run, client, graph, capsys):
    run("login")
    run(
        "push",
        "--project",
        "test",
        "--sha",
        graph["c3"],
        "--run-id",
        "r1",
        *METRICS,
    )
    assert (
        capsys.readouterr().out
        == f"{graph['c3'][:8]} r1: 1 created, 0 already present\n"
    )
    run(
        "push",
        "--project",
        "test",
        "--sha",
        graph["c3"],
        "--run-id",
        "r1",
        *METRICS,
    )
    assert "0 created, 1 already present" in capsys.readouterr().out
    (row,) = series(client)
    assert row["sha"] == graph["c3"]
    assert row["value"] == 42
    assert row["source"] == "manual"


def test_push_resolves_rev(run, client, remote, graph, monkeypatch):
    run("login")
    monkeypatch.chdir(remote.path)
    run("push", "--project", "test", "--sha", "master~1", *METRICS)
    (row,) = series(client)
    assert row["sha"] == graph["m1"]


def test_push_default_run_id(run, graph, capsys):
    run("login")
    run("push", "--project", "test", "--sha", graph["c3"], *METRICS)
    assert " manual-" in capsys.readouterr().out


def test_push_unknown_rev(run, remote, monkeypatch, capsys):
    run("login")
    monkeypatch.chdir(remote.path)
    with pytest.raises(SystemExit):
        run("push", "--project", "test", "--sha", "nope", *METRICS)
    assert "cannot resolve 'nope'" in capsys.readouterr().err


def test_push_server_error(run, capsys):
    run("login")
    with pytest.raises(SystemExit):
        run("push", "--project", "test", "--sha", "0" * 40, *METRICS)
    assert "(409)" in capsys.readouterr().err


def test_push_requires_login(run, graph, capsys):
    with pytest.raises(SystemExit):
        run("push", "--project", "test", "--sha", graph["c3"], *METRICS)
    assert "not logged in" in capsys.readouterr().err


def test_push_token_from_env(run, client, graph, monkeypatch):
    token = client.as_user("writer")["Authorization"].removeprefix("Bearer ")
    monkeypatch.setenv("REVTREND_TOKEN", token)
    run("push", "--project", "test", "--sha", graph["c3"], *METRICS)
    assert len(series(client)) == 1


def expire_session(tmp_path):
    path = tmp_path / "cache/revtrend/sessions.json"
    data = sessions(tmp_path)
    data[URL]["expires_at"] = 0
    path.write_text(json.dumps(data))


def test_refresh(run, client, auth_server, graph, tmp_path):
    run("login")
    old = sessions(tmp_path)[URL]["refresh_token"]
    expire_session(tmp_path)
    run("push", "--project", "test", "--sha", graph["c3"], *METRICS)
    assert auth_server.token_requests[-1]["grant_type"] == "refresh_token"
    session = sessions(tmp_path)[URL]
    assert session["refresh_token"] not in (None, old)
    assert session["expires_at"] > 0
    assert len(series(client)) == 1


def test_refresh_rejected(run, auth_server, graph, tmp_path, capsys):
    run("login")
    auth_server.refresh_tokens.clear()
    expire_session(tmp_path)
    with pytest.raises(SystemExit):
        run("push", "--project", "test", "--sha", graph["c3"], *METRICS)
    assert "session expired, run 'revtrend login'" in capsys.readouterr().err


def test_push_labels(run, client, graph):
    run("login")
    run(
        "push",
        "--project",
        "test",
        "--sha",
        graph["c3"],
        "--label",
        "machine:amd64",
        "--label",
        "image:core-image",
        "image.size_bytes=42",
        "image.files=1.5",
    )
    (row,) = series(client)
    assert row["labels"] == {"machine": "amd64", "image": "core-image"}
    (row,) = series(client, metric="image.files")
    assert row["value"] == 1.5


@pytest.mark.parametrize(
    "metric", ["image.size_bytes", "image.size_bytes=", "=1", "x=abc", "x=nan", "x=inf"]
)
def test_push_invalid_metric(run, graph, metric, capsys):
    with pytest.raises(SystemExit):
        run("push", "--project", "test", "--sha", graph["c3"], metric)
    assert "expected NAME=VALUE" in capsys.readouterr().err


def test_push_invalid_label(run, graph, capsys):
    with pytest.raises(SystemExit):
        run(
            "push",
            "--project",
            "test",
            "--sha",
            graph["c3"],
            "--label",
            "machine",
            "x=1",
        )
    assert "expected KEY:VALUE" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (["x=1", "x=2"], "duplicate metric: x"),
        (["--label", "a:1", "--label", "a:2", "x=1"], "duplicate label: a"),
    ],
)
def test_push_duplicates(run, graph, args, error, capsys):
    run("login")
    with pytest.raises(SystemExit):
        run("push", "--project", "test", "--sha", graph["c3"], *args)
    assert error in capsys.readouterr().err


def test_compare(run, client, graph, capsys):
    conftest.push(client, graph["c2"], 40)
    conftest.push(client, graph["x2"], 50)
    conftest.push(client, graph["x2"], 7.25, metric="test.duration_seconds")
    run("login")
    capsys.readouterr()
    run("compare", "--project", "test", "--head", graph["x2"])
    assert capsys.readouterr().out == (
        f"{graph['x2'][:8]} vs. master (merge base {graph['c3'][:8]}, tip {graph['c3'][:8]})\n"
        "\n"
        "series                                   baseline  head  delta  delta %\n"
        "image.size_bytes{machine=amd64}       40 (2 back)    50    +10      +25\n"
        "test.duration_seconds{machine=amd64}            -  7.25      -        -\n"
    )


def test_compare_json(run, client, remote, graph, monkeypatch, capsys):
    conftest.push(client, graph["c3"], 40)
    conftest.push(client, graph["x2"], 50)
    conftest.push(client, graph["x2"], 1, metric="other")
    run("login")
    capsys.readouterr()
    monkeypatch.chdir(remote.path)
    remote.git("checkout", "--quiet", "feature/x")
    run(
        "compare",
        "--project",
        "test",
        "--metric",
        "image.size_bytes",
        "--label",
        "machine:amd64",
        "--json",
    )
    result = json.loads(capsys.readouterr().out)
    assert result["head"]["sha"] == graph["x2"]
    (row,) = result["results"]
    assert row["delta"] == 10
