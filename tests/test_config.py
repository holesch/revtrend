import pathlib

import pytest

import revtrend.config as config

EXAMPLE = """
[server]
listen = "127.0.0.1:9000"
public_url = "https://revtrend.example/"
data_dir = "/srv/revtrend"

[cli_auth]
authority = "https://login.example/tenant/v2.0"
client_id = "client"
scopes = ["api://revtrend/Metrics.Write", "offline_access"]

[[issuers]]
name = "jenkins"
issuer = "https://jenkins.example/oidc"
audience = "revtrend"
source = "ci"

  [[issuers.rules]]
  claim = "sub"
  regex = '^https://jenkins\\.example/job/myproject/'
  grants = [{ project = "myproject", role = "writer" }]

[[issuers]]
name = "entra"
issuer = "https://login.example/tenant/v2.0"
audience = "client"
source = "manual"

  [[issuers.rules]]
  claim = "roles"
  contains = "myproject.Reader"
  grants = [{ project = "myproject", role = "reader" }]

[[projects]]
name = "myproject"
remote_url = "https://git.example/scm/myproject.git"
credentials_file = "/etc/revtrend/myproject.token"
fetch_interval = "10m"
"""


def test_load(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(EXAMPLE)
    cfg = config.load(path)
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 9000
    assert cfg.server.public_url == "https://revtrend.example"
    assert cfg.server.data_dir == pathlib.Path("/srv/revtrend")
    assert cfg.cli_auth.scopes == ("api://revtrend/Metrics.Write", "offline_access")
    jenkins, entra = cfg.issuers
    assert jenkins.source == "ci"
    assert jenkins.rules[0].regex.match(
        "https://jenkins.example/job/myproject/job/master"
    )
    assert entra.rules[0].contains == "myproject.Reader"
    assert entra.rules[0].grants == (config.Grant("myproject", "reader"),)
    project = cfg.project("myproject")
    assert project.fetch_interval == 600
    assert project.compare_max_walk == 50
    assert project.credentials_file == pathlib.Path("/etc/revtrend/myproject.token")


def test_grant_for_unknown_project():
    data = {
        "server": {"public_url": "https://x"},
        "issuers": [
            {
                "name": "i",
                "issuer": "https://i",
                "audience": "a",
                "source": "ci",
                "rules": [
                    {
                        "claim": "sub",
                        "contains": "x",
                        "grants": [{"project": "nope", "role": "reader"}],
                    }
                ],
            }
        ],
    }
    with pytest.raises(config.ConfigError, match="unknown project"):
        config.parse(data)


def test_invalid_source():
    data = {
        "server": {"public_url": "https://x"},
        "issuers": [
            {"name": "i", "issuer": "https://i", "audience": "a", "source": "bogus"}
        ],
    }
    with pytest.raises(config.ConfigError, match="invalid source"):
        config.parse(data)


@pytest.mark.parametrize(
    ("value", "seconds"), [("30s", 30), ("5m", 300), ("2h", 7200), ("90d", 7776000)]
)
def test_parse_duration(value, seconds):
    assert config.parse_duration(value) == seconds


def test_parse_duration_invalid():
    with pytest.raises(config.ConfigError):
        config.parse_duration("5 minutes")
