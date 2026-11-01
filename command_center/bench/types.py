"""Value objects for the agent hard-bench-set (VOYN-AGT-HARD-BENCH).

No I/O here — pure dataclasses, validated at construction, so a malformed
case or result fails at the point it is built rather than surfacing as a
silently wrong leaderboard later. This mirrors `command_center.dispatch.models`:
value objects the policy/scoring layers consume, kept hermetically testable.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The five lenses the acceptance criterion names explicitly: critical
#: operational cases, incidents, code, UX, and security. Fixed and closed —
#: a case naming any other category fails validation rather than silently
#: forming a new, unweighted bucket.
CATEGORIES: tuple[str, ...] = ("critical", "incident", "code", "ux", "security")


@dataclass(frozen=True)
class BenchCase:
    """One fixed, versioned case in the hard-bench-set.

    `severity` (1..5) is the within-category weight: a case that fails
    catastrophically (e.g. an agent executing an injected instruction) should
    move the score more than a minor style nit, even within the same
    category.
    """

    id: str
    category: str
    title: str
    prompt: str
    severity: int
    rubric: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("bench case requires a non-empty id")
        if self.category not in CATEGORIES:
            raise ValueError(
                f"case {self.id!r} has unknown category {self.category!r}; "
                f"must be one of {CATEGORIES}"
            )
        if not 1 <= self.severity <= 5:
            raise ValueError(
                f"case {self.id!r} severity must be 1..5, got {self.severity}"
            )
        if not self.rubric:
            raise ValueError(f"case {self.id!r} has no rubric criteria")


@dataclass(frozen=True)
class CaseResult:
    """The graded outcome of one agent attempting one case.

    `evidence` is required and non-empty: this repo's scoring convention
    (see `advisor/scorer.py`, `recommend.py`) is explainable numbers, never an
    opaque score with nothing backing it.
    """

    case_id: str
    agent_id: str
    passed: bool
    score: float
    evidence: str

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case result requires a case_id")
        if not self.agent_id:
            raise ValueError("case result requires an agent_id")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"score must be within 0..1, got {self.score}")
        if not self.evidence:
            raise ValueError(
                f"case result for {self.case_id!r}/{self.agent_id!r} requires evidence"
            )


@dataclass(frozen=True)
class CategoryScore:
    """One agent's aggregated performance within a single category."""

    category: str
    weighted_rate: float
    cases_run: int
    cases_passed: int
    lower_bound: float


@dataclass(frozen=True)
class RankedAgent:
    """One agent's position on the weekly leaderboard.

    `stable_score` is the ranking scale (EWMA-smoothed across weeks);
    `raw_score` is this week's number in isolation, kept alongside it so a
    reader can see how much smoothing moved the figure.
    """

    agent_id: str
    stable_score: float
    raw_score: float
    provisional: bool
    categories: tuple[CategoryScore, ...]
    reasons: tuple[str, ...]
