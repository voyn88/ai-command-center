"""Value objects for the Арена (agent-duel) engine.

A duel pits multiple agents' independent attempts at the **same case** against
one another. ``Case`` is the shared problem statement; ``SolutionVariant`` is
one contestant's submitted attempt, carrying both its own output and the raw
measurements (correctness, quality, explainability, time, cost) the scorer
needs to compare it against the others. Nothing here touches storage or the
network — these are plain, immutable dataclasses so the scorer and the service
can both be exercised in isolation with hand-built inputs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Case:
    """The shared problem statement every contestant in a duel answers."""

    id: str
    prompt: str


@dataclass(frozen=True, slots=True)
class SolutionVariant:
    """One agent's attempt at a :class:`Case`.

    ``correct`` is a pass/fail verdict (from a grader, a test suite, or a
    human) — the one axis that is not a matter of degree. ``quality`` and
    ``explainability`` are ``0.0..1.0`` judgments already normalized by
    whatever produced them (an LLM-as-judge, a rubric, a reviewer); the scorer
    does not attempt to derive them from ``output``/``rationale`` itself.
    ``duration_seconds`` and ``cost_usd`` are the raw wall-clock time and spend
    the attempt took — the scorer normalizes these *relative to the other
    variants in the same duel*, since "fast" and "cheap" only mean something
    in comparison to the field.
    """

    agent_id: str
    output: str
    rationale: str
    correct: bool
    quality: float
    explainability: float
    duration_seconds: float
    cost_usd: float
