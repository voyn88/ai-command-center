"""Tests for `command_center.evolution.breeding` -- the acceptance property
this feature exists for: crossbreeding two non-dominated config-agents
assembles a new one projected to beat both on fitness.
"""

from __future__ import annotations

import pytest

from command_center.evolution.breeding import (
    breed_new_config_agent,
    crossbreed,
    is_projected_improvement,
    projected_fitness,
)
from command_center.evolution.scorer import fitness
from command_center.evolution.types import ConfigAgent, RunOutcome, metrics_from_outcomes

CHEAP_BUT_UNRELIABLE = ConfigAgent(
    config_id="codex-cheap",
    genes={"executor": "codex", "max_attempts": 1, "timeout_seconds": 300, "wip_limit": 2},
)
THOROUGH_BUT_EXPENSIVE = ConfigAgent(
    config_id="claude-thorough",
    genes={"executor": "claude", "max_attempts": 5, "timeout_seconds": 1800, "wip_limit": 4},
)


def _outcomes(config_id: str, *, n: int, successes: int, cost_usd: float, duration_seconds: float):
    return [
        RunOutcome(
            config_id=config_id,
            succeeded=i < successes,
            cost_usd=cost_usd,
            duration_seconds=duration_seconds,
        )
        for i in range(n)
    ]


CHEAP_METRICS = metrics_from_outcomes(
    _outcomes("codex-cheap", n=10, successes=3, cost_usd=0.5, duration_seconds=200.0)
)
THOROUGH_METRICS = metrics_from_outcomes(
    _outcomes("claude-thorough", n=10, successes=9, cost_usd=2.0, duration_seconds=200.0)
)


def test_parents_are_non_dominated_fixture_sanity_check():
    """Neither parent leads on every dimension: cheap wins cost, thorough
    wins success -- the scenario `is_projected_improvement` is meant for."""
    assert fitness(CHEAP_METRICS) != fitness(THOROUGH_METRICS)
    assert is_projected_improvement(CHEAP_METRICS, THOROUGH_METRICS)


def test_crossbreed_inherits_each_gene_from_its_dimension_leader():
    child = crossbreed(
        CHEAP_BUT_UNRELIABLE, CHEAP_METRICS, THOROUGH_BUT_EXPENSIVE, THOROUGH_METRICS, child_id="child-1"
    )
    # cost-dimension genes come from the cheaper parent...
    assert child.gene("executor") == "codex"
    assert child.gene("wip_limit") == 2
    # ...success-dimension genes come from the more reliable parent.
    assert child.gene("max_attempts") == 5
    assert child.gene("timeout_seconds") == 1800


def test_crossbred_child_records_generation_and_lineage():
    child = crossbreed(
        CHEAP_BUT_UNRELIABLE, CHEAP_METRICS, THOROUGH_BUT_EXPENSIVE, THOROUGH_METRICS, child_id="child-1"
    )
    assert child.config_id == "child-1"
    assert child.generation == 1
    assert child.parent_ids == ("codex-cheap", "claude-thorough")


def test_crossbreed_is_deterministic():
    first = crossbreed(
        CHEAP_BUT_UNRELIABLE, CHEAP_METRICS, THOROUGH_BUT_EXPENSIVE, THOROUGH_METRICS, child_id="child-1"
    )
    second = crossbreed(
        CHEAP_BUT_UNRELIABLE, CHEAP_METRICS, THOROUGH_BUT_EXPENSIVE, THOROUGH_METRICS, child_id="child-1"
    )
    assert first == second


def test_projected_fitness_beats_both_parents_for_a_non_dominated_pair():
    projected = projected_fitness(CHEAP_METRICS, THOROUGH_METRICS)
    assert projected > fitness(CHEAP_METRICS)
    assert projected > fitness(THOROUGH_METRICS)


def test_dominance_ordered_pair_projects_no_improvement():
    """When one parent leads on every dimension, crossbreeding it with a
    strictly weaker parent cannot be projected to beat it -- the floor
    equals the dominant parent's own fitness, not something higher."""
    strictly_better = ConfigAgent(
        config_id="better",
        genes={"executor": "claude", "max_attempts": 3, "timeout_seconds": 900, "wip_limit": 4},
    )
    strictly_worse = ConfigAgent(
        config_id="worse",
        genes={"executor": "codex", "max_attempts": 1, "timeout_seconds": 300, "wip_limit": 2},
    )
    better_metrics = metrics_from_outcomes(
        _outcomes("better", n=10, successes=9, cost_usd=1.0, duration_seconds=100.0)
    )
    worse_metrics = metrics_from_outcomes(
        _outcomes("worse", n=10, successes=2, cost_usd=3.0, duration_seconds=900.0)
    )
    assert not is_projected_improvement(better_metrics, worse_metrics)
    child = crossbreed(strictly_better, better_metrics, strictly_worse, worse_metrics, child_id="child-2")
    assert child.genes == strictly_better.genes


def test_breed_new_config_agent_picks_the_two_fittest_by_realized_fitness():
    weak_third = ConfigAgent(
        config_id="copilot-weak",
        genes={"executor": "copilot", "max_attempts": 1, "timeout_seconds": 300, "wip_limit": 1},
    )
    weak_metrics = metrics_from_outcomes(
        _outcomes("copilot-weak", n=10, successes=1, cost_usd=4.5, duration_seconds=1700.0)
    )
    population = [
        (CHEAP_BUT_UNRELIABLE, CHEAP_METRICS),
        (THOROUGH_BUT_EXPENSIVE, THOROUGH_METRICS),
        (weak_third, weak_metrics),
    ]
    child = breed_new_config_agent(population)
    assert set(child.parent_ids) == {"codex-cheap", "claude-thorough"}
    assert "copilot-weak" not in child.parent_ids
    assert child.generation == 1


def test_breed_new_config_agent_child_id_is_deterministic_when_unspecified():
    population = [(CHEAP_BUT_UNRELIABLE, CHEAP_METRICS), (THOROUGH_BUT_EXPENSIVE, THOROUGH_METRICS)]
    first = breed_new_config_agent(population)
    second = breed_new_config_agent(population)
    assert first == second


def test_breed_new_config_agent_requires_two_ranked_parents():
    with pytest.raises(ValueError):
        breed_new_config_agent([(CHEAP_BUT_UNRELIABLE, CHEAP_METRICS)])
    with pytest.raises(ValueError):
        breed_new_config_agent(
            [(CHEAP_BUT_UNRELIABLE, CHEAP_METRICS), (THOROUGH_BUT_EXPENSIVE, metrics_from_outcomes([]))]
        )
