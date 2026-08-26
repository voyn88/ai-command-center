"""Request bodies and thin response wrappers for the Wave-2 conflicts surface.

The *entity* returned on this surface is the shared contract model
:class:`command_center.api.models.Conflict`; the classes here only describe the
**inputs** a client POSTs (open a conflict, assign an owner, record a mitigation)
and the small composite response (a list page) that wraps the entity.

Kept separate from ``models.py`` on purpose: the entity skeleton is the
read/response contract both shells code against; request shapes are an
implementation detail of this backend and evolve independently.
"""

from __future__ import annotations

from pydantic import BaseModel

from command_center.api.models import Conflict, ConflictKind


class ConflictCreate(BaseModel):
    """POST body for opening a conflict. ``kind`` is required (a conflict is
    always classified); ``project_ref`` is optional — when it names a BANK/LEGAL
    project the write is rejected (redaction), and when set it lets the read path
    drop the row so its ``source_ref`` never leaves the surface."""

    kind: ConflictKind
    source_ref: str = ""
    severity: str = "sev3"
    owner: str | None = None
    mitigation: str | None = None
    project_ref: str | None = None


class ConflictAssign(BaseModel):
    """POST body for ``/conflicts/{id}/assign`` — set (or reassign) the owner."""

    owner: str


class ConflictMitigation(BaseModel):
    """POST body for ``/conflicts/{id}/mitigation`` — record the mitigation
    text/plan. Recording a mitigation on an ``open`` conflict also advances it to
    ``mitigating``."""

    mitigation: str


class ConflictList(BaseModel):
    """A page of conflicts plus the paging echo the client sent."""

    conflicts: list[Conflict]
    limit: int
    offset: int
