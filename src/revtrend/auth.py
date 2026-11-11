import asyncio
import dataclasses
import logging
import time

import jwt

import revtrend.config as config

log = logging.getLogger(__name__)

ROLE_LEVELS = {role: level for level, role in enumerate(config.ROLES, start=1)}
ALGORITHMS = [
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
]
LEEWAY = 60
JWKS_MIN_REFRESH_INTERVAL = 60


class AuthError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Principal:
    issuer: str
    subject: str
    source: str
    roles: dict[str, str]

    @property
    def submitted_by(self):
        return f"{self.issuer}:{self.subject}"

    def has_role(self, project, role):
        granted = self.roles.get(project)
        return granted is not None and ROLE_LEVELS[granted] >= ROLE_LEVELS[role]


class Authenticator:
    """Validates bearer JWTs from the configured issuers and maps claims to roles."""

    def __init__(self, issuers, http_client):
        self._issuers = {i.issuer: i for i in issuers}
        self._http = http_client
        self._keys = {url: _IssuerKeys() for url in self._issuers}

    async def authenticate(self, token):
        try:
            header = jwt.get_unverified_header(token)
            unverified = jwt.decode(token, options={"verify_signature": False})
        except jwt.PyJWTError as e:
            raise AuthError(f"malformed token: {e}") from e
        issuer = self._issuers.get(unverified.get("iss"))
        if issuer is None:
            raise AuthError("unknown issuer")
        key = await self._key(issuer, header.get("kid"))
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=ALGORITHMS,
                audience=issuer.audience,
                issuer=issuer.issuer,
                leeway=LEEWAY,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as e:
            raise AuthError(f"invalid token: {e}") from e
        return principal(issuer, claims)

    async def _key(self, issuer, kid):
        keys = self._keys[issuer.issuer]
        key = keys.find(kid)
        if key is None:
            await keys.refresh(self._http, issuer.issuer)
            key = keys.find(kid)
        if key is None:
            raise AuthError(f"no signing key {kid!r} for issuer {issuer.name}")
        return key


class _IssuerKeys:
    def __init__(self):
        self.jwks = None
        self.fetched_at = None
        self.lock = asyncio.Lock()

    def find(self, kid):
        if self.jwks is None:
            return None
        if kid is None:
            return self.jwks.keys[0] if len(self.jwks.keys) == 1 else None
        for key in self.jwks.keys:
            if key.key_id == kid:
                return key
        return None

    async def refresh(self, http, issuer_url):
        async with self.lock:
            now = time.monotonic()
            if (
                self.fetched_at is not None
                and now - self.fetched_at < JWKS_MIN_REFRESH_INTERVAL
            ):
                return
            self.fetched_at = now
            discovery = await _get_json(
                http, f"{issuer_url.rstrip('/')}/.well-known/openid-configuration"
            )
            self.jwks = jwt.PyJWKSet.from_dict(
                await _get_json(http, discovery["jwks_uri"])
            )
            log.info("loaded %d signing keys for %s", len(self.jwks.keys), issuer_url)


async def _get_json(http, url):
    response = await http.get(url)
    response.raise_for_status()
    return response.json()


def principal(issuer, claims):
    roles = {}
    source = None
    for rule in issuer.rules:
        if not matches(rule, claims.get(rule.claim)):
            continue
        for grant in rule.grants:
            current = roles.get(grant.project)
            if current is None or ROLE_LEVELS[grant.role] > ROLE_LEVELS[current]:
                roles[grant.project] = grant.role
        if source is None:
            source = rule.source
    return Principal(
        issuer=issuer.name,
        subject=claims["sub"],
        source=source or issuer.source,
        roles=roles,
    )


def matches(rule, value):
    values = value if isinstance(value, list) else [value]
    values = [v for v in values if isinstance(v, str)]
    if rule.regex is not None:
        return any(rule.regex.search(v) for v in values)
    return rule.contains in values
