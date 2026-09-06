"""HTTP routes for the Skill Acquisition surface
(VOYN-W0-AICC-SKILL-ACQUISITION).

Controllers only: each handler is a thin adapter that validates its inputs
via FastAPI, delegates to exactly one :mod:`command_center.skills.service`
function, and maps a ``None``/domain error onto the right HTTP status. No
business logic, no data access and no acquisition work live here.

Mounted under the versioned ``/api/v1`` prefix (see ``api/app.py``); every
path below is relative to that.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from command_center.api import models
from command_center.api import skills_schemas as w
from command_center.skills import service

router = APIRouter(prefix="/api/v1", tags=["skills"])

_MAX_LIMIT = 500


# --- sources (the source allowlist) ---------------------------------------


@router.get("/skills/sources", response_model=w.SkillSourceList)
def list_sources(
    kind: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> w.SkillSourceList:
    return service.list_sources(kind=kind, status=status, limit=limit, offset=offset)


@router.get("/skills/sources/{source_id}", response_model=models.SkillSource)
def get_source(source_id: str) -> models.SkillSource:
    found = service.get_source(source_id)
    if found is None:
        raise HTTPException(status_code=404, detail="skill source not found")
    return found


@router.post("/skills/sources", response_model=models.SkillSource, status_code=201)
def propose_source(payload: w.SkillSourceCreate) -> models.SkillSource:
    try:
        return service.propose_source(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/skills/sources/{source_id}/approve", response_model=models.SkillSource)
def approve_source(source_id: str, payload: w.SkillSourceActorRequest) -> models.SkillSource:
    try:
        return service.approve_source(source_id, actor=payload.actor)
    except service.SkillSourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/skills/sources/{source_id}/revoke", response_model=models.SkillSource)
def revoke_source(source_id: str, payload: w.SkillSourceActorRequest) -> models.SkillSource:
    try:
        return service.revoke_source(source_id, actor=payload.actor, reason=payload.reason)
    except service.SkillSourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- items (the registry) --------------------------------------------------


@router.get("/skills/items", response_model=w.SkillItemList)
def list_items(
    kind: str | None = None,
    status: str | None = None,
    task_class: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> w.SkillItemList:
    return service.list_items(
        kind=kind, status=status, task_class=task_class, limit=limit, offset=offset
    )


@router.get("/skills/items/{item_id}", response_model=models.SkillItem)
def get_item(item_id: str) -> models.SkillItem:
    found = service.get_item(item_id)
    if found is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return found


@router.post("/skills/items", response_model=models.SkillItem, status_code=201)
def register_candidate(payload: w.SkillItemCreate) -> models.SkillItem:
    try:
        return service.register_candidate(payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/skills/items/{item_id}/acquire", response_model=models.SkillItem)
def acquire_skill(item_id: str, payload: w.SkillActorRequest) -> models.SkillItem:
    try:
        return service.acquire_skill(item_id, actor=payload.actor)
    except service.SkillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/skills/items/{item_id}/reject", response_model=models.SkillItem)
def reject_candidate(item_id: str, payload: w.SkillActorRequest) -> models.SkillItem:
    try:
        return service.reject_candidate(item_id, actor=payload.actor, reason=payload.reason)
    except service.SkillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/skills/items/{item_id}/revoke", response_model=models.SkillItem)
def revoke_skill(item_id: str, payload: w.SkillActorRequest) -> models.SkillItem:
    try:
        return service.revoke_skill(item_id, actor=payload.actor, reason=payload.reason)
    except service.SkillNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/skills/items/{item_id}/log", response_model=w.SkillAcquisitionLog)
def get_acquisition_log(
    item_id: str,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> w.SkillAcquisitionLog:
    found = service.get_acquisition_log(item_id, limit=limit, offset=offset)
    if found is None:
        raise HTTPException(status_code=404, detail="skill not found")
    return found


# --- outcomes + effect measurement -----------------------------------------


@router.post(
    "/skills/items/{item_id}/outcomes", response_model=models.SkillOutcome, status_code=201
)
def record_outcome(item_id: str, payload: w.SkillOutcomeCreate) -> models.SkillOutcome:
    if service.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="skill not found")
    try:
        return service.record_outcome(
            item_id,
            task_id=payload.task_id,
            phase=payload.phase,
            cost=payload.cost,
            accepted=payload.accepted,
            first_pass=payload.first_pass,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/skills/items/{item_id}/effect", response_model=models.SkillEffectReport)
def get_effect(item_id: str) -> models.SkillEffectReport:
    if service.get_item(item_id) is None:
        raise HTTPException(status_code=404, detail="skill not found")
    report = service.evaluate_effect(item_id)
    return models.SkillEffectReport(
        skill_id=report.skill_id,
        baseline_samples=report.baseline_samples,
        with_skill_samples=report.with_skill_samples,
        baseline_cost_per_accepted=report.baseline_cost_per_accepted,
        with_skill_cost_per_accepted=report.with_skill_cost_per_accepted,
        baseline_first_pass_rate=report.baseline_first_pass_rate,
        with_skill_first_pass_rate=report.with_skill_first_pass_rate,
        improved=report.improved,
    )
