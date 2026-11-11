import os
import subprocess
import time

import cryptography.hazmat.primitives.asymmetric.rsa
import httpx
import jwt
import pytest

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

    def __init__(self, url, kid="key-1"):
        self.url = url
        self.kid = kid
        self.private_key = (
            cryptography.hazmat.primitives.asymmetric.rsa.generate_private_key(
                public_exponent=65537, key_size=2048
            )
        )
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
