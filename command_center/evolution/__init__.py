"""Evolutionary config-agent breeding (VOYN-MIN-EVOL).

A pure crossbreeding engine (`types`, `scorer`, `breeding`) plus a JSON-backed
population store (`store`) that follows the
`command_center.dispatch.policy_config` persistence pattern. `store.
evolve_population` is the single entry point that assembles one new
config-agent automatically from the fittest two config-agents in the current
population; `seeds.DEFAULT_SEED_CONFIG_AGENTS` bootstraps a population from
the fleet's proven executors.
"""

from command_center.evolution.breeding import (
    breed_new_config_agent,
    crossbreed,
    is_projected_improvement,
    projected_fitness,
)
from command_center.evolution.scorer import DEFAULT_WEIGHTS, FitnessWeights, dimension_value, fitness
from command_center.evolution.seeds import DEFAULT_SEED_CONFIG_AGENTS
from command_center.evolution.store import evolve_population, load_population, record_outcome, seed_population
from command_center.evolution.types import ConfigAgent, ConfigMetrics, RunOutcome, metrics_from_outcomes

__all__ = [
    "ConfigAgent",
    "ConfigMetrics",
    "RunOutcome",
    "metrics_from_outcomes",
    "FitnessWeights",
    "DEFAULT_WEIGHTS",
    "fitness",
    "dimension_value",
    "crossbreed",
    "projected_fitness",
    "is_projected_improvement",
    "breed_new_config_agent",
    "DEFAULT_SEED_CONFIG_AGENTS",
    "load_population",
    "seed_population",
    "record_outcome",
    "evolve_population",
]
