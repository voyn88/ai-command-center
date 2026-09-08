from __future__ import annotations

from command_center import agent_metrics


def _run(run_id: str, agent: str, state: str, duration: float | None = None) -> dict:
    return {"id": run_id, "agent": agent, "state": state, "duration_seconds": duration}


def _completion(*, requires_human: bool = False, recovery_count: int = 0, review_verdict: str | None = None) -> dict:
    return {
        "requires_human": requires_human,
        "recovery_count": recovery_count,
        "review_verdict": review_verdict,
    }


def test_compute_agent_metrics_returns_nothing_for_no_runs():
    assert agent_metrics.compute_agent_metrics([], {}, {}) == []


def test_quality_counts_completed_runs_not_rejected_by_review():
    runs = [
        _run("r1", "claude", "COMPLETED"),
        _run("r2", "claude", "COMPLETED"),
        _run("r3", "claude", "FAILED"),
        _run("r4", "claude", "COMPLETED"),
    ]
    completions = {"r4": _completion(review_verdict="REJECT")}

    [metrics] = agent_metrics.compute_agent_metrics(runs, completions, {})

    assert metrics.agent == "claude"
    assert metrics.sample_size == 4
    # r1, r2 succeed; r3 failed; r4 completed but independent review rejected it.
    assert metrics.quality == 2 / 4


def test_manual_rework_and_rollback_rates_use_completion_only_denominator():
    runs = [
        _run("r1", "codex", "COMPLETED"),
        _run("r2", "codex", "COMPLETED"),
        _run("r3", "codex", "FAILED"),  # never reaches completion — excluded from these two rates
    ]
    completions = {
        "r1": _completion(requires_human=True, recovery_count=0),
        "r2": _completion(requires_human=False, recovery_count=2),
    }

    [metrics] = agent_metrics.compute_agent_metrics(runs, completions, {})

    assert metrics.raw.completions_total == 2
    assert metrics.manual_rework_rate == 1 / 2
    assert metrics.rollback_rate == 1 / 2


def test_manual_rework_and_rollback_rate_are_none_without_any_completion():
    runs = [_run("r1", "codex", "FAILED")]

    [metrics] = agent_metrics.compute_agent_metrics(runs, {}, {})

    assert metrics.manual_rework_rate is None
    assert metrics.rollback_rate is None


def test_speed_normalizes_fastest_agent_to_one_and_slowest_to_zero():
    runs = [
        _run("r1", "fast-agent", "COMPLETED", duration=10.0),
        _run("r2", "slow-agent", "COMPLETED", duration=110.0),
        _run("r3", "no-data-agent", "COMPLETED", duration=None),
    ]

    by_agent = {m.agent: m for m in agent_metrics.compute_agent_metrics(runs, {}, {})}

    assert by_agent["fast-agent"].speed == 1.0
    assert by_agent["slow-agent"].speed == 0.0
    assert by_agent["no-data-agent"].speed is None


def test_speed_is_one_for_every_agent_when_all_durations_tie():
    runs = [
        _run("r1", "a", "COMPLETED", duration=42.0),
        _run("r2", "b", "COMPLETED", duration=42.0),
    ]

    by_agent = {m.agent: m for m in agent_metrics.compute_agent_metrics(runs, {}, {})}

    assert by_agent["a"].speed == 1.0
    assert by_agent["b"].speed == 1.0


def test_cost_normalizes_fewest_attempts_to_one():
    runs = [
        _run("r1", "cheap", "COMPLETED"),
        _run("r2", "expensive", "COMPLETED"),
        _run("r3", "no-attempt-data", "COMPLETED"),
    ]
    attempts = {
        "r1": [{"attempt_number": 1}],
        "r2": [{"attempt_number": 1}, {"attempt_number": 2}, {"attempt_number": 3}],
    }

    by_agent = {m.agent: m for m in agent_metrics.compute_agent_metrics(runs, {}, attempts)}

    assert by_agent["cheap"].cost == 1.0
    assert by_agent["expensive"].cost == 0.0
    assert by_agent["no-attempt-data"].cost is None


def test_runs_without_agent_are_grouped_under_placeholder():
    runs = [{"id": "r1", "agent": None, "state": "COMPLETED", "duration_seconds": None}]

    [metrics] = agent_metrics.compute_agent_metrics(runs, {}, {})

    assert metrics.agent == "—"


def test_results_are_sorted_by_agent_name():
    runs = [
        _run("r1", "zeta", "COMPLETED"),
        _run("r2", "alpha", "COMPLETED"),
    ]

    result = agent_metrics.compute_agent_metrics(runs, {}, {})

    assert [m.agent for m in result] == ["alpha", "zeta"]
