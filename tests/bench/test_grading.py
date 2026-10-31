"""Tests for command_center.bench.grading."""

from __future__ import annotations

import pytest

from command_center.bench.grading import GraderMismatchError, run_suite
from command_center.bench.types import BenchCase, CaseResult

CASES = (
    BenchCase(
        id="c1", category="code", title="t1", prompt="p1", severity=2, rubric=("r1",)
    ),
    BenchCase(
        id="c2", category="ux", title="t2", prompt="p2", severity=3, rubric=("r2",)
    ),
)


def _always_pass(case: BenchCase, agent_id: str) -> CaseResult:
    return CaseResult(case_id=case.id, agent_id=agent_id, passed=True, score=1.0, evidence="ok")


def test_run_suite_covers_every_case_for_every_agent():
    results = run_suite(["agent-a", "agent-b"], _always_pass, cases=CASES)
    assert set(results) == {"agent-a", "agent-b"}
    for agent_id, agent_results in results.items():
        assert [r.case_id for r in agent_results] == [c.id for c in CASES]
        assert all(r.agent_id == agent_id for r in agent_results)


def test_run_suite_requires_at_least_one_agent():
    with pytest.raises(ValueError):
        run_suite([], _always_pass, cases=CASES)


def test_run_suite_requires_at_least_one_case():
    with pytest.raises(ValueError):
        run_suite(["agent-a"], _always_pass, cases=())


def test_run_suite_rejects_grader_returning_wrong_case():
    def wrong_case(case: BenchCase, agent_id: str) -> CaseResult:
        return CaseResult(case_id="not-this-case", agent_id=agent_id, passed=True, score=1.0, evidence="x")

    with pytest.raises(GraderMismatchError):
        run_suite(["agent-a"], wrong_case, cases=CASES)


def test_run_suite_rejects_grader_returning_wrong_agent():
    def wrong_agent(case: BenchCase, agent_id: str) -> CaseResult:
        return CaseResult(case_id=case.id, agent_id="someone-else", passed=True, score=1.0, evidence="x")

    with pytest.raises(GraderMismatchError):
        run_suite(["agent-a"], wrong_agent, cases=CASES)
