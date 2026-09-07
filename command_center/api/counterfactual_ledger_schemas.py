"""Request bodies and thin response wrappers for the Counterfactual Ledger
surface.

The *entities* returned on this surface are the shared contract models
:class:`command_center.api.models.Decision` and
:class:`command_center.api.models.Alternative`; the classes here only describe
the **inputs** a client POSTs (open a decision, record an alternative, finalize
a decision) and the small composite responses (list pages) that wrap them.

Kept separate from ``models.py`` on purpose: the entity skeleton is the
read/response contract both shells code against; request shapes are an
implementation detail of this backend and evolve independently.
"""

from __future__ import annotations

from pydantic import BaseModel

from command_center.api.models import Alternative, Decision, DecisionCriticality


class DecisionCreate(BaseModel):
    """POST body for opening a decision. ``title`` is required; ``criticality``
    defaults to ``normal`` — set it to ``critical`` to gate finalization on the
    minimum-alternatives rule. ``project_ref`` is optional — when it names a
    BANK/LEGAL project the write is rejected (redaction)."""

    title: str
    description: str = ""
    criticality: DecisionCriticality = "normal"
    owner: str | None = None
    project_ref: str | None = None


class AlternativeCreate(BaseModel):
    """POST body for ``/decisions/{id}/alternatives`` — record one path
    considered and not taken, and why."""

    option: str
    rejection_reason: str = ""


class DecisionFinalize(BaseModel):
    """POST body for ``/decisions/{id}/finalize`` — record the path actually
    chosen, and why, next to the alternatives that explain what was not."""

    chosen_option: str
    rationale: str = ""


class DecisionList(BaseModel):
    """A page of decisions plus the paging echo the client sent."""

    decisions: list[Decision]
    limit: int
    offset: int


class AlternativeList(BaseModel):
    """Every alternative recorded against one decision."""

    alternatives: list[Alternative]
