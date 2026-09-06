"""HTTP routes for the skill-acquisition surface.

Controllers only: each handler validates its inputs via FastAPI, delegates to
one :mod:`command_center.skills.service` function, and maps its
``None``/domain-error result onto the right HTTP status. No business logic, no
data access, and no execution of any acquired skill lives here.

Every state-transition endpoint below (``approve``/``revoke`` on a source,
``acquire``/``reject``/``revoke`` on an item) uses the same three-way mapping,
so a caller sees the same status for the same class of failure everywhere on
this surface: a missing id is a 404, an invalid transition or malformed input
is a 422, and a concurrent writer racing the same compare-and-set is a 409.
``kind``/``status``/``task_class`` filters on the list endpoints are typed to
the closed ``Literal`` sets in ``api.models`` (not bare ``str``), so a typo'd
filter is a 422 from FastAPI's own validation rather than a silently empty
page.

Mounted under the versioned ``/api/v1`` prefix (see ``api/app.py``); every
path below is relative to that.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from command_center.api import models
from command_center.api import skills_schemas as s
from command_center.runtime.db import LostUpdateError
from command_center.skills import service

router = APIRouter(prefix="/api/v1", tags=["skills"])

# Shared paging bound for every list endpoint on this surface.
_MAX_LIMIT = 500


def _not_found(exc: Exception) -> HTTPException:
    return HTTPException(status_code=404, detail=str(exc))


def _unprocessable(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc))


def _conflict(exc: Exception) -> HTTPException:
    return HTTPException(status_code=409, detail=str(exc))


# --------------------------------------------------------------------------
# Sources — propose / approve / revoke / get / list
# --------------------------------------------------------------------------


@router.post("/skills/sources", response_model=models.SkillSource, status_code=201)
def propose_source(payload: s.ProposeSourceRequest) -> models.SkillSource:
    """Propose a new allowlisted source. Always ``proposed`` — never
    auto-approved; see ``approve_source`` for the required human gate."""
    try:
        return service.propose_source(payload)
    except ValueError as exc:
        raise _unprocessable(exc) from exc


@router.get("/skills/sources", response_model=s.SkillSourceList)
def list_sources(
    kind: models.SkillSourceKind | None = Query(default=None),
    status: models.SkillSourceStatus | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> s.SkillSourceList:
    return service.list_sources(kind=kind, status=status, limit=limit, offset=offset)


@router.get("/skills/sources/{source_id}", response_model=models.SkillSource)
def get_source(source_id: str) -> models.SkillSource:
    found = service.get_source(source_id)
    if found is None:
        raise HTTPException(status_code=404, detail="skill source not found")
    return found


@router.post("/skills/sources/{source_id}/approve", response_model=models.SkillSource)
def approve_source(source_id: str, payload: s.SourceTransitionRequest) -> models.SkillSource:
    """The human gate: a source may not be pulled from until a human calls
    this. Nothing in the acquisition pipeline may reach this endpoint itself."""
    try:
        return service.approve_source(
            source_id, expected_version=payload.expected_version, actor=payload.actor
        )
    except service.SkillSourceNotFoundError as exc:
        raise _not_found(exc) from exc
    except LostUpdateError as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise _unprocessable(exc) from exc


@router.post("/skills/sources/{source_id}/revoke", response_model=models.SkillSource)
def revoke_source(source_id: str, payload: s.SourceTransitionRequest) -> models.SkillSource:
    try:
        return service.revoke_source(
            source_id, expected_version=payload.expected_version, actor=payload.actor
        )
    except service.SkillSourceNotFoundError as exc:
        raise _not_found(exc) from exc
    except LostUpdateError as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise _unprocessable(exc) from exc


# --------------------------------------------------------------------------
# Items — register / acquire / reject / revoke / get / list
# --------------------------------------------------------------------------


@router.post("/skills/items", response_model=models.SkillItem, status_code=201)
def register_item(payload: s.RegisterSkillRequest) -> models.SkillItem:
    """Register a candidate skill against an approved source. A source that
    does not exist is a 404; one that exists but is not ``approved`` is a
    422 — the allowlist gate, enforced atomically one layer down."""
    try:
        return service.register_candidate(payload)
    except service.SkillSourceNotFoundError as exc:
        raise _not_found(exc) from exc
    except ValueError as exc:
        raise _unprocessable(exc) from exc


@router.get("/skills/items", response_model=s.SkillItemList)
def list_items(
    source_id: str | None = Query(default=None),
    kind: models.SkillItemKind | None = Query(default=None),
    status: models.SkillItemStatus | None = Query(default=None),
    task_class: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> s.SkillItemList:
    return service.list_items(
        source_id=source_id, kind=kind, status=status, task_class=task_class,
        limit=limit, offset=offset,
    )


@router.get("/skills/items/{item_id}", response_model=models.SkillItem)
def get_item(item_id: str) -> models.SkillItem:
    found = service.get_item(item_id)
    if found is None:
        raise HTTPException(status_code=404, detail="skill item not found")
    return found


@router.post("/skills/items/{item_id}/acquire", response_model=models.SkillItem)
def acquire_item(item_id: str, payload: s.AcquireSkillRequest) -> models.SkillItem:
    """Claim the item and materialise it through the (default: no-op, no
    network) injected executor. A failed materialisation reverts the claim to
    ``candidate`` (retryable) and is surfaced as 502, not 500 — the failure is
    in the upstream fetch, not in this request."""
    try:
        return service.acquire_skill(
            item_id, expected_version=payload.expected_version, actor=payload.actor
        )
    except service.SkillNotFoundError as exc:
        raise _not_found(exc) from exc
    except LostUpdateError as exc:
        raise _conflict(exc) from exc
    except service.SkillAcquisitionFailedError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        raise _unprocessable(exc) from exc


@router.post("/skills/items/{item_id}/reject", response_model=models.SkillItem)
def reject_item(item_id: str, payload: s.ItemTransitionRequest) -> models.SkillItem:
    try:
        return service.reject_item(
            item_id, expected_version=payload.expected_version, actor=payload.actor,
            detail=payload.detail,
        )
    except service.SkillNotFoundError as exc:
        raise _not_found(exc) from exc
    except LostUpdateError as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise _unprocessable(exc) from exc


@router.post("/skills/items/{item_id}/revoke", response_model=models.SkillItem)
def revoke_item(item_id: str, payload: s.ItemTransitionRequest) -> models.SkillItem:
    try:
        return service.revoke_item(
            item_id, expected_version=payload.expected_version, actor=payload.actor,
            detail=payload.detail,
        )
    except service.SkillNotFoundError as exc:
        raise _not_found(exc) from exc
    except LostUpdateError as exc:
        raise _conflict(exc) from exc
    except ValueError as exc:
        raise _unprocessable(exc) from exc


@router.get("/skills/items/{item_id}/log", response_model=s.SkillAcquisitionLog)
def get_acquisition_log(
    item_id: str,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> s.SkillAcquisitionLog:
    found = service.get_acquisition_log(item_id, limit=limit, offset=offset)
    if found is None:
        raise HTTPException(status_code=404, detail="skill item not found")
    return found


# --------------------------------------------------------------------------
# Outcomes + effect measurement
# --------------------------------------------------------------------------


@router.post("/skills/items/{item_id}/outcomes", response_model=models.SkillOutcome, status_code=201)
def record_outcome(item_id: str, payload: s.RecordOutcomeRequest) -> models.SkillOutcome:
    try:
        result = service.record_outcome(item_id, payload)
    except ValueError as exc:
        raise _unprocessable(exc) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="skill item not found")
    return result


@router.get("/skills/items/{item_id}/outcomes", response_model=s.SkillOutcomeList)
def list_outcomes(
    item_id: str,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> s.SkillOutcomeList:
    found = service.list_outcomes(item_id, limit=limit, offset=offset)
    if found is None:
        raise HTTPException(status_code=404, detail="skill item not found")
    return found


@router.get("/skills/items/{item_id}/effect", response_model=models.SkillEffect)
def get_effect(item_id: str) -> models.SkillEffect:
    found = service.get_effect(item_id)
    if found is None:
        raise HTTPException(status_code=404, detail="skill item not found")
    return found
