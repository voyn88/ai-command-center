"""Runs the hard-bench-set against a set of agents (VOYN-AGT-HARD-BENCH).

Actually putting a case in front of a live agent (claude/codex/etc., see
`runtime.providers`) and judging whether the transcript satisfied the
rubric is a separate, follow-on integration — it needs a real provider call
and a grading strategy (rubric-LLM-judge, human review, or replay-fixture),
none of which belong in the scoring/ranking core. `Grader` is the seam: this
module only guarantees every case reaches every agent, in a fixed order, and
that each result actually answers the case it was asked to grade.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from command_center.bench.cases import CASES
from command_center.bench.types import BenchCase, CaseResult


class Grader(Protocol):
    """Produces a graded `CaseResult` for one (case, agent) pair.

    Implementations are free to do whatever grading requires (invoke the
    agent, replay a recorded transcript, ask a human) — `run_suite` only
    requires the returned result to be honest about which case/agent it
    graded.
    """

    def __call__(self, case: BenchCase, agent_id: str) -> CaseResult: ...


class GraderMismatchError(ValueError):
    """A grader returned a result for a different case/agent than asked."""


def run_suite(
    agent_ids: Sequence[str],
    grader: Grader,
    *,
    cases: Sequence[BenchCase] = CASES,
) -> dict[str, list[CaseResult]]:
    """Run every case in `cases` against every agent in `agent_ids`.

    Returns results keyed by agent id, in case order — every agent is graded
    against the same fixed set, so a difference in the leaderboard reflects
    agent performance, not which cases happened to run for whom.
    """
    if not agent_ids:
        raise ValueError("run_suite requires at least one agent id")
    if not cases:
        raise ValueError("run_suite requires at least one case")

    results: dict[str, list[CaseResult]] = {}
    for agent_id in agent_ids:
        agent_results: list[CaseResult] = []
        for case in cases:
            result = grader(case, agent_id)
            if result.case_id != case.id or result.agent_id != agent_id:
                raise GraderMismatchError(
                    f"grader was asked for case={case.id!r} agent={agent_id!r} "
                    f"but returned case={result.case_id!r} agent={result.agent_id!r}"
                )
            agent_results.append(result)
        results[agent_id] = agent_results
    return results
