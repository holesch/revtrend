import re

import conftest
import pytest

import revtrend.auth as auth
import revtrend.config as config


def issuers(jenkins, entra):
    return [
        config.Issuer(
            name="jenkins",
            issuer=jenkins.url,
            audience="revtrend",
            source="ci",
            rules=(
                config.Rule(
                    claim="sub",
                    regex=re.compile(r"^https://jenkins\.example/job/myproject/"),
                    grants=(config.Grant("myproject", "writer"),),
                ),
            ),
        ),
        config.Issuer(
            name="entra",
            issuer=entra.url,
            audience="client-id",
            source="manual",
            rules=(
                config.Rule(
                    claim="roles",
                    contains="myproject.Reader",
                    grants=(config.Grant("myproject", "reader"),),
                ),
                config.Rule(
                    claim="roles",
                    contains="myproject.Admin",
                    grants=(config.Grant("myproject", "admin"),),
                ),
                config.Rule(
                    claim="azp",
                    contains="automation",
                    grants=(config.Grant("myproject", "writer"),),
                    source="ci",
                ),
            ),
        ),
    ]


@pytest.fixture
async def authenticator(jenkins, entra):
    async with conftest.mock_http_client(jenkins, entra) as http:
        yield auth.Authenticator(issuers(jenkins, entra), http)


async def test_jenkins_job_gets_writer_and_ci_source(authenticator, jenkins):
    token = jenkins.token(
        "revtrend", subject="https://jenkins.example/job/myproject/job/master"
    )
    principal = await authenticator.authenticate(token)
    assert principal.source == "ci"
    assert principal.roles == {"myproject": "writer"}
    assert principal.has_role("myproject", "reader")
    assert principal.has_role("myproject", "writer")
    assert not principal.has_role("myproject", "admin")
    assert (
        principal.submitted_by
        == "jenkins:https://jenkins.example/job/myproject/job/master"
    )


async def test_other_jenkins_job_gets_nothing(authenticator, jenkins):
    token = jenkins.token(
        "revtrend", subject="https://jenkins.example/job/other/job/master"
    )
    principal = await authenticator.authenticate(token)
    assert principal.roles == {}
    assert not principal.has_role("myproject", "reader")


async def test_entra_highest_role_wins(authenticator, entra):
    token = entra.token("client-id", roles=["myproject.Admin", "myproject.Reader"])
    principal = await authenticator.authenticate(token)
    assert principal.roles == {"myproject": "admin"}
    assert principal.source == "manual"


async def test_rule_source_overrides_issuer_source(authenticator, entra):
    principal = await authenticator.authenticate(
        entra.token("client-id", azp="automation")
    )
    assert principal.source == "ci"


async def test_wrong_audience(authenticator, jenkins):
    with pytest.raises(auth.AuthError, match="Audience"):
        await authenticator.authenticate(jenkins.token("client-id"))


async def test_expired(authenticator, jenkins):
    with pytest.raises(auth.AuthError, match="expired"):
        await authenticator.authenticate(jenkins.token("revtrend", expires_in=-3600))


async def test_unknown_issuer(authenticator):
    other = conftest.TokenIssuer("https://evil.example")
    with pytest.raises(auth.AuthError, match="unknown issuer"):
        await authenticator.authenticate(other.token("revtrend"))


async def test_forged_signature(authenticator, jenkins):
    forger = conftest.TokenIssuer(jenkins.url)
    with pytest.raises(auth.AuthError, match="Signature"):
        await authenticator.authenticate(forger.token("revtrend"))


async def test_hs256_rejected(authenticator, jenkins):
    token = auth.jwt.encode(
        {"iss": jenkins.url, "aud": "revtrend", "sub": "x", "exp": 9999999999},
        "k" * 32,
        headers={"kid": jenkins.kid},
    )
    with pytest.raises(auth.AuthError):
        await authenticator.authenticate(token)


async def test_malformed(authenticator):
    with pytest.raises(auth.AuthError, match="malformed"):
        await authenticator.authenticate("not-a-jwt")


async def test_keys_are_cached_and_refresh_is_rate_limited(authenticator, jenkins):
    await authenticator.authenticate(jenkins.token("revtrend"))
    await authenticator.authenticate(jenkins.token("revtrend"))
    assert len(jenkins.requests) == 2
    with pytest.raises(auth.AuthError, match="no signing key"):
        await authenticator.authenticate(jenkins.token("revtrend", kid="rotated"))
    assert len(jenkins.requests) == 2


async def test_unknown_kid_triggers_refresh(authenticator, jenkins, monkeypatch):
    await authenticator.authenticate(jenkins.token("revtrend"))
    monkeypatch.setattr(auth, "JWKS_MIN_REFRESH_INTERVAL", 0)
    jenkins.kid = "rotated"
    await authenticator.authenticate(jenkins.token("revtrend"))
    assert len(jenkins.requests) == 4
