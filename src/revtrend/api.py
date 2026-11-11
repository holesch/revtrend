import dataclasses
import typing

import fastapi

import revtrend.auth as auth
import revtrend.config as config
import revtrend.db as db
import revtrend.mirror as mirror

router = fastapi.APIRouter()


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
