import functools
import os
import subprocess
import time

import cryptography.hazmat.primitives.asymmetric.rsa
import fastapi.testclient
import httpx
import jwt
import pytest

import revtrend.config
import revtrend.server

# Makes asyncio report slow callbacks, e.g. blocking calls in async code.
os.environ.setdefault("PYTHONASYNCIODEBUG", "1")


class Remote:
    """Builds a git repository with deterministic commit times."""

    def __init__(self, path):
        self.path = path
        self.time = 1_700_000_000
        self.git("init", "--quiet", "--initial-branch=master")

    @property
    def url(self):
        return f"file://{self.path}"

    def git(self, *args):
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
            "GIT_AUTHOR_DATE": f"{self.time} +0000",
            "GIT_COMMITTER_DATE": f"{self.time} +0000",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        result = subprocess.run(
            ["git", "-C", str(self.path), *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip()

    def commit(self, subject):
        self.time += 60
        self.git("commit", "--quiet", "--allow-empty", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def merge(self, branch, subject):
        self.time += 60
        self.git("merge", "--quiet", "--no-ff", "-m", subject, branch)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def remote(tmp_path):
    path = tmp_path / "remote"
    path.mkdir()
    return Remote(path)


class TokenIssuer:
    """An OIDC issuer serving discovery and JWKS through an httpx mock transport."""

    def __init__(self, url, kid="key-1", key_name="default"):
        self.url = url
        self.kid = kid
        self.private_key = rsa_key(key_name)
        self.requests = []

    def jwks(self):
        jwk = jwt.algorithms.RSAAlgorithm.to_jwk(
            self.private_key.public_key(), as_dict=True
        )
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig"}]}

    def handle(self, request):
        self.requests.append(str(request.url))
        if str(request.url) == f"{self.url}/.well-known/openid-configuration":
            return httpx.Response(
                200, json={"issuer": self.url, "jwks_uri": f"{self.url}/keys"}
            )
        if str(request.url) == f"{self.url}/keys":
            return httpx.Response(200, json=self.jwks())
        return None

    def token(self, audience, subject="user", expires_in=300, kid=None, **claims):
        now = int(time.time())
        payload = {
            "iss": self.url,
            "aud": audience,
            "sub": subject,
            "iat": now,
            "exp": now + expires_in,
            **claims,
        }
        return jwt.encode(
            payload,
            self.private_key,
            algorithm="RS256",
            headers={"kid": kid or self.kid},
        )


@functools.cache
def rsa_key(_name):
    return cryptography.hazmat.primitives.asymmetric.rsa.generate_private_key(
        public_exponent=65537, key_size=2048
    )


def mock_http_client(*issuers):
    def handler(request):
        for issuer in issuers:
            response = issuer.handle(request)
            if response is not None:
                return response
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def jenkins():
    return TokenIssuer("https://jenkins.example/oidc")


@pytest.fixture
def entra():
    return TokenIssuer("https://login.example/tenant/v2.0")


@pytest.fixture
def graph(remote):
    """
    master:      c1 - c2 ------- m1 - c3
                       \\       /
    feature/a:          a1 - a2
    release/1.0:  c2 - r1
    feature/x:    c3 - x1 - x2
    """
    shas = {}
    shas["c1"] = remote.commit("c1")
    shas["c2"] = remote.commit("c2")
    remote.git("checkout", "--quiet", "-b", "feature/a")
    shas["a1"] = remote.commit("a1")
    shas["a2"] = remote.commit("a2")
    remote.git("checkout", "--quiet", "master")
    shas["m1"] = remote.merge("feature/a", "Merge feature/a")
    shas["c3"] = remote.commit("c3")
    remote.git("checkout", "--quiet", "-b", "release/1.0", shas["c2"])
    shas["r1"] = remote.commit("r1")
    remote.git("checkout", "--quiet", "-b", "feature/x", shas["c3"])
    shas["x1"] = remote.commit("x1")
    shas["x2"] = remote.commit("x2")
    remote.git("checkout", "--quiet", "master")
    remote.git("tag", "v1", shas["c2"])
    return shas


def app_config(tmp_path, remote, jenkins, entra):
    return revtrend.config.parse(
        {
            "server": {
                "public_url": "https://revtrend.example",
                "data_dir": str(tmp_path / "data"),
            },
            "cli_auth": {
                "authority": entra.url,
                "client_id": "client-id",
                "scopes": ["api://revtrend/Metrics.Write", "offline_access"],
            },
            "issuers": [
                {
                    "name": "jenkins",
                    "issuer": jenkins.url,
                    "audience": "revtrend",
                    "source": "ci",
                    "rules": [
                        {
                            "claim": "sub",
                            "regex": "^job/test/",
                            "grants": [{"project": "test", "role": "writer"}],
                        },
                    ],
                },
                {
                    "name": "entra",
                    "issuer": entra.url,
                    "audience": "client-id",
                    "source": "manual",
                    "rules": [
                        {
                            "claim": "roles",
                            "contains": role,
                            "grants": [{"project": "test", "role": role}],
                        }
                        for role in ("reader", "writer", "admin")
                    ],
                },
            ],
            "projects": [
                {"name": "test", "remote_url": remote.url, "compare_max_walk": 3}
            ],
        }
    )


class Client(fastapi.testclient.TestClient):
    def __init__(self, app, jenkins, entra):
        super().__init__(app)
        self.jenkins = jenkins
        self.entra = entra

    def as_ci(self, job="job/test/master"):
        return {
            "Authorization": f"Bearer {self.jenkins.token('revtrend', subject=job)}"
        }

    def as_user(self, *roles):
        return {
            "Authorization": f"Bearer {self.entra.token('client-id', subject='alice', roles=list(roles))}"
        }

    def wait_ready(self):
        deadline = time.monotonic() + 10
        while not all(
            p["ready"] for p in self.get("/healthz").json()["projects"].values()
        ):
            assert time.monotonic() < deadline, "mirror not ready"
            time.sleep(0.02)


@pytest.fixture
def client(tmp_path, remote, graph, jenkins, entra):  # noqa: ARG001
    cfg = app_config(tmp_path, remote, jenkins, entra)
    app = revtrend.server.create_app(cfg, http_client=mock_http_client(jenkins, entra))
    with Client(app, jenkins, entra) as c:
        c.wait_ready()
        yield c
