import base64
import hashlib
import json
import secrets
import stat
import urllib.parse

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
