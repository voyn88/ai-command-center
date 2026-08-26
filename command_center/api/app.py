"""FastAPI application for the AI Command Center HTTP/JSON API.

Not a read-only application, whatever earlier revisions of this docstring said:
it mounts 27 mutating routes alongside the read surface. All of them are
authenticated and authorized by :mod:`command_center.http_auth`, mounted once
as an ``include_router`` dependency below and verified against the routing
table by ``validate_routing`` before this factory returns.

Run it with::

    uvicorn command_center.api.app:app

``app`` is a module-level instance (built by :func:`create_app`) so the uvicorn
target above resolves directly; tests build their own isolated instance with
``create_app()`` and a :class:`fastapi.testclient.TestClient`.

Controllers only: every handler is a one-liner that delegates to
:mod:`command_center.api.service` (read paths) or
:mod:`command_center.api.wave1_service` (the Wave-1 write paths, mounted from
:mod:`command_center.api.wave1_routes`) and returns a typed
:mod:`command_center.api.schemas` / :mod:`command_center.api.models` model. No
business logic, no data access and no mutation lives in this module.

Versioning: **every** route is mounted under the ``/api/v1`` prefix. Pinning the
version before any client consumes the contract keeps a future ``/api/v2`` a
clean, additive move rather than a breaking rename of live paths.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from command_center.api import (
    audit_routes,
    backlog_routes,
    conflict_routes,
    council_routes,
    marketplace_routes,
    model_registry_routes,
    networking_routes,
    schemas,
    service,
    wave1_routes,
)
from command_center.http_auth.routing import enforce, validate_routing

# All read-only endpoints hang off one versioned router; the Wave-1 write
# endpoints live on their own router (also ``/api/v1``) and are included below.
_API_PREFIX = "/api/v1"


def _build_read_router() -> APIRouter:
    router = APIRouter(prefix=_API_PREFIX)

    @router.get("/health", response_model=schemas.HealthResponse)
    def health() -> schemas.HealthResponse:
        return service.get_health()

    @router.get("/dashboard", response_model=schemas.DashboardResponse)
    def dashboard() -> schemas.DashboardResponse:
        return service.build_dashboard()

    @router.get("/projects", response_model=list[schemas.Project])
    def projects() -> list[schemas.Project]:
        return service.list_projects()

    @router.get("/projects/{project_id}", response_model=schemas.Project)
    def project(project_id: str) -> schemas.Project:
        found = service.get_project(project_id)
        if found is None:
            raise HTTPException(status_code=404, detail="project not found")
        return found

    @router.get("/tasks", response_model=schemas.TaskList)
    def tasks(
        project: str | None = None, status: str | None = None
    ) -> schemas.TaskList:
        return service.list_tasks(project=project, status=status)

    # Declared before ``/tasks/{task_id}`` so the literal ``graph`` segment is
    # never captured as a task id by the parametrised route below.
    @router.get("/tasks/graph", response_model=schemas.TaskGraph)
    def tasks_graph(project: str | None = None) -> schemas.TaskGraph:
        return service.task_graph(project=project)

    @router.get("/tasks/{task_id}", response_model=schemas.Task)
    def task(task_id: str) -> schemas.Task:
        found = service.get_task(task_id)
        if found is None:
            raise HTTPException(status_code=404, detail="task not found")
        return found

    @router.get("/agents", response_model=schemas.AgentsResponse)
    def agents() -> schemas.AgentsResponse:
        return service.list_agents()

    return router


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Open the PostgreSQL pool for the life of the process, when configured.

    Same contract as `command_center.webapi.app._lifespan`: absence of
    `AICC_PG_HOST` means "this deployment has no server backlog" (a
    developer's own SQLite-backed instance), not a misconfiguration —
    startup proceeds and `backlog_routes` surfaces 503 per request. A host
    that *is* set but unusable is fatal here on purpose, same reasoning as
    the webapi twin: a bad DSN should stop the deploy, not surface as a 503
    storm on every dashboard poll.
    """
    opened = False
    if os.environ.get("AICC_PG_HOST"):
        from command_center.db import pool

        pool.open_pool()
        opened = True
    try:
        yield
    finally:
        if opened:
            from command_center.db import pool

            pool.close_pool()


def create_app() -> FastAPI:
    app = FastAPI(
        title="AI Command Center API",
        version=service.get_health().version,
        summary="Backend for the desktop and mobile shells (v1).",
        lifespan=_lifespan,
    )

    # Dev-only CORS for a locally served frontend (opt-in via env). Write verbs
    # are needed now that the Wave-1 surface accepts POSTs.
    if os.environ.get("AICC_API_DEV") == "1":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173"],
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    # One authentication dependency, mounted on every included router
    # (VOYN-W0-AICC-AUTH-HTTP-01). It resolves the operation for the *matched*
    # route from `http_auth.routing.ROUTE_OPERATIONS` and returns immediately
    # for anything that is not a mutating verb, so no read pays for it. Mounted
    # here rather than repeated at 27 decorators so that coverage is a table
    # `validate_routing` can check against the router tree.
    guard = [Depends(enforce)]
    app.include_router(_build_read_router(), dependencies=guard)
    app.include_router(wave1_routes.router, dependencies=guard)
    app.include_router(conflict_routes.router, dependencies=guard)
    app.include_router(audit_routes.router, dependencies=guard)
    app.include_router(council_routes.router, dependencies=guard)
    app.include_router(marketplace_routes.router, dependencies=guard)
    app.include_router(model_registry_routes.router, dependencies=guard)
    app.include_router(networking_routes.router, dependencies=guard)
    app.include_router(backlog_routes.router, dependencies=guard)

    # Fail closed at boot, not only in CI: a mutating route this build does not
    # route to an operation stops the process here.
    validate_routing(app)
    return app


app = create_app()
