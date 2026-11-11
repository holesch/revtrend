import asyncio
import dataclasses
import logging
import time
import typing

import fastapi
import pydantic

import revtrend.auth as auth
import revtrend.config as config
import revtrend.db as db
import revtrend.mirror as mirror
import revtrend.store as store

log = logging.getLogger(__name__)

router = fastapi.APIRouter()

FETCH_TIMEOUT = 60

Sha = typing.Annotated[str, pydantic.StringConstraints(pattern=r"^[0-9a-f]{40}$")]
MetricName = typing.Annotated[
    str, pydantic.StringConstraints(pattern=r"^[a-z0-9_.]+$", max_length=128)
]
LabelKey = typing.Annotated[
    str, pydantic.StringConstraints(pattern=r"^[a-z0-9_]+$", max_length=64)
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
