"""Value-object tests for `command_center.evolution.types`."""

from __future__ import annotations

from command_center.evolution.types import (
    ConfigAgent,
    ConfigMetrics,
    RunOutcome,
    metrics_from_outcomes,
)


def test_config_agent_roundtrips_through_dict():
    agent = ConfigAgent(
        config_id="claude-baseline",
        genes={"executor": "claude", "max_attempts": 3},
        generation=2,
        parent_ids=("a", "b"),
    )
    restored = ConfigAgent.from_dict(agent.as_dict())
    assert restored == agent


def test_config_agent_from_dict_is_fail_closed_on_garbage():
    for garbage in (None, [], "nope", 42, {}, {"config_id": ""}, {"config_id": "x", "genes": "nope"}):
        assert ConfigAgent.from_dict(garbage) is None


def test_config_agent_from_dict_drops_unknown_genes():
    restored = ConfigAgent.from_dict(
        {"config_id": "x", "genes": {"executor": "claude", "made_up": "nonsense"}}
    )
    assert restored is not None
    assert restored.genes == {"executor": "claude"}


def test_config_agent_from_dict_rejects_bad_generation_and_parents():
    restored = ConfigAgent.from_dict(
        {"config_id": "x", "genes": {"executor": "claude"}, "generation": -3, "parent_ids": ["only-one"]}
    )
    assert restored is not None
    assert restored.generation == 0
    assert restored.parent_ids is None


def test_metrics_aggregate_across_outcomes():
    outcomes = [
        RunOutcome(config_id="c", succeeded=True, cost_usd=1.0, duration_seconds=100.0),
        RunOutcome(config_id="c", succeeded=False, cost_usd=2.0, duration_seconds=300.0),
        RunOutcome(config_id="c", succeeded=True, cost_usd=1.0, duration_seconds=200.0),
    ]
    metrics = metrics_from_outcomes(outcomes)
    assert metrics.runs == 3
    assert metrics.successes == 2
    assert metrics.success_rate == 2 / 3
    assert metrics.avg_cost_usd == (1.0 + 2.0 + 1.0) / 3
    assert metrics.avg_duration_seconds == (100.0 + 300.0 + 200.0) / 3


def test_metrics_with_no_runs_reports_zero_not_a_division_error():
    metrics = ConfigMetrics()
    assert metrics.success_rate == 0.0
    assert metrics.avg_cost_usd == 0.0
    assert metrics.avg_duration_seconds == 0.0


def test_metrics_roundtrips_through_dict():
    metrics = metrics_from_outcomes(
        [RunOutcome(config_id="c", succeeded=True, cost_usd=0.5, duration_seconds=90.0)]
    )
    restored = ConfigMetrics.from_dict(metrics.as_dict())
    assert restored == metrics


def test_metrics_from_dict_is_fail_closed_on_garbage():
    for garbage in (None, [], "nope", 42, {"runs": -5}, {"runs": "lots"}):
        assert ConfigMetrics.from_dict(garbage) == ConfigMetrics()


def test_metrics_from_dict_clamps_successes_to_runs():
    restored = ConfigMetrics.from_dict({"runs": 2, "successes": 99})
    assert restored.runs == 2
    assert restored.successes == 2
