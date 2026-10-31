"""Tests for command_center.bench.history."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.bench import history
from command_center.bench.types import CaseResult


@pytest.fixture()
def bench_dir(tmp_path: Path) -> Path:
    return tmp_path / "bench"


def _result(case_id="critical-001", agent_id="agent-a", *, passed=True, score=1.0) -> CaseResult:
    return CaseResult(case_id=case_id, agent_id=agent_id, passed=passed, score=score, evidence="e")


def test_record_run_returns_an_id(bench_dir):
    run_id = history.record_run(bench_dir, "2026-09-01")
    assert run_id


def test_record_run_rejects_duplicate_week(bench_dir):
    history.record_run(bench_dir, "2026-09-01")
    with pytest.raises(history.WeekAlreadyRecorded):
        history.record_run(bench_dir, "2026-09-01")


def test_record_run_allows_distinct_weeks(bench_dir):
    history.record_run(bench_dir, "2026-09-01")
    second = history.record_run(bench_dir, "2026-09-08")
    assert second


def test_record_and_list_case_results(bench_dir):
    run_id = history.record_run(bench_dir, "2026-09-01")
    result = _result()
    history.record_case_result(bench_dir, run_id, "critical", result)
    rows = history.list_case_results(bench_dir, run_id, "agent-a")
    assert len(rows) == 1
    assert rows[0]["case_id"] == "critical-001"
    assert rows[0]["category"] == "critical"
    assert rows[0]["passed"] is True
    assert rows[0]["evidence"] == "e"


def test_latest_stable_scores_picks_most_recent_week_per_agent(bench_dir):
    run1 = history.record_run(bench_dir, "2026-09-01")
    history.record_agent_score(
        bench_dir, run1, "agent-a", raw_score=50.0, stable_score=50.0, provisional=True
    )
    run2 = history.record_run(bench_dir, "2026-09-08")
    history.record_agent_score(
        bench_dir, run2, "agent-a", raw_score=70.0, stable_score=62.0, provisional=True
    )
    scores = history.latest_stable_scores(bench_dir)
    assert scores == {"agent-a": 62.0}


def test_latest_stable_scores_tracks_multiple_agents_independently(bench_dir):
    run1 = history.record_run(bench_dir, "2026-09-01")
    history.record_agent_score(bench_dir, run1, "agent-a", raw_score=10.0, stable_score=10.0, provisional=True)
    run2 = history.record_run(bench_dir, "2026-09-08")
    history.record_agent_score(bench_dir, run2, "agent-b", raw_score=90.0, stable_score=90.0, provisional=True)
    scores = history.latest_stable_scores(bench_dir)
    assert scores == {"agent-a": 10.0, "agent-b": 90.0}


def test_run_counts_accumulates_across_weeks(bench_dir):
    run1 = history.record_run(bench_dir, "2026-09-01")
    history.record_agent_score(bench_dir, run1, "agent-a", raw_score=1.0, stable_score=1.0, provisional=True)
    run2 = history.record_run(bench_dir, "2026-09-08")
    history.record_agent_score(bench_dir, run2, "agent-a", raw_score=2.0, stable_score=2.0, provisional=True)
    assert history.run_counts(bench_dir) == {"agent-a": 2}


def test_record_and_get_report(bench_dir):
    run_id = history.record_run(bench_dir, "2026-09-01")
    history.record_report(bench_dir, run_id, "2026-09-01", "# report body")
    assert history.get_report(bench_dir, "2026-09-01") == "# report body"


def test_get_report_missing_week_returns_none(bench_dir):
    assert history.get_report(bench_dir, "2099-01-01") is None


def test_list_runs_orders_newest_first(bench_dir):
    history.record_run(bench_dir, "2026-09-01")
    history.record_run(bench_dir, "2026-09-08")
    runs = history.list_runs(bench_dir)
    assert [r["week_of"] for r in runs] == ["2026-09-08", "2026-09-01"]


def test_operations_on_a_fresh_directory_do_not_raise(tmp_path):
    fresh = tmp_path / "never-touched"
    assert history.list_runs(fresh) == []
    assert history.latest_stable_scores(fresh) == {}
    assert history.run_counts(fresh) == {}
    assert history.get_report(fresh, "2026-09-01") is None
    history.record_run(fresh, "2026-09-01")
