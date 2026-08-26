"""Request/response contracts for the advisor API surface.

Kept in the advisor package (not the shared ``api`` schemas) because they
describe *this engine's* trigger endpoint, not the entity contract the shells
code against — those stay in ``command_center.api.models``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class AdvisorRunRequest(BaseModel):
    """POST body for ``/advisor/run``. Both fields are optional: with an empty
    body the pass runs every registered collector across every project."""

    collectors: list[str] | None = None
    project: str | None = None


class AdvisorProposalOutcome(BaseModel):
    """One proposal produced by a pass and what the engine did with it."""

    proposal_id: str
    kind: str
    title: str
    project_ref: str
    action: str
    priority: float
    promoted_task_id: str | None = None


class AdvisorRunResponse(BaseModel):
    """Summary of a collection pass."""

    collectors: list[str]
    candidates: int
    created: int
    promoted: int
    deduped: int
    skipped_sensitive: int
    by_kind: dict[str, int] = Field(default_factory=dict)
    proposals: list[AdvisorProposalOutcome] = Field(default_factory=list)
