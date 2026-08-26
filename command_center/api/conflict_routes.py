"""HTTP routes for the Wave-2 conflicts surface.

Controllers only: each handler is a thin adapter that validates its inputs via
FastAPI, delegates to exactly one :mod:`command_center.conflicts.service`
function, and maps a ``None``/domain error onto the right HTTP status. No
business logic, no data access and no event handling live here.

Mounted under the versioned ``/api/v1`` prefix (see ``api/app.py``); every path
below is relative to that.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from command_center.api import conflict_schemas as w
from command_center.api import models
from command_center.conflicts import service

router = APIRouter(prefix="/api/v1", tags=["conflicts"])

# Shared paging bound for the list endpoint on this surface.
_MAX_LIMIT = 500


@router.get("/conflicts", response_model=w.ConflictList)
def list_conflicts(
    kind: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> w.ConflictList:
    return service.list_conflicts(
        kind=kind, status=status, owner=owner, limit=limit, offset=offset
    )


@router.get("/conflicts/{conflict_id}", response_model=models.Conflict)
def get_conflict(conflict_id: str) -> models.Conflict:
    found = service.get_conflict(conflict_id)
    if found is None:
        raise HTTPException(status_code=404, detail="conflict not found")
    return found


@router.post("/conflicts", response_model=models.Conflict, status_code=201)
def create_conflict(payload: w.ConflictCreate) -> models.Conflict:
    try:
        return service.create_conflict(payload)
    except service.SensitiveProjectRefError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        # Bad kind/severity — a client error, refused before it reaches SQL.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/conflicts/{conflict_id}/assign", response_model=models.Conflict)
def assign_conflict(conflict_id: str, payload: w.ConflictAssign) -> models.Conflict:
    updated = service.assign_conflict(conflict_id, payload.owner)
    if updated is None:
        raise HTTPException(status_code=404, detail="conflict not found")
    return updated


@router.post("/conflicts/{conflict_id}/mitigation", response_model=models.Conflict)
def set_mitigation(
    conflict_id: str, payload: w.ConflictMitigation
) -> models.Conflict:
    updated = service.set_mitigation(conflict_id, payload.mitigation)
    if updated is None:
        raise HTTPException(status_code=404, detail="conflict not found")
    return updated


@router.post("/conflicts/{conflict_id}/resolve", response_model=models.Conflict)
def resolve_conflict(conflict_id: str) -> models.Conflict:
    try:
        resolved = service.resolve_conflict(conflict_id)
    except service.ConflictNotResolvableError as exc:
        # Missing owner and/or mitigation — the conflict cannot be resolved in
        # its current state.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if resolved is None:
        raise HTTPException(status_code=404, detail="conflict not found")
    return resolved
