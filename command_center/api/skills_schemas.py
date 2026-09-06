"""Request bodies and thin response wrappers for the Skill Acquisition
surface (VOYN-W0-AICC-SKILL-ACQUISITION).

Kept separate from ``models.py`` on the same principle the marketplace
surface follows: the entities returned here (``SkillSource``, ``SkillItem``,
``SkillAcquisitionLogEntry``, ``SkillOutcome``, ``SkillEffectReport``) are the
shared read/response contract; the classes in this module only describe the
**inputs** a client POSTs and the small composite responses (list pages) that
wrap those entities.
"""

from __future__ import annotations

from pydantic import BaseModel

from command_center.api.models import (
    SkillAcquisitionLogEntry,
    SkillItem,
    SkillItemKind,
    SkillOutcome,
    SkillOutcomePhase,
    SkillSource,
    SkillSourceKind,
)


class SkillSourceCreate(BaseModel):
    """POST body for proposing a new allowlist origin. Always starts
    ``proposed`` — a source is approved only through a separate, explicit
    call naming the approving actor (the human gate)."""

    name: str
    kind: SkillSourceKind
    origin: str
    proposed_by: str


class SkillSourceActorRequest(BaseModel):
    """POST body for ``/skills/sources/{id}/approve`` and
    ``/skills/sources/{id}/revoke`` — ``actor`` is required so both the
    approval and the revocation of a source are attributed."""

    actor: str
    reason: str = ""


class SkillSourceList(BaseModel):
    items: list[SkillSource]
    limit: int
    offset: int


class SkillItemCreate(BaseModel):
    """POST body for directly registering a skill candidate. ``version`` and
    ``content_hash`` are the pin; ``source_id`` must already resolve to an
    ``approved`` :class:`~command_center.api.models.SkillSource` — the
    persistence boundary refuses anything else."""

    name: str
    kind: SkillItemKind
    version: str
    content_hash: str
    source_id: str
    provenance: str = ""
    task_class: str = ""


class SkillActorRequest(BaseModel):
    """POST body for ``/skills/items/{id}/acquire``,
    ``/skills/items/{id}/reject`` and ``/skills/items/{id}/revoke`` —
    ``actor`` is required so every lifecycle action is attributed."""

    actor: str
    reason: str = ""


class SkillItemList(BaseModel):
    items: list[SkillItem]
    limit: int
    offset: int


class SkillAcquisitionLog(BaseModel):
    skill_id: str
    entries: list[SkillAcquisitionLogEntry]
    limit: int
    offset: int


class SkillOutcomeCreate(BaseModel):
    """POST body for recording one raw per-task sample behind the effect
    measurement."""

    task_id: str
    phase: SkillOutcomePhase
    cost: float
    accepted: bool
    first_pass: bool


class SkillOutcomeList(BaseModel):
    skill_id: str
    items: list[SkillOutcome]
    limit: int
    offset: int
