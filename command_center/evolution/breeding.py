"""Guided crossbreeding: assemble one new config-agent from the two fittest
in a population.

Gene-wise, not whole-parent, recombination: each gene is inherited from
whichever parent leads `scorer.GENE_DIMENSION[gene]`'s dimension (a gene
with no recorded dimension falls back to the overall fitter parent). This is
Pareto-guided by construction, not a coin flip -- if parent A leads on cost
and parent B leads on success, the child inherits A's cost genes and B's
success genes. `projected_fitness` below is the provable payoff of that
choice: a weighted sum of the *better* of the two parents' values on every
dimension can never be less than either parent's own fitness, and is
strictly greater whenever the parents are not dominance-ordered (each leads
on at least one dimension the other doesn't) -- see `test_breeding.py` for
that exact non-dominated case, which is what "improvement in metrics" means
here before the child has accumulated any `RunOutcome`s of its own.
"""

from __future__ import annotations

from collections.abc import Sequence

from command_center.evolution.scorer import (
    COST,
    DEFAULT_WEIGHTS,
    DURATION,
    FitnessWeights,
    GENE_DIMENSION,
    SUCCESS,
    dimension_value,
    fitness,
)
from command_center.evolution.types import ConfigAgent, ConfigMetrics

__all__ = [
    "crossbreed",
    "projected_fitness",
    "is_projected_improvement",
    "breed_new_config_agent",
]


def crossbreed(
    parent_a: ConfigAgent,
    metrics_a: ConfigMetrics,
    parent_b: ConfigAgent,
    metrics_b: ConfigMetrics,
    *,
    child_id: str,
    weights: FitnessWeights = DEFAULT_WEIGHTS,
) -> ConfigAgent:
    """Assemble a new config-agent from `parent_a`/`parent_b`'s genes.

    Deterministic and pure: the same parents/metrics/`child_id` always
    produce the same child, so this is exercised with hand-built fixtures
    like the rest of the house `dispatch`/`advisor` engines -- no RNG, no
    I/O.
    """
    default_parent = (
        parent_a if fitness(metrics_a, weights) >= fitness(metrics_b, weights) else parent_b
    )
    genes: dict[str, object] = {}
    for gene_name in dict.fromkeys((*parent_a.genes, *parent_b.genes)):
        dimension = GENE_DIMENSION.get(gene_name)
        if dimension is None:
            source = default_parent
        else:
            value_a = dimension_value(metrics_a, dimension, weights)
            value_b = dimension_value(metrics_b, dimension, weights)
            source = parent_a if value_a >= value_b else parent_b
        genes[gene_name] = (
            source.genes[gene_name] if gene_name in source.genes else default_parent.genes.get(gene_name)
        )
    return ConfigAgent(
        config_id=child_id,
        genes=genes,
        generation=max(parent_a.generation, parent_b.generation) + 1,
        parent_ids=(parent_a.config_id, parent_b.config_id),
    )


def projected_fitness(
    metrics_a: ConfigMetrics, metrics_b: ConfigMetrics, weights: FitnessWeights = DEFAULT_WEIGHTS
) -> float:
    """The best-of-both-parents fitness estimate crossbreeding is bred
    toward -- NOT a guarantee of real-world results (a config-agent's true
    fitness is only known once it accumulates its own `RunOutcome`s), but a
    provable floor: it takes the better of the two parents' values on EVERY
    dimension independently, so it can never be less than either parent's
    actual fitness (see `is_projected_improvement`)."""
    return sum(
        weight * max(dimension_value(metrics_a, dimension, weights), dimension_value(metrics_b, dimension, weights))
        for dimension, weight in (
            (SUCCESS, weights.success_weight),
            (COST, weights.cost_weight),
            (DURATION, weights.duration_weight),
        )
    )


def is_projected_improvement(
    metrics_a: ConfigMetrics, metrics_b: ConfigMetrics, weights: FitnessWeights = DEFAULT_WEIGHTS
) -> bool:
    """True when the two parents are not dominance-ordered -- each leads on
    at least one dimension the other doesn't -- so crossbreeding them is
    projected to beat either parent alone, not merely match the better one."""
    baseline = max(fitness(metrics_a, weights), fitness(metrics_b, weights))
    return projected_fitness(metrics_a, metrics_b, weights) > baseline


def breed_new_config_agent(
    population: Sequence[tuple[ConfigAgent, ConfigMetrics]],
    *,
    child_id: str | None = None,
    weights: FitnessWeights = DEFAULT_WEIGHTS,
) -> ConfigAgent:
    """The package's core entry point for "assemble one new config-agent
    automatically": ranks `population` by realized fitness and crossbreeds
    the two fittest. Raises `ValueError` if fewer than two config-agents
    have any recorded runs -- crossbreeding needs two ranked parents, not
    zero-evidence seeds.

    `child_id` defaults to a deterministic name derived from the parents and
    the resulting generation, so a caller does not have to invent one just
    to get an automatic cross; pass an explicit id to re-breed the same pair
    under a fresh name."""
    ranked = sorted(
        (pair for pair in population if pair[1].runs > 0),
        key=lambda pair: fitness(pair[1], weights),
        reverse=True,
    )
    if len(ranked) < 2:
        raise ValueError("need at least two config-agents with recorded runs to crossbreed")
    (parent_a, metrics_a), (parent_b, metrics_b) = ranked[0], ranked[1]
    generation = max(parent_a.generation, parent_b.generation) + 1
    resolved_id = child_id or f"{parent_a.config_id}+{parent_b.config_id}@g{generation}"
    return crossbreed(parent_a, metrics_a, parent_b, metrics_b, child_id=resolved_id, weights=weights)
