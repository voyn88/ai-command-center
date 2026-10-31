"""Tests for `command_center.evolution.scorer`."""

from __future__ import annotations

import pytest

from command_center.evolution.scorer import (
    COST,
    DURATION,
    SUCCESS,
    FitnessWeights,
    dimension_value,
    fitness,
)
from command_center.evolution.types import ConfigMetrics, RunOutcome, metrics_from_outcomes


def test_untested_config_scores_zero():
    assert fitness(ConfigMetrics()) == 0.0
    for dimension in (SUCCESS, COST, DURATION):
        assert dimension_value(ConfigMetrics(), dimension) == 0.0


def test_perfect_cheap_fast_config_scores_the_full_weight_sum():
    metrics = metrics_from_outcomes(
        [RunOutcome(config_id="c", succeeded=True, cost_usd=0.0, duration_seconds=0.0)]
    )
    weights = FitnessWeights()
    assert fitness(metrics, weights) == pytest.approx(
        weights.success_weight + weights.cost_weight + weights.duration_weight
    )


def test_higher_success_rate_scores_higher_all_else_equal():
    weak = metrics_from_outcomes(
        [RunOutcome(config_id="c", succeeded=False, cost_usd=1.0, duration_seconds=60.0)]
        + [RunOutcome(config_id="c", succeeded=True, cost_usd=1.0, duration_seconds=60.0)] * 1
    )
    strong = metrics_from_outcomes(
        [RunOutcome(config_id="c", succeeded=True, cost_usd=1.0, duration_seconds=60.0)] * 2
    )
    assert fitness(strong) > fitness(weak)


def test_higher_cost_scores_lower_all_else_equal():
    cheap = metrics_from_outcomes(
        [RunOutcome(config_id="c", succeeded=True, cost_usd=0.5, duration_seconds=60.0)]
    )
    expensive = metrics_from_outcomes(
        [RunOutcome(config_id="c", succeeded=True, cost_usd=4.0, duration_seconds=60.0)]
    )
    assert fitness(cheap) > fitness(expensive)


def test_dimension_value_clamps_beyond_scale():
    metrics = metrics_from_outcomes(
        [RunOutcome(config_id="c", succeeded=True, cost_usd=999.0, duration_seconds=999999.0)]
    )
    assert dimension_value(metrics, COST) == 0.0
    assert dimension_value(metrics, DURATION) == 0.0


def test_dimension_value_rejects_unknown_dimension():
    with pytest.raises(ValueError):
        dimension_value(ConfigMetrics(runs=1), "made_up")
