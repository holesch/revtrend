import asyncio
import dataclasses
import fnmatch
import json
import logging
import re
import time
import typing

import fastapi
import fastapi.responses
import pydantic

import revtrend.auth as auth
import revtrend.config as config
import revtrend.db as db
import revtrend.mirror as mirror
import revtrend.store as store

log = logging.getLogger(__name__)

router = fastapi.APIRouter()

FETCH_TIMEOUT = 60
ON_DEMAND_FETCH_INTERVAL = 30
METRIC_RE = re.compile(r"^[a-z0-9_.]+$")
LABEL_KEY_RE = re.compile(r"^[a-z0-9_]+$")

Sha = typing.Annotated[str, pydantic.StringConstraints(pattern=r"^[0-9a-f]{40}$")]
MetricName = typing.Annotated[
    str, pydantic.StringConstraints(pattern=METRIC_RE.pattern, max_length=128)
]
LabelKey = typing.Annotated[
    str, pydantic.StringConstraints(pattern=LABEL_KEY_RE.pattern, max_length=64)
]
LabelValue = typing.Annotated[
    str, pydantic.StringConstraints(min_length=1, max_length=256)
]


@dataclasses.dataclass
class Project:
    config: config.Project
    id: int
    mirror: mirror.Mirror


@dataclasses.dataclass
class State:
    config: config.Config
    db: db.Database
    auth: auth.Authenticator
    projects: dict[str, Project]


def get_state(request: fastapi.Request):
    return request.app.state.revtrend


async def get_principal(
    state: typing.Annotated[State, fastapi.Depends(get_state)],
    authorization: typing.Annotated[str | None, fastapi.Header()] = None,
):
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise auth.AuthError("missing bearer token")
    return await state.auth.authenticate(token.strip())


def project_access(role):
    async def dependency(
        project: str,
        state: typing.Annotated[State, fastapi.Depends(get_state)],
        principal: typing.Annotated[auth.Principal, fastapi.Depends(get_principal)],
    ):
        if project not in state.projects:
            raise fastapi.HTTPException(404, f"unknown project {project!r}")
        if not principal.has_role(project, role):
            raise fastapi.HTTPException(
                403, f"role {role!r} required for project {project!r}"
            )
        return Access(state=state, project=state.projects[project], principal=principal)

    return dependency


@dataclasses.dataclass
class Access:
    state: State
    project: Project
    principal: auth.Principal


StateDep = typing.Annotated[State, fastapi.Depends(get_state)]
PrincipalDep = typing.Annotated[auth.Principal, fastapi.Depends(get_principal)]
ReaderDep = typing.Annotated[Access, fastapi.Depends(project_access("reader"))]
WriterDep = typing.Annotated[Access, fastapi.Depends(project_access("writer"))]
AdminDep = typing.Annotated[Access, fastapi.Depends(project_access("admin"))]
Aggregation = typing.Literal[tuple(store.AGGREGATIONS)]
MetricsQuery = typing.Annotated[list[str], fastapi.Query(alias="metric")]
LabelsQuery = typing.Annotated[list[str], fastapi.Query()]


@router.get("/healthz")
async def healthz(state: StateDep):
    return {
        "status": "ok",
        "projects": {
            name: {"ready": p.mirror.ready} for name, p in state.projects.items()
        },
    }


class Metric(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    name: MetricName
    value: pydantic.FiniteFloat
    labels: typing.Annotated[
        dict[LabelKey, LabelValue], pydantic.Field(max_length=16)
    ] = {}


class Measurements(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(extra="forbid")
    sha: Sha
    run_id: typing.Annotated[
        str, pydantic.StringConstraints(min_length=1, max_length=512)
    ]
    metrics: typing.Annotated[
        list[Metric], pydantic.Field(min_length=1, max_length=10000)
    ]

    @pydantic.model_validator(mode="after")
    def unique_series(self):
        keys = [(m.name, tuple(sorted(m.labels.items()))) for m in self.metrics]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate metric name/labels combination")
        return self


@router.post("/api/v1/projects/{project}/measurements", status_code=201)
async def ingest(body: Measurements, access: WriterDep, response: fastapi.Response):
    await require_commit(access.project.mirror, body.sha)
    principal = access.principal
    run = store.Run(
        project_id=access.project.id,
        sha=body.sha,
        run_id=body.run_id,
        source=principal.source,
        submitted_by=principal.submitted_by,
        measured_at=int(time.time()),
    )
    metrics = [(m.name, m.labels, m.value) for m in body.metrics]
    try:
        created = await access.state.db.write(store.insert_measurements, run, metrics)
    except store.ConflictError as e:
        raise fastapi.HTTPException(409, str(e)) from e
    log.info(
        "ingest project=%s sha=%s run_id=%s source=%s submitted_by=%s metrics=%d created=%d",
        access.project.config.name,
        body.sha,
        body.run_id,
        principal.source,
        principal.submitted_by,
        len(body.metrics),
        created,
    )
    if not created:
        response.status_code = 200
    return {"created": created, "existing": len(body.metrics) - created}


async def require_commit(m, sha):
    """Fetches if `sha` is unknown. Only commits that exist in the remote are accepted."""
    if await m.has_commit(sha):
        return
    requested = time.monotonic()
    try:
        await asyncio.wait_for(
            asyncio.shield(m.fetch(not_before=requested)), FETCH_TIMEOUT
        )
    except (TimeoutError, mirror.GitError) as e:
        log.warning("fetch for unknown commit %s failed: %s", sha, e)
        raise fastapi.HTTPException(503, "fetching from remote failed") from e
    if not await m.has_commit(sha):
        raise fastapi.HTTPException(409, f"commit {sha} not found in remote")


@router.get("/api/v1/projects/{project}/series")
async def series(  # noqa: PLR0913
    access: ReaderDep,
    metrics: MetricsQuery,
    ref: str = "default",
    base: str = "default",
    from_ms: typing.Annotated[int | None, fastapi.Query(alias="from")] = None,
    to_ms: typing.Annotated[int | None, fastapi.Query(alias="to")] = None,
    labels: LabelsQuery = (),
    agg: Aggregation = "median",
):
    metric_names = parse_metrics(metrics)
    label_filters = parse_label_filters(labels)
    m = access.project.mirror
    ref_sha = await resolve(m, ref)
    base_sha = await resolve(m, base)
    history = await m.history(ref_sha)
    on_branch = await m.branch_only(base_sha, ref_sha)
    commits = [
        (idx, c)
        for idx, c in enumerate(history)
        if (from_ms is None or c.time * 1000 >= from_ms)
        and (to_ms is None or c.time * 1000 <= to_ms)
    ]
    rows = await access.state.db.read(
        store.measurements,
        access.project.id,
        [c.sha for _, c in commits],
        metric_names,
        label_filters,
    )
    points = store.aggregate(rows, agg)
    return fastapi.responses.JSONResponse(series_rows(commits, points, on_branch))


def series_rows(commits, points, on_branch):
    """Flat rows for Grafana, oldest commit first."""
    by_sha = {}
    for (sha, metric, labels_json), point in points.items():
        by_sha.setdefault(sha, []).append((metric, labels_json, point))
    result = []
    for idx, commit in reversed(commits):
        for metric, labels_json, point in sorted(by_sha.get(commit.sha, ())):
            series_labels = json.loads(labels_json)
            result.append(
                {
                    "time": commit.time * 1000,
                    "idx": idx,
                    "sha": commit.sha,
                    "short": commit.sha[:8],
                    "subject": commit.subject,
                    "metric": metric,
                    "labels": series_labels,
                    "series": series_name(metric, series_labels),
                    "value": point.value,
                    "runs": point.runs,
                    "source": point.source,
                    "on_branch": commit.sha in on_branch,
                }
            )
    return result


def parse_metrics(values):
    names = [n.strip() for v in values for n in v.split(",") if n.strip()]
    invalid = [n for n in names if not METRIC_RE.match(n)]
    if invalid or not names:
        raise fastapi.HTTPException(400, f"invalid metric names: {invalid}")
    return names


def parse_label_filters(values):
    filters = {}
    for value in values:
        key, sep, label_value = value.partition(":")
        if not sep or not LABEL_KEY_RE.match(key):
            raise fastapi.HTTPException(
                400, f"invalid label filter {value!r}, expected key:value"
            )
        filters.setdefault(key, []).append(label_value)
    return filters


async def resolve(m, ref):
    """Resolves a ref name or full SHA; 'default' means the remote's default branch.

    Unknown refs trigger a (rate-limited) fetch before giving up.
    """
    if ref == "default":
        ref = m.default_branch
    try:
        return await m.resolve(ref)
    except mirror.UnknownRefError:
        pass
    try:
        not_before = time.monotonic() - ON_DEMAND_FETCH_INTERVAL
        await asyncio.wait_for(
            asyncio.shield(m.fetch(not_before=not_before)), FETCH_TIMEOUT
        )
    except (TimeoutError, mirror.GitError) as e:
        log.warning("fetch for unknown ref %s failed: %s", ref, e)
    return await m.resolve(ref)


def series_name(metric, labels):
    if not labels:
        return metric
    return f"{metric}{{{','.join(f'{k}={v}' for k, v in sorted(labels.items()))}}}"


@router.get("/api/v1/projects")
async def projects(state: StateDep, principal: PrincipalDep):
    return [
        {"name": name, "default_branch": p.mirror.default_branch}
        for name, p in state.projects.items()
        if principal.has_role(name, "reader")
    ]


@router.get("/api/v1/projects/{project}/refs")
async def refs(
    access: ReaderDep, active_since: str | None = None, match: str | None = None
):
    m = access.project.mirror
    min_time = 0
    if active_since is not None:
        try:
            min_time = time.time() - config.parse_duration(active_since)
        except config.ConfigError as e:
            raise fastapi.HTTPException(400, str(e)) from e
    result = [
        {
            "name": r.name,
            "default": r.name == m.default_branch,
            "sha": r.sha,
            "time": r.time * 1000,
        }
        for r in await m.refs()
        if (r.time >= min_time or r.name == m.default_branch)
        and (match is None or fnmatch.fnmatchcase(r.name, match))
    ]
    result.sort(key=lambda r: (not r["default"], -r["time"], r["name"]))
    return result


@router.get("/api/v1/projects/{project}/metrics")
async def metrics(access: ReaderDep):
    return await access.state.db.read(store.metrics, access.project.id)
