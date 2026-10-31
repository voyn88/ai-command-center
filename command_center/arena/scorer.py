"""Five-axis scoring for an agent duel.

The Арена compares contestants on the acceptance criteria verbatim:
**explainability, quality, time, cost and correctness**. Correctness and
quality/explainability are absolute (already ``0.0..1.0``, or a pass/fail
verdict); time and cost are relative — a variant that took 40s is only "slow"
next to one that took 4s, so both are normalized against the field's own
min/max rather than against a fixed scale. The result is one composite score
per variant plus its five components, so a ranking can always be explained by
pointing at which axis moved it (the explainability of the comparison itself).
"""

from __future__ import annotations

from dataclasses import dataclass

from command_center.arena.types import SolutionVariant

MIN_VARIANTS = 3


def _clamp(value: float) -> float:
    """Clamp to the ``0.0..1.0`` band every axis lives in."""
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


class TooFewVariantsError(ValueError):
    """Raised when a duel is scored with fewer than :data:`MIN_VARIANTS`
    solution variants — a duel is a comparison, and the acceptance criterion
    requires at least three independent attempts to compare."""


@dataclass(frozen=True, slots=True)
class VariantScore:
    """One variant's normalized axes and composite score.

    ``time_score``/``cost_score`` are inverted (higher is better, like the
    other axes) so all five components read the same direction: 1.0 is always
    the best a variant can do on that axis, within this duel's field.
    """

    agent_id: str
    correctness: float
    quality: float
    explainability: float
    time_score: float
    cost_score: float
    composite: float


class DuelScorer:
    """Compute a :class:`VariantScore` for every variant in a duel.

    Weights are constructor-injected so a caller can retune emphasis (e.g.
    weigh correctness even harder for a safety-critical case) without a code
    change. Defaults favor correctness and quality — a fast, cheap, well
    explained *wrong* answer should not outrank a correct one — while still
    letting time/cost break ties among otherwise-equal correct attempts."""

    def __init__(
        self,
        *,
        correctness_weight: float = 0.40,
        quality_weight: float = 0.25,
        explainability_weight: float = 0.15,
        time_weight: float = 0.10,
        cost_weight: float = 0.10,
    ) -> None:
        total = (
            correctness_weight
            + quality_weight
            + explainability_weight
            + time_weight
            + cost_weight
        )
        if total <= 0:
            raise ValueError("duel scorer weights must sum to > 0")
        self._correctness_weight = correctness_weight / total
        self._quality_weight = quality_weight / total
        self._explainability_weight = explainability_weight / total
        self._time_weight = time_weight / total
        self._cost_weight = cost_weight / total

    def score_all(self, variants: list[SolutionVariant]) -> list[VariantScore]:
        """Score every variant in one duel. Raises :class:`TooFewVariantsError`
        when fewer than :data:`MIN_VARIANTS` are supplied — a duel needs a
        field to compare against, and a field of one or two is not a
        competition."""
        if len(variants) < MIN_VARIANTS:
            raise TooFewVariantsError(
                f"a duel needs at least {MIN_VARIANTS} solution variants, "
                f"got {len(variants)}"
            )
        durations = [v.duration_seconds for v in variants]
        costs = [v.cost_usd for v in variants]
        min_duration, max_duration = min(durations), max(durations)
        min_cost, max_cost = min(costs), max(costs)

        scores = []
        for variant in variants:
            time_score = self._invert(
                variant.duration_seconds, min_duration, max_duration
            )
            cost_score = self._invert(variant.cost_usd, min_cost, max_cost)
            correctness = 1.0 if variant.correct else 0.0
            quality = _clamp(variant.quality)
            explainability = _clamp(variant.explainability)
            composite = (
                self._correctness_weight * correctness
                + self._quality_weight * quality
                + self._explainability_weight * explainability
                + self._time_weight * time_score
                + self._cost_weight * cost_score
            )
            scores.append(
                VariantScore(
                    agent_id=variant.agent_id,
                    correctness=correctness,
                    quality=quality,
                    explainability=explainability,
                    time_score=time_score,
                    cost_score=cost_score,
                    composite=_clamp(composite),
                )
            )
        return scores

    @staticmethod
    def _invert(value: float, low: float, high: float) -> float:
        """Map ``value`` onto ``0.0..1.0`` within ``[low, high]``, inverted so
        the smallest raw value (fastest/cheapest) scores 1.0. When every
        variant tied (``low == high``) nobody is penalized on this axis."""
        if high == low:
            return 1.0
        return _clamp((high - value) / (high - low))
