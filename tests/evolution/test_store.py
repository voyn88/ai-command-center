"""Tests for `command_center.evolution.store`.

`AICC_DATA_DIR` is redirected to a temp dir by the session conftest, so
`store.*` writes never touch the developer's real `data/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.evolution import store
from command_center.evolution.scorer import fitness
from command_center.evolution.types import ConfigAgent, RunOutcome

ROOT = Path("/unused-because-AICC_DATA_DIR-overrides")

CLAUDE = ConfigAgent(config_id="claude-baseline", genes={"executor": "claude", "max_attempts": 3})
CODEX = ConfigAgent(config_id="codex-baseline", genes={"executor": "codex", "max_attempts": 3})


def test_load_population_is_empty_before_anything_is_seeded():
    assert store.load_population(ROOT) == {}


def test_seed_population_registers_config_agents_with_zero_metrics():
    store.seed_population(ROOT, [CLAUDE, CODEX])
    population = store.load_population(ROOT)
    assert set(population) == {"claude-baseline", "codex-baseline"}
    agent, metrics = population["claude-baseline"]
    assert agent == CLAUDE
    assert metrics.runs == 0


def test_seed_population_does_not_clobber_existing_metrics():
    store.seed_population(ROOT, [CLAUDE])
    store.record_outcome(
        ROOT, RunOutcome(config_id="claude-baseline", succeeded=True, cost_usd=1.0, duration_seconds=60.0)
    )
    store.seed_population(ROOT, [CLAUDE])  # re-running the bootstrap
    _, metrics = store.load_population(ROOT)["claude-baseline"]
    assert metrics.runs == 1


def test_record_outcome_persists_across_reloads():
    store.seed_population(ROOT, [CLAUDE])
    store.record_outcome(
        ROOT, RunOutcome(config_id="claude-baseline", succeeded=True, cost_usd=0.5, duration_seconds=30.0)
    )
    store.record_outcome(
        ROOT, RunOutcome(config_id="claude-baseline", succeeded=False, cost_usd=0.5, duration_seconds=30.0)
    )
    _, metrics = store.load_population(ROOT)["claude-baseline"]
    assert metrics.runs == 2
    assert metrics.successes == 1
    assert metrics.total_cost_usd == 1.0


def test_record_outcome_refuses_an_unknown_config_id():
    store.seed_population(ROOT, [CLAUDE])
    with pytest.raises(KeyError):
        store.record_outcome(
            ROOT, RunOutcome(config_id="typo-id", succeeded=True, cost_usd=1.0, duration_seconds=1.0)
        )


def test_evolve_population_assembles_and_persists_one_new_config_agent():
    store.seed_population(ROOT, [CLAUDE, CODEX])
    for _ in range(9):
        store.record_outcome(
            ROOT, RunOutcome(config_id="claude-baseline", succeeded=True, cost_usd=2.0, duration_seconds=200.0)
        )
    store.record_outcome(
        ROOT, RunOutcome(config_id="claude-baseline", succeeded=False, cost_usd=2.0, duration_seconds=200.0)
    )
    for _ in range(3):
        store.record_outcome(
            ROOT, RunOutcome(config_id="codex-baseline", succeeded=True, cost_usd=0.5, duration_seconds=200.0)
        )
    for _ in range(7):
        store.record_outcome(
            ROOT, RunOutcome(config_id="codex-baseline", succeeded=False, cost_usd=0.5, duration_seconds=200.0)
        )

    before = store.load_population(ROOT)
    child = store.evolve_population(ROOT)
    after = store.load_population(ROOT)

    # Exactly one new config-agent was assembled, on top of the two seeds.
    assert set(after) == set(before) | {child.config_id}
    assert child.parent_ids == ("claude-baseline", "codex-baseline")
    assert child.generation == 1

    parents_by_id = {"claude-baseline": before["claude-baseline"], "codex-baseline": before["codex-baseline"]}
    parent_a_metrics = parents_by_id[child.parent_ids[0]][1]
    parent_b_metrics = parents_by_id[child.parent_ids[1]][1]
    from command_center.evolution.breeding import projected_fitness

    assert projected_fitness(parent_a_metrics, parent_b_metrics) > max(
        fitness(parent_a_metrics), fitness(parent_b_metrics)
    )


def test_evolve_population_requires_two_evaluated_config_agents():
    store.seed_population(ROOT, [CLAUDE])
    with pytest.raises(ValueError):
        store.evolve_population(ROOT)


def test_evolve_population_refuses_to_silently_reset_an_existing_child():
    store.seed_population(ROOT, [CLAUDE, CODEX])
    store.record_outcome(
        ROOT, RunOutcome(config_id="claude-baseline", succeeded=True, cost_usd=1.0, duration_seconds=60.0)
    )
    store.record_outcome(
        ROOT, RunOutcome(config_id="codex-baseline", succeeded=True, cost_usd=1.0, duration_seconds=60.0)
    )
    store.evolve_population(ROOT)
    with pytest.raises(ValueError):
        store.evolve_population(ROOT)  # same top-two pair, same deterministic id
