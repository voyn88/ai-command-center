"""Fitness scoring for config-agents.

Mirrors `advisor.scorer`'s pattern -- constructor-injected weights, small
`0.0..1.0`-normalized dimensions -- applied to `ConfigMetrics` instead of
advisor `Candidate.signals`.

The score is a sum of three independent per-dimension terms (success, cost,
duration) kept separable on purpose: `breeding.crossbreed` needs to know
which parent leads on which *dimension*, not just which parent has the
higher overall score, so it can inherit the gene that earned each parent its
lead rather than a coin flip that could regress either dimension.
"""

from __future__ import annotations

from dataclasses import dataclass

from command_center.evolution.types import ConfigMetrics

SUCCESS = "success"
COST = "cost"
DURATION = "duration"

#: Which gene each dimension is attributed to for guided crossover -- the
#: fleet knob most directly responsible for that outcome axis. `executor`
#: picks which account/CLI runs (the dominant cost lever:
#: `dispatch.models.DispatchPolicy.cost_matrix` is keyed by executor);
#: `max_attempts`/`timeout_seconds` are the fleet's existing retry/patience
#: budget (the dominant success lever -- a cascade's `max_attempts` already
#: IS its length, see `orchestrator.planner.plan_once`); `wip_limit` bounds
#: concurrent spend (`orchestrator.planner.PlanLimits.wip_limit`), a
#: secondary cost lever. `duration` has no dedicated gene -- it rides along
#: on whichever parent's other genes happen to help (see
#: `breeding.projected_fitness`).
GENE_DIMENSION: dict[str, str] = {
    "executor": COST,
    "wip_limit": COST,
    "max_attempts": SUCCESS,
    "timeout_seconds": SUCCESS,
}


@dataclass(frozen=True, slots=True)
class FitnessWeights:
    """Constructor-injected like `advisor.scorer.ProposalScorer` so a caller
    can retune without a code change. `cost_scale_usd`/`duration_scale_
    seconds` are the "this much is fully bad" normalizers a raw average is
    divided by before the penalty applies."""

    success_weight: float = 1.0
    cost_weight: float = 0.4
    duration_weight: float = 0.2
    cost_scale_usd: float = 5.0
    duration_scale_seconds: float = 1800.0


DEFAULT_WEIGHTS = FitnessWeights()


def _clamp01(value: float) -> float:
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else value


def dimension_value(
    metrics: ConfigMetrics, dimension: str, weights: FitnessWeights = DEFAULT_WEIGHTS
) -> float:
    """A `0.0..1.0` per-dimension score, always "higher is better" (cost and
    duration are inverted here) so callers can compare parents on any
    dimension with one `>=`. A config-agent with no recorded runs scores 0.0
    on every dimension -- untested, not good."""
    if not metrics.runs:
        return 0.0
    if dimension == SUCCESS:
        return _clamp01(metrics.success_rate)
    if dimension == COST:
        if weights.cost_scale_usd <= 0:
            return 0.0
        return _clamp01(1.0 - metrics.avg_cost_usd / weights.cost_scale_usd)
    if dimension == DURATION:
        if weights.duration_scale_seconds <= 0:
            return 0.0
        return _clamp01(1.0 - metrics.avg_duration_seconds / weights.duration_scale_seconds)
    raise ValueError(f"unknown fitness dimension: {dimension!r}")


def fitness(metrics: ConfigMetrics, weights: FitnessWeights = DEFAULT_WEIGHTS) -> float:
    """A config-agent with no recorded runs scores 0.0: untested, not good."""
    if metrics.runs == 0:
        return 0.0
    return (
        weights.success_weight * dimension_value(metrics, SUCCESS, weights)
        + weights.cost_weight * dimension_value(metrics, COST, weights)
        + weights.duration_weight * dimension_value(metrics, DURATION, weights)
    )
