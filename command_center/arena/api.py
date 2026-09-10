"""Application entry the controller calls: run a duel, return the API response.

The HTTP route stays a one-liner — it calls :func:`run_duel`, which maps the
wire :class:`~command_center.arena.schemas.DuelRunRequest` onto the engine's
:class:`~command_center.arena.types.Case`/:class:`~command_center.arena.types.SolutionVariant`,
delegates to :class:`~command_center.arena.service.DuelService` and maps the
internal :class:`~command_center.arena.service.DuelResult` back onto the wire
:class:`~command_center.arena.schemas.DuelRunResponse`. Keeping the mapping
here (not in the route) means the same one call is reusable from a CLI or a
scheduled job later without going through FastAPI.
"""

from __future__ import annotations

from command_center.arena.schemas import (
    DuelRunRequest,
    DuelRunResponse,
    RankedVariantOut,
)
from command_center.arena.service import DuelResult, DuelService
from command_center.arena.types import Case, SolutionVariant


def _to_response(result: DuelResult) -> DuelRunResponse:
    return DuelRunResponse(
        case_id=result.case.id,
        winner_agent_id=result.winner_agent_id,
        rationale=result.rationale,
        ranking=[
            RankedVariantOut(
                rank=ranked.rank,
                agent_id=ranked.variant.agent_id,
                correct=ranked.variant.correct,
                correctness=ranked.score.correctness,
                quality=ranked.score.quality,
                explainability=ranked.score.explainability,
                time_score=ranked.score.time_score,
                cost_score=ranked.score.cost_score,
                composite=ranked.score.composite,
                rationale=ranked.rationale,
            )
            for ranked in result.ranking
        ],
    )


def run_duel(
    request: DuelRunRequest, *, service: DuelService | None = None
) -> DuelRunResponse:
    """Run one duel with the default engine (or an injected ``service``) and
    return the API response model. Propagates
    :class:`~command_center.arena.scorer.TooFewVariantsError` when the request
    carries fewer than three variants."""
    engine = service or DuelService()
    case = Case(id=request.case_id, prompt=request.case_prompt)
    variants = [
        SolutionVariant(
            agent_id=v.agent_id,
            output=v.output,
            rationale=v.rationale,
            correct=v.correct,
            quality=v.quality,
            explainability=v.explainability,
            duration_seconds=v.duration_seconds,
            cost_usd=v.cost_usd,
        )
        for v in request.variants
    ]
    result = engine.run(case, variants)
    return _to_response(result)
