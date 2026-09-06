"""Request bodies and response wrappers for the skill-acquisition surface.

The *entities* returned here are the shared contract models in
:mod:`command_center.api.models` (``SkillSource``, ``SkillItem``,
``SkillAcquisitionLogEntry``, ``SkillOutcome``, ``SkillEffect``) — these
classes describe the **inputs** a client POSTs and the small composite
responses (list pages) that wrap those entities. Kept separate from
``models.py`` for the same reason the other Wave-3 surfaces are: the entity
skeletons are the read/response contract both shells code against; request
shapes are an implementation detail of this backend and evolve independently.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from command_center.api.models import (
    SkillAcquisitionLogEntry,
    SkillItem,
    SkillItemKind,
    SkillOutcome,
    SkillSource,
    SkillSourceKind,
)


class ProposeSourceRequest(BaseModel):
    """POST body for ``/skills/sources``. ``kind`` and ``origin`` are required
    — a proposed source is always classified and points somewhere concrete. A
    freshly proposed source is always ``proposed``, never auto-approved."""

    kind: SkillSourceKind
    origin: str
    actor: str | None = None


class SourceTransitionRequest(BaseModel):
    """POST body for ``/skills/sources/{id}/approve`` and ``/revoke``.
    ``expected_version`` is the compare-and-set guard against a concurrent
    writer; ``actor`` is required — the human gate on a new source's first
    connection must be attributable to who threw it."""

    expected_version: int
    actor: str


class RegisterSkillRequest(BaseModel):
    """POST body for ``/skills/items``. ``content_hash`` is required — a skill
    is always pinned to exactly what will be acquired, never to "latest"."""

    source_id: str
    name: str
    kind: SkillItemKind
    content_hash: str
    version: str = ""
    task_class: str = ""
    provenance: str = ""


class AcquireSkillRequest(BaseModel):
    """POST body for ``/skills/items/{id}/acquire``."""

    expected_version: int
    actor: str


class ItemTransitionRequest(BaseModel):
    """POST body for ``/skills/items/{id}/reject`` and ``/revoke``."""

    expected_version: int
    actor: str
    detail: str = ""


class RecordOutcomeRequest(BaseModel):
    """POST body for ``/skills/items/{id}/outcomes``: one per-task evidence
    row feeding the effect measurement. ``used`` distinguishes a with-skill
    row from a ``used=False`` baseline row for the same task class."""

    task_id: str
    used: bool
    cost_usd: float
    accepted: bool
    latency_seconds: float = 0.0
    detail: str = ""


class SkillSourceList(BaseModel):
    """A page of sources plus the paging echo the client sent."""

    sources: list[SkillSource] = Field(default_factory=list)
    limit: int
    offset: int


class SkillItemList(BaseModel):
    """A page of skill items plus the paging echo the client sent."""

    items: list[SkillItem] = Field(default_factory=list)
    limit: int
    offset: int


class SkillAcquisitionLog(BaseModel):
    """The append-only lifecycle history for one skill, oldest first."""

    skill_id: str
    entries: list[SkillAcquisitionLogEntry] = Field(default_factory=list)
    limit: int
    offset: int


class SkillOutcomeList(BaseModel):
    """A page of raw per-task evidence for one skill, newest first."""

    skill_id: str
    outcomes: list[SkillOutcome] = Field(default_factory=list)
    limit: int
    offset: int
