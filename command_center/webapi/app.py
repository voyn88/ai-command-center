"""FastAPI app factory for the AI Command Center web dashboard's backend.

`GET /api/home` and `GET /api/execution` are read-only and build the
Workspace Home read model (`workspace_home.build_workspace_home_snapshot`,
already redacted for BANK/LEGAL at the run/report/artifact/activity level)
and map it onto frontend DTOs via pure serializers (which
additionally closes the one redaction gap that function leaves open — see
`serializers.py`'s module docstring). They mutate nothing.

The app also mounts the health probes and the two mutating dispatch routes
(`POST /api/v1/dispatch/assign`, `PUT /api/v1/dispatch/policy`), which are
authenticated and authorized through `command_center.http_auth`.

`build_workspace_home_snapshot` and `ExecutionCenterAPI` are imported as
plain module-level names and referenced unqualified inside the route
handler, specifically so a test can `monkeypatch.setattr(this_module, name,
fake)` and have the handler pick up the fake at call time — see
`tests/webapi/test_endpoints.py`. `ExecutionCenterAPI()` is constructed
lazily on the *first* request and then cached on `app.state` for reuse,
never at import time and never at `create_app()` time, because its
constructor has a real side effect: `Supervisor.__init__` runs
`db.migrate(...)` against the real `runtime.db`. Import-time or
app-factory-time construction would make every test that merely imports this
module, or calls `create_app()`, touch the real database; per-request
construction would re-run `db.migrate(...)` on every poll. Caching on the
per-app `app.state` (a fresh app per `create_app()`) keeps both properties:
each test gets its own empty cache and patches the module globals before its
first request, so the fake — never the real class — is what gets cached.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from command_center.dispatch.api import create_dispatch_router
from command_center.http_auth.routing import enforce, validate_routing
from command_center.webapi.queue_routes import create_queue_router
from command_center.runtime.api import ExecutionCenterAPI
from command_center.webapi.serializers import serialize_execution, serialize_home
from command_center.workspace_home import build_workspace_home_snapshot

# Repo root is three levels up from this file: <root>/command_center/webapi/app.py
_REPO_ROOT = Path(__file__).resolve().parents[2]
_WEB_DIST = _REPO_ROOT / "web" / "dist"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Open the PostgreSQL pool for the life of the process, when configured.

    Without this the pool is only ever opened by `python -m command_center.db`,
    so `/readyz` in a served process would report the database unreachable
    forever and no replica would enter rotation.

    Absence of `AICC_PG_HOST` means "this deployment has no server database"
    (desktop, CLI, the test suite), not a misconfiguration: startup proceeds and
    `/readyz` reports degraded. A host that *is* set but unusable is fatal here
    on purpose — a bad DSN should stop the deploy, not surface as a 503 storm.
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
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )

    if os.environ.get("AICC_WEB_DEV") == "1":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173"],
            allow_methods=["GET"],
            allow_headers=["*"],
        )

    @app.get("/api/home")
    def home() -> dict:  # read-only, no mutation
        # Build the ExecutionCenterAPI once per app and reuse it: its
        # constructor runs db.migrate(...) against runtime.db, so doing it
        # per request would re-migrate on every poll. Resolved as a module
        # global on first use so the test monkeypatch seam holds (see the
        # module docstring).
        api = getattr(app.state, "execution_center_api", None)
        if api is None:
            api = ExecutionCenterAPI()
            app.state.execution_center_api = api
        snapshot = build_workspace_home_snapshot(execution_center_api=api)
        return serialize_home(snapshot)

    @app.get("/api/execution")
    def execution() -> dict:  # read-only, no mutation
        api = getattr(app.state, "execution_center_api", None)
        if api is None:
            api = ExecutionCenterAPI()
            app.state.execution_center_api = api
        snapshot = build_workspace_home_snapshot(execution_center_api=api)
        return serialize_execution(snapshot)

    # Liveness and readiness probes (VOYN-W0-AICC-SRV-01a). Registered before
    # the SPA mount so they are not shadowed by the catch-all static handler,
    # and outside `/api` because orchestrators and load balancers expect them
    # at the root. `command_center.db.health` is imported inside the handlers:
    # it reaches `psycopg` only when a probe actually runs, so the desktop and
    # CLI entry points — which have no PostgreSQL client installed — can still
    # import this module.
    @app.get("/healthz")
    def healthz() -> dict:
        """Is the process alive? Never touches the database (see health.py)."""
        from command_center.db.health import check_liveness

        return check_liveness().to_dict()

    @app.get("/readyz")
    def readyz(response: Response) -> dict:
        """Should this process receive traffic? 503 when the database is not usable."""
        from command_center.db.health import check_readiness

        report = check_readiness()
        response.status_code = 200 if report.ok else 503
        return report.to_dict()

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        """Prometheus scrape surface; contains no payloads or credentials."""
        from command_center.observability import CONTENT_TYPE, render_control_metrics

        return Response(render_control_metrics(), media_type=CONTENT_TYPE)

    # Agent-dispatch policy layer (VOYN-W2-AGENT): `/api/v1/dispatch/*`.
    # Registered before the SPA mount so its routes resolve ahead of the
    # catch-all static handler.
    # Its two write routes are authenticated and authorized through
    # `command_center.http_auth` (VOYN-W0-AICC-AUTH-HTTP-01); `enforce` is
    # mounted here rather than on the individual routes so `validate_routing`
    # below can prove the coverage against the router tree instead of trusting
    # that every future route remembers a decorator. `/api/home`,
    # `/api/execution`, `/healthz` and `/readyz` are registered directly on the
    # app and stay unauthenticated reads (VOYN-W0-AICC-AUTH-HTTP-02).
    app.include_router(create_dispatch_router(), dependencies=[Depends(enforce)])

    # Server work-queue status + audit enqueue (VOYN-W0-APP-CONTROL-S1/S4):
    # `/api/v1/queue/*`, before the SPA mount. The POST is a mutating route
    # and therefore rides the same `enforce` table row
    # (`queue:audit:enqueue`); the two GETs additionally authenticate inside
    # the router (they expose run transcripts — see queue_routes.py for the
    # recorded read-auth decision under VOYN-W0-AICC-AUTH-HTTP-02).
    app.include_router(create_queue_router(), dependencies=[Depends(enforce)])

    # Fail closed at boot: an unrouted mutating route stops the process here,
    # in the environment that matters, not only in a CI report.
    validate_routing(app)

    # Serve the built SPA (built via `web/`'s `npm run build`) from the same
    # origin as the API, so production needs no CORS configuration. Mounted
    # AFTER the `/api` routes so `/api/home` resolves before the catch-all
    # static handler. Resolved relative to this file's location (not the
    # process CWD) so it works regardless of where the server is launched
    # from. Absent in dev (before a build, or under test), in which case the
    # API-only app above is served as-is.
    if _WEB_DIST.is_dir():
        app.mount("/", StaticFiles(directory=_WEB_DIST, html=True), name="spa")

    return app
