"""Request bodies and thin response wrappers for the Wave-3 networking surface.

The *entities* returned on this surface are the shared contract models
:class:`command_center.api.models.Contact` / ``Message`` / ``Invitation``; the
classes here only describe the **inputs** a client POSTs and the small composite
responses (list pages, the feedback→task result) that wrap them.

Kept separate from ``models.py`` on purpose: the entity skeletons are the
read/response contract both shells code against; request shapes are an
implementation detail of this backend and evolve independently.
"""

from __future__ import annotations

from pydantic import BaseModel

from command_center.api.models import (
    Contact,
    Invitation,
    Message,
    MessageDirection,
)


class ContactCreate(BaseModel):
    """POST body for adding a networking contact. ``display_name`` is required;
    ``project_ref`` is optional — when it names a BANK/LEGAL project the write is
    rejected (redaction), and when set it lets the read path drop the row so its
    handle never leaves the surface."""

    display_name: str
    handle: str = ""
    org: str | None = None
    note: str | None = None
    project_ref: str | None = None


class ContactList(BaseModel):
    """A page of contacts plus the paging echo the client sent."""

    contacts: list[Contact]
    limit: int
    offset: int


class MessageCreate(BaseModel):
    """POST body for logging a message with a contact. ``contact_id`` is required;
    ``direction`` defaults to ``inbound``. ``project_ref`` is redaction-gated like
    every other write on this surface."""

    contact_id: str
    body: str = ""
    direction: MessageDirection = "inbound"
    project_ref: str | None = None


class MessageList(BaseModel):
    """A page of messages plus the paging echo the client sent."""

    messages: list[Message]
    limit: int
    offset: int


class FeedbackCreate(BaseModel):
    """POST body for the feedback intake. A piece of inbound feedback from a
    ``contact_id`` is captured as a message and turned into an **actionable board
    task** in ``project_ref`` (which must be a real, non-sensitive project).
    ``title`` is the task heading; ``body`` its detail."""

    contact_id: str
    project_ref: str
    title: str
    body: str = ""


class FeedbackResponse(BaseModel):
    """Result of the feedback intake: the captured message and the created task's
    id (the acceptance ref — a submitted feedback always yields an actionable
    task)."""

    message: Message
    task_id: str


class InviteCreate(BaseModel):
    """POST body for inviting a contact to the Council. ``council_ref`` is
    optional — the service mints a stable one when omitted. No external identity
    is resolved here; this only records the invitation + emits the seam event."""

    contact_id: str
    council_ref: str | None = None
    note: str | None = None
    project_ref: str | None = None


class InvitationList(BaseModel):
    """A page of invitations plus the paging echo the client sent."""

    invitations: list[Invitation]
    limit: int
    offset: int
