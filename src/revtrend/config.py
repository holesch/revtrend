import dataclasses
import pathlib
import re
import tomllib

ROLES = ("reader", "writer", "admin")
SOURCES = ("ci", "manual")
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
DURATION_RE = re.compile(r"^(\d+)([smhd])$")
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class ConfigError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Server:
    host: str
    port: int
    public_url: str
    data_dir: pathlib.Path


@dataclasses.dataclass(frozen=True)
class CliAuth:
    authority: str
    client_id: str
    scopes: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Grant:
    project: str
    role: str


@dataclasses.dataclass(frozen=True)
class Rule:
    claim: str
    grants: tuple[Grant, ...]
    regex: re.Pattern | None = None
    contains: str | None = None
    source: str | None = None


@dataclasses.dataclass(frozen=True)
class Issuer:
    name: str
    issuer: str
    audience: str
    source: str
    rules: tuple[Rule, ...]


@dataclasses.dataclass(frozen=True)
class Project:
    name: str
    remote_url: str
    credentials_file: pathlib.Path | None
    fetch_interval: int
    compare_max_walk: int


@dataclasses.dataclass(frozen=True)
class Config:
    server: Server
    cli_auth: CliAuth | None
    issuers: tuple[Issuer, ...]
    projects: tuple[Project, ...]

    def project(self, name):
        for project in self.projects:
            if project.name == name:
                return project
        raise KeyError(name)


def load(path):
    with open(path, "rb") as f:
        return parse(tomllib.load(f))


def parse(data):
    projects = tuple(_parse_project(p) for p in data.get("projects", []))
    names = [p.name for p in projects]
    if len(names) != len(set(names)):
        raise ConfigError("duplicate project names")
    issuers = tuple(_parse_issuer(i, set(names)) for i in data.get("issuers", []))
    if len({i.issuer for i in issuers}) != len(issuers):
        raise ConfigError("duplicate issuer URLs")
    cli_auth = data.get("cli_auth")
    return Config(
        server=_parse_server(data.get("server", {})),
        cli_auth=_parse_cli_auth(cli_auth) if cli_auth is not None else None,
        issuers=issuers,
        projects=projects,
    )


def _parse_server(data):
    host, _, port = data.get("listen", "127.0.0.1:8080").rpartition(":")
    return Server(
        host=host,
        port=int(port),
        public_url=data["public_url"].rstrip("/"),
        data_dir=pathlib.Path(data.get("data_dir", "/var/lib/revtrend")),
    )


def _parse_cli_auth(data):
    return CliAuth(
        authority=data["authority"].rstrip("/"),
        client_id=data["client_id"],
        scopes=tuple(data["scopes"]),
    )


def _parse_issuer(data, project_names):
    source = data["source"]
    _check_source(source)
    return Issuer(
        name=data["name"],
        issuer=data["issuer"],
        audience=data["audience"],
        source=source,
        rules=tuple(_parse_rule(r, project_names) for r in data.get("rules", [])),
    )


def _parse_rule(data, project_names):
    if ("regex" in data) == ("contains" in data):
        raise ConfigError(
            f"rule on claim {data['claim']!r} needs exactly one of 'regex', 'contains'"
        )
    source = data.get("source")
    if source is not None:
        _check_source(source)
    return Rule(
        claim=data["claim"],
        grants=tuple(_parse_grant(g, project_names) for g in data["grants"]),
        regex=re.compile(data["regex"]) if "regex" in data else None,
        contains=data.get("contains"),
        source=source,
    )


def _parse_grant(data, project_names):
    if data["project"] not in project_names:
        raise ConfigError(f"grant for unknown project {data['project']!r}")
    if data["role"] not in ROLES:
        raise ConfigError(f"invalid role {data['role']!r}, expected one of {ROLES}")
    return Grant(project=data["project"], role=data["role"])


def _parse_project(data):
    name = data["name"]
    if not PROJECT_NAME_RE.match(name):
        raise ConfigError(f"invalid project name {name!r}")
    credentials_file = data.get("credentials_file")
    return Project(
        name=name,
        remote_url=data["remote_url"],
        credentials_file=pathlib.Path(credentials_file) if credentials_file else None,
        fetch_interval=parse_duration(data.get("fetch_interval", "5m")),
        compare_max_walk=int(data.get("compare_max_walk", 50)),
    )


def parse_duration(value):
    match = DURATION_RE.match(value)
    if not match:
        raise ConfigError(
            f"invalid duration {value!r}, expected e.g. '30s', '5m', '90d'"
        )
    return int(match[1]) * DURATION_UNITS[match[2]]


def _check_source(source):
    if source not in SOURCES:
        raise ConfigError(f"invalid source {source!r}, expected one of {SOURCES}")
