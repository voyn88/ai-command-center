"""HTTP routes for the Counterfactual Ledger surface.

Controllers only: each handler is a thin adapter that validates its inputs via
FastAPI, delegates to exactly one
:mod:`command_center.counterfactual_ledger.service` function, and maps a
``None``/domain error onto the right HTTP status. No business logic, no data
access and no event handling live here.

Mounted under the versioned ``/api/v1`` prefix (see ``api/app.py``); every path
below is relative to that.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from command_center.api import counterfactual_ledger_schemas as w
from command_center.api import models
from command_center.counterfactual_ledger import service
from command_center.runtime.db.counterfactual_ledger import (
    CounterfactualDecisionFinalizedError,
    InvalidCounterfactualDecisionTransitionError,
)

router = APIRouter(prefix="/api/v1", tags=["counterfactual-ledger"])

# Shared paging bound for the list endpoint on this surface.
_MAX_LIMIT = 500


@router.get("/decisions", response_model=w.DecisionList)
def list_decisions(
    criticality: str | None = None,
    status: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> w.DecisionList:
    return service.list_decisions(
        criticality=criticality, status=status, limit=limit, offset=offset
    )


@router.get("/decisions/{decision_id}", response_model=models.CounterfactualDecision)
def get_decision(decision_id: str) -> models.CounterfactualDecision:
    found = service.get_decision(decision_id)
    if found is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return found


@router.post("/decisions", response_model=models.CounterfactualDecision, status_code=201)
def create_decision(payload: w.DecisionCreate) -> models.CounterfactualDecision:
    try:
        return service.create_decision(payload)
    except service.SensitiveProjectRefError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        # Bad title/criticality — a client error, refused before it reaches SQL.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/decisions/{decision_id}/alternatives", response_model=w.AlternativeList)
def list_alternatives(decision_id: str) -> w.AlternativeList:
    found = service.list_alternatives(decision_id)
    if found is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return found


@router.post(
    "/decisions/{decision_id}/alternatives",
    response_model=models.Alternative,
    status_code=201,
)
def add_alternative(
    decision_id: str, payload: w.AlternativeCreate
) -> models.Alternative:
    try:
        created = service.add_alternative(decision_id, payload)
    except CounterfactualDecisionFinalizedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if created is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return created


@router.post("/decisions/{decision_id}/finalize", response_model=models.CounterfactualDecision)
def finalize_decision(decision_id: str, payload: w.DecisionFinalize) -> models.CounterfactualDecision:
    try:
        finalized = service.finalize_decision(decision_id, payload)
    except service.DecisionNotFinalizableError as exc:
        # Too few alternatives for a critical decision — the decision cannot be
        # finalized in its current state.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except InvalidCounterfactualDecisionTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if finalized is None:
        raise HTTPException(status_code=404, detail="decision not found")
    return finalized
