import argparse
import asyncio
import contextlib
import logging

import fastapi
import fastapi.encoders
import fastapi.exceptions
import fastapi.responses
import httpx
import uvicorn

import revtrend.api as api
import revtrend.auth as auth
import revtrend.config as config
import revtrend.db as db
import revtrend.mirror as mirror

log = logging.getLogger(__name__)

INIT_RETRY_INTERVAL = 60


def main():
    parser = argparse.ArgumentParser(prog="revtrend-server")
    parser.add_argument(
        "--config", type=config.load, default="/etc/revtrend/config.toml"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    uvicorn.run(
        create_app(args.config),
        host=args.config.server.host,
        port=args.config.server.port,
    )


def create_app(cfg, http_client=None):
    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with contextlib.AsyncExitStack() as stack:
            http = http_client or await stack.enter_async_context(
                httpx.AsyncClient(timeout=10)
            )
            state = await stack.enter_async_context(running_state(cfg, http))
            app.state.revtrend = state
            tasks = [
                asyncio.create_task(maintain_mirror(state.db, p))
                for p in state.projects.values()
            ]
            try:
                yield
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    app = fastapi.FastAPI(title="revtrend", lifespan=lifespan)
    app.include_router(api.router)
    app.add_exception_handler(
        fastapi.exceptions.RequestValidationError, validation_error
    )
    app.add_exception_handler(auth.AuthError, auth_error)
    app.add_exception_handler(mirror.NotReadyError, not_ready_error)
    app.add_exception_handler(mirror.UnknownRefError, unknown_ref_error)
    return app


@contextlib.asynccontextmanager
async def running_state(cfg, http):
    cfg.server.data_dir.mkdir(parents=True, exist_ok=True)
    database = db.Database(cfg.server.data_dir / "metrics.db")
    database.open()
    try:
        ids = await database.write(db.sync_projects, [p.name for p in cfg.projects])
        projects = {p.name: make_project(cfg, p, ids[p.name]) for p in cfg.projects}
        yield api.State(
            config=cfg,
            db=database,
            auth=auth.Authenticator(cfg.issuers, http),
            projects=projects,
        )
    finally:
        database.close()


def make_project(cfg, project_cfg, project_id):
    path = cfg.server.data_dir / "repos" / f"{project_cfg.name}.git"
    return api.Project(
        config=project_cfg,
        id=project_id,
        mirror=mirror.Mirror(
            path, project_cfg.remote_url, project_cfg.credentials_file
        ),
    )


async def maintain_mirror(database, project):
    """Clones the mirror if needed, then fetches periodically."""
    m = project.mirror
    while True:
        try:
            if m.ready:
                await m.fetch()
            else:
                await m.init()
            await database.write(db.set_default_branch, project.id, m.default_branch)
        except Exception:
            log.exception("updating mirror of %s failed", project.config.name)
        interval = project.config.fetch_interval
        if not m.ready:
            interval = min(interval, INIT_RETRY_INTERVAL)
        await asyncio.sleep(interval)


async def validation_error(_request, exc):
    detail = fastapi.encoders.jsonable_encoder(
        exc.errors(), custom_encoder={Exception: str}
    )
    return fastapi.responses.JSONResponse({"detail": detail}, status_code=400)


async def auth_error(_request, exc):
    return fastapi.responses.JSONResponse(
        {"detail": str(exc)},
        status_code=401,
        headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
    )


async def not_ready_error(_request, exc):
    return fastapi.responses.JSONResponse(
        {"detail": str(exc)}, status_code=503, headers={"Retry-After": "30"}
    )


async def unknown_ref_error(_request, exc):
    return fastapi.responses.JSONResponse(
        {"detail": f"unknown ref {exc}"}, status_code=404
    )
