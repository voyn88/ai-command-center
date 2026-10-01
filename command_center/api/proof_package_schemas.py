"""Response contract for the VOYN-MIN-WOW-1 proof-package surface.

One composite document per project — there is no request body beyond the
project name in the path, and no list/paging shape, because the acceptance
this surface exists for is "one proof package per pilot client", not a
collection.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class MemoryEvent(BaseModel):
    """One entry in the Digital Memory timeline: a council journal row or a
    proposal lifecycle event, normalized to a common shape."""

    source: str
    ref_id: str
    ref_title: str = ""
    event_type: str
    actor: str | None = None
    message: str | None = None
    created_at: str


class CounterfactualEntry(BaseModel):
    """One path the Board considered and did not take: a withdrawn motion or
    a rejected decision, with the rationale on record."""

    kind: str
    ref_id: str
    title: str = ""
    rationale: str = ""
    tally: dict[str, int] = Field(default_factory=dict)
    at: str | None = None


class DecisionImpact(BaseModel):
    """One decision that recorded an estimated financial/time impact."""

    ref_id: str
    title: str = ""
    outcome: str
    impact: dict[str, Any] = Field(default_factory=dict)
    decided_at: str | None = None


class EvidenceItem(BaseModel):
    """One immutable evidence row backing a proposal raised for the project."""

    proposal_id: str
    proposal_title: str = ""
    seq: int
    kind: str
    source: str
    summary: str | None = None
    is_blocker: bool = False
    data: dict[str, Any] | None = None
    observed_at: str


class ProofPackage(BaseModel):
    """The assembled, hash-stamped proof package for one project."""

    project: str
    generated_at: str
    digital_memory: list[MemoryEvent] = Field(default_factory=list)
    counterfactual: list[CounterfactualEntry] = Field(default_factory=list)
    decision_pnl: list[DecisionImpact] = Field(default_factory=list)
    audit_vault: list[EvidenceItem] = Field(default_factory=list)
    integrity_hash: str
