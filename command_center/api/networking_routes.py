"""HTTP routes for the Wave-3 networking surface.

Controllers only: each handler is a thin adapter that validates its inputs via
FastAPI, delegates to exactly one :mod:`command_center.networking.service`
function, and maps a ``None``/domain error onto the right HTTP status. No
business logic, no data access and no event handling live here.

Mounted under the versioned ``/api/v1`` prefix (see ``api/app.py``); every path
below is relative to that.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from command_center.api import models
from command_center.api import networking_schemas as s
from command_center.networking import service

router = APIRouter(prefix="/api/v1", tags=["networking"])

# Shared paging bound for the list endpoints on this surface.
_MAX_LIMIT = 500


# --- contacts -------------------------------------------------------------


@router.get("/networking/contacts", response_model=s.ContactList)
def list_contacts(
    project: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> s.ContactList:
    return service.list_contacts(project=project, limit=limit, offset=offset)


@router.get("/networking/contacts/{contact_id}", response_model=models.Contact)
def get_contact(contact_id: str) -> models.Contact:
    found = service.get_contact(contact_id)
    if found is None:
        raise HTTPException(status_code=404, detail="contact not found")
    return found


@router.post("/networking/contacts", response_model=models.Contact, status_code=201)
def create_contact(payload: s.ContactCreate) -> models.Contact:
    try:
        return service.create_contact(payload)
    except service.SensitiveProjectRefError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --- messages -------------------------------------------------------------


@router.get("/networking/messages", response_model=s.MessageList)
def list_messages(
    contact_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> s.MessageList:
    return service.list_messages(contact_id=contact_id, limit=limit, offset=offset)


@router.post("/networking/messages", response_model=models.Message, status_code=201)
def create_message(payload: s.MessageCreate) -> models.Message:
    try:
        return service.create_message(payload)
    except service.SensitiveProjectRefError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except service.ContactNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- feedback intake (→ actionable task) ----------------------------------


@router.post("/networking/feedback", response_model=s.FeedbackResponse, status_code=201)
def submit_feedback(payload: s.FeedbackCreate) -> s.FeedbackResponse:
    try:
        return service.submit_feedback(payload)
    except service.SensitiveProjectRefError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except service.UnknownProjectRefError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except service.ContactNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --- Council invitations --------------------------------------------------


@router.get("/networking/invitations", response_model=s.InvitationList)
def list_invitations(
    status: str | None = None,
    contact_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=_MAX_LIMIT),
    offset: int = Query(default=0, ge=0),
) -> s.InvitationList:
    return service.list_invitations(
        status=status, contact_id=contact_id, limit=limit, offset=offset
    )


@router.post("/networking/invite", response_model=models.Invitation, status_code=201)
def invite_contact(payload: s.InviteCreate) -> models.Invitation:
    try:
        return service.invite_contact(payload)
    except service.SensitiveProjectRefError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except service.ContactNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
