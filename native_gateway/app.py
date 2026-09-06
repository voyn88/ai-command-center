"""AICC Native Gateway v1 — the HTTP surface.

Read-only by construction: only GET routes are registered, and the only
scope v1 issues is ``read``.  Every request passes, in order: bearer-token
authentication → scope check → client-version negotiation → rate limit.
Every response passes the redaction boundary (`native_gateway.redaction`)
before its bytes leave the process.

Transport security is enforced one layer down: `native_gateway.serve` refuses
to bind without TLS, so the app never listens on plaintext outside tests.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import GATEWAY_VERSION, SCHEMA_VERSION
from .auth import SCOPE_READ, Device, DeviceRegistry, bearer_token
from .config import GatewaySettings
from .dto import Freshness
from .errors import GatewayError, error_response, new_trace_id
from .pagination import decode_cursor, encode_cursor
from .ratelimit import RateLimiter
from .redaction import assert_body_safe, sanitize_tree
from .source import FileProjectionSource, Projection

SUPPORTED_CLIENT_MAJOR = "1"


@dataclass
class GatewayRuntime:
    settings: GatewaySettings
    source: FileProjectionSource
    registry: DeviceRegistry
    limiter: RateLimiter


def _runtime(request: Request) -> GatewayRuntime:
    return request.app.state.runtime


def _authenticate(request: Request) -> Device:
    runtime = _runtime(request)
    token = bearer_token(request.headers.get("Authorization"))
    if token is None:
        raise GatewayError(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
    device = runtime.registry.authenticate(token)
    if device is None:
        raise GatewayError(401, "unauthorized", {"WWW-Authenticate": "Bearer"})
    if device.scope != SCOPE_READ:
        raise GatewayError(403, "forbidden")
    return device


def _negotiate(request: Request) -> None:
    accept = request.headers.get("Accept")
    if accept and "application/json" not in accept and "*/*" not in accept:
        raise GatewayError(422, "unsupported_accept")
    version = request.headers.get("X-AICC-Client-Version")
    if not version:
        raise GatewayError(422, "client_version_required")
    major = version.strip().split(".", 1)[0]
    if major != SUPPORTED_CLIENT_MAJOR:
        raise GatewayError(422, "unsupported_client_version")


def _guard(request: Request) -> Device:
    """The per-request gate shared by every /v1 route."""
    request.state.trace_id = new_trace_id()
    device = _authenticate(request)
    _negotiate(request)
    retry_after = _runtime(request).limiter.check(device.device_id)
    if retry_after is not None:
        raise GatewayError(429, "rate_limited", {"Retry-After": str(retry_after)})
    return device


# Module-level singleton for the shared per-request gate (ruff B008).
GUARD = Depends(_guard)


def _safe_json(
    payload: dict, status_code: int = 200, headers: dict | None = None
) -> Response:
    """Serialize through the redaction boundary; fail closed on any residue."""
    sanitized = sanitize_tree(payload)
    body = json.dumps(sanitized, separators=(",", ":"), ensure_ascii=False)
    assert_body_safe(body)  # RedactionViolation → opaque 500 via handler
    return Response(
        content=body,
        status_code=status_code,
        media_type="application/json",
        headers=headers or {},
    )


def _etag(revision: str) -> str:
    return f'"{revision}"'


def _if_none_match_hit(header: str | None, revision: str) -> bool:
    if not header:
        return False
    candidates = {
        v.strip().strip('"').removeprefix("W/").strip('"') for v in header.split(",")
    }
    return revision in candidates


def _collection_page(
    projection: Projection,
    collection: str,
    items: list[dict],
    cursor: str | None,
    limit: int,
    settings: GatewaySettings,
    revision_bound: bool,
) -> dict:
    offset = 0
    if cursor:
        decoded = decode_cursor(cursor, collection)
        if decoded is None:
            raise GatewayError(422, "validation_failed")
        offset, minted_revision = decoded
        if revision_bound and minted_revision != projection.snapshot.revision:
            raise GatewayError(409, "resync_required")
    limit = max(1, min(limit, settings.max_page_size))
    page = items[offset : offset + limit]
    next_cursor = None
    if offset + limit < len(items):
        next_cursor = encode_cursor(
            collection,
            offset + limit,
            projection.snapshot.revision if revision_bound else "",
        )
    return {
        "schemaVersion": SCHEMA_VERSION,
        "revision": projection.snapshot.revision,
        "generatedAt": projection.snapshot.generatedAt,
        "freshness": projection.snapshot.freshness.value,
        "items": page,
        "page": {"nextCursor": next_cursor},
    }


def create_app(runtime: GatewayRuntime) -> FastAPI:
    app = FastAPI(
        title="AICC Native Gateway",
        version=GATEWAY_VERSION,
        description=(
            "Read-only, redacted projection of AIOS state for native clients. "
            "AIOS remains the owner of tasks, decisions, queues, access and "
            "evidence; this API serves DTO schema 1.0 only."
        ),
        docs_url=None,
        redoc_url=None,
    )
    app.state.runtime = runtime

    @app.exception_handler(GatewayError)
    async def _gateway_error(request: Request, exc: GatewayError) -> JSONResponse:
        trace = getattr(request.state, "trace_id", None)
        return error_response(exc.status_code, exc.code, trace, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            422, "validation_failed", getattr(request.state, "trace_id", None)
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        if exc.status_code == 404:
            code = "not_found"
        elif exc.status_code == 405:
            # Write methods do not exist in v1; commands are contract-only.
            code = "validation_failed"
        else:
            code = "internal"
        return error_response(
            exc.status_code, code, getattr(request.state, "trace_id", None)
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        # Includes RedactionViolation: fail closed, leak nothing.
        return error_response(500, "internal", getattr(request.state, "trace_id", None))

    @app.get("/v1/snapshot")
    def snapshot(request: Request, _: Device = GUARD) -> Response:
        projection = runtime.source.load()
        snap = projection.snapshot
        etag = _etag(snap.revision)
        headers = {
            "ETag": etag,
            "Cache-Control": "no-store",
            "X-Trace-Id": request.state.trace_id,
        }
        # 304 only while fresh: once freshness decays (stale/offline/degraded)
        # the client must receive the new freshness even at the same revision.
        if snap.freshness is Freshness.fresh and _if_none_match_hit(
            request.headers.get("If-None-Match"), snap.revision
        ):
            return Response(status_code=304, headers=headers)
        return _safe_json(snap.model_dump(mode="json"), headers=headers)

    @app.get("/v1/tasks")
    def tasks(
        request: Request,
        cursor: str | None = None,
        limit: int | None = None,
        _: Device = GUARD,
    ) -> Response:
        projection = runtime.source.load()
        payload = _collection_page(
            projection,
            "tasks",
            [t.model_dump(mode="json") for t in projection.snapshot.tasks],
            cursor,
            limit or runtime.settings.default_page_size,
            runtime.settings,
            revision_bound=False,
        )
        return _safe_json(payload)

    @app.get("/v1/tasks/{task_id}")
    def task_detail(request: Request, task_id: str, _: Device = GUARD) -> Response:
        projection = runtime.source.load()
        for task in projection.snapshot.tasks:
            if task.id == task_id:
                return _safe_json(
                    {
                        "schemaVersion": SCHEMA_VERSION,
                        "revision": projection.snapshot.revision,
                        "freshness": projection.snapshot.freshness.value,
                        "task": task.model_dump(mode="json"),
                    }
                )
        raise GatewayError(404, "not_found")

    @app.get("/v1/dialogs")
    def dialogs(
        request: Request,
        cursor: str | None = None,
        limit: int | None = None,
        _: Device = GUARD,
    ) -> Response:
        projection = runtime.source.load()
        payload = _collection_page(
            projection,
            "dialogs",
            [d.model_dump(mode="json") for d in projection.dialogs],
            cursor,
            limit or runtime.settings.default_page_size,
            runtime.settings,
            revision_bound=False,
        )
        return _safe_json(payload)

    @app.get("/v1/decisions")
    def decisions(
        request: Request,
        cursor: str | None = None,
        limit: int | None = None,
        _: Device = GUARD,
    ) -> Response:
        projection = runtime.source.load()
        payload = _collection_page(
            projection,
            "decisions",
            [d.model_dump(mode="json") for d in projection.decisions],
            cursor,
            limit or runtime.settings.default_page_size,
            runtime.settings,
            revision_bound=False,
        )
        return _safe_json(payload)

    @app.get("/v1/events")
    def events(
        request: Request,
        after_cursor: str | None = None,
        limit: int | None = None,
        _: Device = GUARD,
    ) -> Response:
        projection = runtime.source.load()
        payload = _collection_page(
            projection,
            "events",
            [e.model_dump(mode="json") for e in projection.snapshot.events],
            after_cursor,
            limit or runtime.settings.default_page_size,
            runtime.settings,
            revision_bound=True,
        )
        return _safe_json(payload)

    return app


def build_default_app() -> FastAPI:
    """Entry point for `native_gateway.serve` — settings from environment."""
    settings = GatewaySettings.from_env()
    runtime = GatewayRuntime(
        settings=settings,
        source=FileProjectionSource(settings),
        registry=DeviceRegistry(settings.token_file),
        limiter=RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_s),
    )
    return create_app(runtime)
