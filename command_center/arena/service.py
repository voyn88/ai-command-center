"""The Арена service: run a duel and return an explained ranking.

:class:`DuelService` is the orchestrator — it validates the field, delegates
scoring to :class:`~command_center.arena.scorer.DuelScorer`, and turns the
scores into a ranking with a human-readable rationale at both the per-variant
and the overall level. Nothing here persists a duel; a caller (an API route,
a CLI, a scheduled job) owns that, the same separation the Советник (advisor)
engine draws between its pure scorer and its persisting service.
"""

from __future__ import annotations

from dataclasses import dataclass

from command_center.arena.scorer import DuelScorer, VariantScore
from command_center.arena.types import Case, SolutionVariant

__all__ = [
    "DuelService",
    "DuelResult",
    "RankedVariant",
]


@dataclass(frozen=True, slots=True)
class RankedVariant:
    """One variant's place in the final ranking, with its score and the
    per-variant rationale explaining how that rank was reached."""

    rank: int
    variant: SolutionVariant
    score: VariantScore
    rationale: str


@dataclass(frozen=True, slots=True)
class DuelResult:
    """The outcome of one duel: every variant ranked best-to-worst, the
    winner, and an overall rationale explaining the margin of victory."""

    case: Case
    ranking: list[RankedVariant]
    winner_agent_id: str
    rationale: str


class DuelService:
    """Run a duel: score every variant, rank them, and explain the result."""

    def __init__(self, scorer: DuelScorer | None = None) -> None:
        self._scorer = scorer or DuelScorer()

    def run(self, case: Case, variants: list[SolutionVariant]) -> DuelResult:
        """Score and rank ``variants`` for ``case``. Propagates
        :class:`~command_center.arena.scorer.TooFewVariantsError` when fewer
        than three variants are supplied — a duel is a comparison, not a
        solo run."""
        scores = self._scorer.score_all(variants)
        ordered = sorted(
            zip(variants, scores), key=lambda pair: pair[1].composite, reverse=True
        )
        ranking = [
            RankedVariant(
                rank=position,
                variant=variant,
                score=score,
                rationale=self._variant_rationale(position, variant, score),
            )
            for position, (variant, score) in enumerate(ordered, start=1)
        ]
        return DuelResult(
            case=case,
            ranking=ranking,
            winner_agent_id=ranking[0].variant.agent_id,
            rationale=self._overall_rationale(ranking),
        )

    @staticmethod
    def _variant_rationale(
        rank: int, variant: SolutionVariant, score: VariantScore
    ) -> str:
        verdict = "correct" if variant.correct else "incorrect"
        return (
            f"#{rank} {variant.agent_id}: {verdict}, "
            f"quality={score.quality:.2f}, explainability={score.explainability:.2f}, "
            f"time_score={score.time_score:.2f} ({variant.duration_seconds:.1f}s), "
            f"cost_score={score.cost_score:.2f} (${variant.cost_usd:.4f}) "
            f"-> composite={score.composite:.3f}"
        )

    @staticmethod
    def _overall_rationale(ranking: list[RankedVariant]) -> str:
        winner = ranking[0]
        message = (
            f"{winner.variant.agent_id} wins with composite "
            f"{winner.score.composite:.3f}"
        )
        if len(ranking) > 1:
            runner_up = ranking[1]
            margin = winner.score.composite - runner_up.score.composite
            message += (
                f", ahead of {runner_up.variant.agent_id} "
                f"({runner_up.score.composite:.3f}) by {margin:.3f}"
            )
        if not winner.variant.correct:
            message += "; note: the winner did not pass correctness"
        return message
