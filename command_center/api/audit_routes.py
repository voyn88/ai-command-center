"""HTTP routes for the Wave-2 Audit surface.

Controllers only: each handler is a thin adapter that validates its inputs via
FastAPI, delegates to exactly one :mod:`command_center.api.audit_service`
function, and maps a ``None``/domain error onto the right HTTP status. No
business logic, no data access, and no event publishing live here.

Mounted under the versioned ``/api/v1`` prefix (see ``api/app.py``); every path
below is relative to that.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from command_center.api import audit_schemas as a
from command_center.api import audit_service as service
from command_center.api import models

router = APIRouter(prefix="/api/v1", tags=["audit"])

# Shared paging bound for every list endpoint on this surface.
_MAX_LIMIT = 500


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


@router.post("/audit/run", response_model=a.AuditRunResult)
def run_audit(payload: a.AuditRunRequest) -> a.AuditRunResult:
    """Run one audit pass over ``payload.project`` and return the finalized run
    plus its findings. A sensitive (BANK/LEGAL) project is rejected (400); an
    unknown check name is a client error (400)."""
    try:
        return service.run_audit(payload)
    except service.SensitiveProjectRefError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/audit/runs", response_model=a.AuditRunList)
def list_runs(
    project: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> a.AuditRunList:
    return service.list_runs(
        project=project, status=status, limit=limit, offset=offset
    )


@router.get("/audit/runs/{run_id}", response_model=models.AuditRun)
def get_run(run_id: str) -> models.AuditRun:
    found = service.get_run(run_id)
    if found is None:
        raise HTTPException(status_code=404, detail="audit run not found")
    return found


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------


@router.get("/audit/findings", response_model=a.AuditFindingList)
def list_findings(
    run: str | None = Query(default=None),
    status: str | None = None,
    owner: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> a.AuditFindingList:
    """The findings inbox, filterable by ``run``, ``status`` and ``owner`` — the
    two triage axes every finding always carries."""
    return service.list_findings(
        run_id=run, status=status, owner=owner, limit=limit, offset=offset
    )


@router.post("/audit/findings/{finding_id}/status", response_model=models.AuditFinding)
def set_finding_status(finding_id: str, payload: a.FindingStatusUpdate) -> models.AuditFinding:
    try:
        updated = service.set_finding_status(finding_id, payload.status)
    except db_transition_errors() as exc:  # illegal lifecycle edge
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if updated is None:
        raise HTTPException(status_code=404, detail="audit finding not found")
    return updated


@router.post("/audit/findings/{finding_id}/promote", response_model=a.PromoteFindingResponse)
def promote_finding(finding_id: str) -> a.PromoteFindingResponse:
    try:
        result = service.promote_finding(finding_id)
    except service.FindingNotPromotableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="audit finding not found")
    return result


def db_transition_errors() -> tuple[type[Exception], ...]:
    """The repository's finding-status transition error, resolved lazily so the
    controller maps an illegal lifecycle edge (e.g. ``fixed`` → ``ack``) onto a
    409 without importing the repository package at module import time."""
    from command_center.runtime.db.audit import InvalidAuditFindingTransitionError

    return (InvalidAuditFindingTransitionError,)
