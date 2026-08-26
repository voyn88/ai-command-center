"""HTTP routes for the Wave-3 Council surface.

Controllers only: each handler is a thin adapter that validates its inputs via
FastAPI, delegates to exactly one :mod:`command_center.council.service` function,
and maps a ``None``/domain error onto the right HTTP status. No business logic,
no data access and no tally/permission logic live here.

Mounted under the versioned ``/api/v1`` prefix (see ``api/app.py``); every path
below is relative to that.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from command_center.api import council_schemas as w
from command_center.api import models
from command_center.council import service
from command_center.council.roles import NotAMemberError, VoterNotPermittedError
from command_center.runtime.db.council import (
    DoubleVoteError,
    MotionNotOpenError,
)

router = APIRouter(prefix="/api/v1", tags=["council"])

# Shared paging bound for every list endpoint on this surface.
_MAX_LIMIT = 500


# --------------------------------------------------------------------------
# Motions
# --------------------------------------------------------------------------


@router.post("/council/motions", response_model=models.Motion, status_code=201)
def create_motion(payload: w.MotionCreate) -> models.Motion:
    try:
        return service.create_motion(payload)
    except service.SensitiveProjectRefError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        # Bad quorum / empty title — a client error, refused before it reaches SQL.
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/council/motions", response_model=w.MotionList)
def list_motions(
    status: str | None = None,
    project: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> w.MotionList:
    return service.list_motions(
        status=status, project=project, limit=limit, offset=offset
    )


@router.get("/council/motions/{motion_id}", response_model=w.MotionDetail)
def get_motion(motion_id: str) -> w.MotionDetail:
    found = service.get_motion_detail(motion_id)
    if found is None:
        raise HTTPException(status_code=404, detail="motion not found")
    return found


@router.post("/council/motions/{motion_id}/vote", response_model=models.Vote, status_code=201)
def cast_vote(motion_id: str, payload: w.VoteCreate) -> models.Vote:
    try:
        vote = service.cast_vote(motion_id, payload)
    except (NotAMemberError, VoterNotPermittedError) as exc:
        # Not a Board member, or a seat not currently permitted to vote.
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except DoubleVoteError as exc:
        # One vote per voter — a second vote is refused.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MotionNotOpenError as exc:
        # The motion is already decided/withdrawn.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if vote is None:
        raise HTTPException(status_code=404, detail="motion not found")
    return vote


@router.post("/council/motions/{motion_id}/close", response_model=w.DecisionRecord)
def close_motion(motion_id: str) -> w.DecisionRecord:
    try:
        record = service.close_motion(motion_id)
    except service.QuorumNotMetError as exc:
        # Not enough votes to decide the motion yet.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MotionNotOpenError as exc:
        # Already decided/withdrawn — a decision is recorded exactly once.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if record is None:
        raise HTTPException(status_code=404, detail="motion not found")
    return record


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------


@router.get("/council/decisions", response_model=w.DecisionList)
def list_decisions(
    outcome: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> w.DecisionList:
    return service.list_decisions(outcome=outcome, limit=limit, offset=offset)
