"""Tests for command_center.bench.service."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.bench import history
from command_center.bench.service import BenchService
from command_center.bench.types import BenchCase, CaseResult

CASES = (
    BenchCase(id="c1", category="code", title="t1", prompt="p1", severity=3, rubric=("r1",)),
    BenchCase(id="c2", category="security", title="t2", prompt="p2", severity=5, rubric=("r2",)),
)


def _grader_all_pass(case: BenchCase, agent_id: str) -> CaseResult:
    return CaseResult(case_id=case.id, agent_id=agent_id, passed=True, score=1.0, evidence="ok")


def _grader_all_fail(case: BenchCase, agent_id: str) -> CaseResult:
    return CaseResult(case_id=case.id, agent_id=agent_id, passed=False, score=0.0, evidence="failed")


@pytest.fixture()
def bench_dir(tmp_path: Path) -> Path:
    return tmp_path / "bench"


def test_run_weekly_pass_ranks_agents_and_persists_a_report(bench_dir):
    service = BenchService(bench_dir)

    def grader(case: BenchCase, agent_id: str) -> CaseResult:
        return _grader_all_pass(case, agent_id) if agent_id == "good" else _grader_all_fail(case, agent_id)

    result = service.run_weekly_pass("2026-09-01", ["good", "bad"], grader, cases=CASES)

    assert [a.agent_id for a in result.ranked] == ["good", "bad"]
    assert result.ranked[0].raw_score > result.ranked[1].raw_score
    assert "good" in result.markdown
    assert "bad" in result.markdown

    stored = history.get_report(bench_dir, "2026-09-01")
    assert stored == result.markdown


def test_run_weekly_pass_persists_case_results(bench_dir):
    service = BenchService(bench_dir)
    service.run_weekly_pass("2026-09-01", ["agent-a"], _grader_all_pass, cases=CASES)

    runs = history.list_runs(bench_dir)
    assert len(runs) == 1
    rows = history.list_case_results(bench_dir, runs[0]["id"], "agent-a")
    assert {r["case_id"] for r in rows} == {"c1", "c2"}


def test_run_weekly_pass_rejects_a_second_run_for_the_same_week(bench_dir):
    service = BenchService(bench_dir)
    service.run_weekly_pass("2026-09-01", ["agent-a"], _grader_all_pass, cases=CASES)
    with pytest.raises(history.WeekAlreadyRecorded):
        service.run_weekly_pass("2026-09-01", ["agent-a"], _grader_all_pass, cases=CASES)


def test_run_weekly_pass_uses_history_for_stable_score(bench_dir):
    service = BenchService(bench_dir)
    service.run_weekly_pass("2026-09-01", ["agent-a"], _grader_all_pass, cases=CASES)
    second = service.run_weekly_pass("2026-09-08", ["agent-a"], _grader_all_fail, cases=CASES)

    agent = second.ranked[0]
    # first week was a perfect 100; this week bombed at 0 -- the stable
    # score should sit strictly between the two, not jump straight to 0.
    assert 0.0 < agent.stable_score < 100.0
    assert agent.raw_score == 0.0


def test_run_weekly_pass_failed_grader_leaves_no_partial_run(bench_dir):
    service = BenchService(bench_dir)

    def boom(case: BenchCase, agent_id: str) -> CaseResult:
        raise RuntimeError("grader exploded")

    with pytest.raises(RuntimeError):
        service.run_weekly_pass("2026-09-01", ["agent-a"], boom, cases=CASES)

    assert history.list_runs(bench_dir) == []
    # the week is retryable, since nothing was recorded for it
    service.run_weekly_pass("2026-09-01", ["agent-a"], _grader_all_pass, cases=CASES)
