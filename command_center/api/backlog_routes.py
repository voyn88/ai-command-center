"""HTTP routes for the Postgres-backed autonomous delivery backlog.

Read-only by construction: no route here accepts a body or mutates a row,
so none needs an entry in ``http_auth.routing.ROUTE_OPERATIONS`` — the same
deliberate scope as every other GET on this app (VOYN-W0-AICC-AUTH-HTTP-02
tracks closing that gap for reads generally; this surface follows the
existing, already-shipped precedent rather than inventing a second one).

Deployment: this router only serves real data when the process has opened
the Postgres pool (``AICC_PG_HOST`` set — see ``app.py``); a desktop shell
not pointed at a server with that configured gets a 503 from the pool guard,
not a silent empty dashboard.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from command_center.api import backlog_schemas as schemas
from command_center.api import backlog_service as service
from command_center.db.pool import PoolNotOpenError

router = APIRouter(prefix="/api/v1/backlog", tags=["backlog"])

_UNCONFIGURED = (
    "the Postgres-backed backlog is not configured on this server "
    "(AICC_PG_HOST unset) — this deployment has no autonomous delivery "
    "backlog to show"
)


@router.get("/status", response_model=schemas.BacklogStatusCounts)
def status_counts() -> schemas.BacklogStatusCounts:
    try:
        return service.get_status_counts()
    except PoolNotOpenError as exc:
        raise HTTPException(status_code=503, detail=_UNCONFIGURED) from exc


@router.get("/tasks", response_model=schemas.BacklogTaskList)
def tasks(
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=service.MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> schemas.BacklogTaskList:
    try:
        return service.list_tasks(status=status, limit=limit, offset=offset)
    except PoolNotOpenError as exc:
        raise HTTPException(status_code=503, detail=_UNCONFIGURED) from exc


@router.get("/tasks/{task_id}", response_model=schemas.BacklogTaskDetail)
def task_detail(task_id: str) -> schemas.BacklogTaskDetail:
    try:
        found = service.get_task_detail(task_id)
    except PoolNotOpenError as exc:
        raise HTTPException(status_code=503, detail=_UNCONFIGURED) from exc
    if found is None:
        raise HTTPException(status_code=404, detail="backlog task not found")
    return found
