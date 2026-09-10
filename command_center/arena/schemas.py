"""Request/response contracts for the Арена API surface.

Kept in the arena package (not the shared ``api`` schemas) because they
describe *this engine's* trigger endpoint, not the entity contract the shells
code against — those stay in ``command_center.api.models``.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class SolutionVariantIn(BaseModel):
    """One contestant's attempt at the case, submitted for scoring."""

    agent_id: str
    output: str
    rationale: str
    correct: bool
    quality: float
    explainability: float
    duration_seconds: float
    cost_usd: float


class DuelRunRequest(BaseModel):
    """POST body for ``/arena/duel``. At least three ``variants`` are
    required — a duel is a comparison, not a solo run."""

    case_id: str
    case_prompt: str
    variants: list[SolutionVariantIn] = Field(min_length=1)


class RankedVariantOut(BaseModel):
    """One variant's place in the final ranking, with its score breakdown
    and the per-variant rationale explaining how that rank was reached."""

    rank: int
    agent_id: str
    correct: bool
    correctness: float
    quality: float
    explainability: float
    time_score: float
    cost_score: float
    composite: float
    rationale: str


class DuelRunResponse(BaseModel):
    """Outcome of one duel: every variant ranked best-to-worst, the winner,
    and an overall rationale explaining the margin of victory."""

    case_id: str
    winner_agent_id: str
    rationale: str
    ranking: list[RankedVariantOut]
