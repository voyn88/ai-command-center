"""Measurable candidate selection -- the "не «по названию»" acceptance
criterion, mechanised.

``select_candidate`` picks a winner strictly from recorded evidence
(historical success rate, cost, latency), never from name, source ordering,
or which candidate a finder happened to list first. A candidate with no
:class:`~command_center.skills.finder.CandidateMetrics` cannot win while a
scored alternative exists, and if *no* candidate carries evidence the
function refuses to choose at all rather than falling back to picking one by
name -- the caller sees that refusal in ``rationale`` and must not treat it as
a selection.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from command_center.skills.finder import CandidateProposal

#: Weights sum to 1.0; success rate dominates, cost and latency are
#: secondary tie-breakers among candidates that are otherwise plausible.
DEFAULT_WEIGHT_SUCCESS = 0.6
DEFAULT_WEIGHT_COST = 0.25
DEFAULT_WEIGHT_LATENCY = 0.15


@dataclass(frozen=True, slots=True)
class SelectionResult:
    """``winner`` is ``None`` exactly when no candidate carried measurable
    evidence -- a refusal, not a default pick. ``rationale`` is the full,
    machine-checkable justification: every scored candidate's numbers, the
    weights used, and which candidates were excluded as unscored."""

    winner: CandidateProposal | None
    rationale: dict = field(default_factory=dict)


def _normalize(values: list[float]) -> list[float]:
    """Min-max normalize to [0, 1]; a constant series normalizes to all-0s
    (no candidate is penalised relative to identical peers)."""
    low, high = min(values), max(values)
    if high == low:
        return [0.0 for _ in values]
    return [(v - low) / (high - low) for v in values]


def select_candidate(
    candidates: list[CandidateProposal],
    *,
    weight_success: float = DEFAULT_WEIGHT_SUCCESS,
    weight_cost: float = DEFAULT_WEIGHT_COST,
    weight_latency: float = DEFAULT_WEIGHT_LATENCY,
) -> SelectionResult:
    scored = [c for c in candidates if c.metrics is not None]
    unscored = [c for c in candidates if c.metrics is None]

    if not scored:
        return SelectionResult(
            winner=None,
            rationale={
                "method": "measurable-history-required",
                "reason": (
                    "no candidate carries historical success/cost/latency "
                    "evidence; refusing to select by name or listing order"
                ),
                "candidates_considered": len(candidates),
            },
        )

    norm_cost = _normalize([c.metrics.avg_cost for c in scored])
    norm_latency = _normalize([c.metrics.avg_latency_seconds for c in scored])

    ranked: list[tuple[float, CandidateProposal, dict]] = []
    for candidate, cost_n, latency_n in zip(scored, norm_cost, norm_latency):
        score = (
            weight_success * candidate.metrics.success_rate
            + weight_cost * (1.0 - cost_n)
            + weight_latency * (1.0 - latency_n)
        )
        ranked.append(
            (
                score,
                candidate,
                {
                    "name": candidate.name,
                    "content_hash": candidate.content_hash,
                    "score": score,
                    "success_rate": candidate.metrics.success_rate,
                    "avg_cost": candidate.metrics.avg_cost,
                    "avg_latency_seconds": candidate.metrics.avg_latency_seconds,
                },
            )
        )

    # Ties broken by content_hash ascending -- deterministic and never a
    # function of name or the order candidates were passed in.
    ranked.sort(key=lambda entry: (-entry[0], entry[1].content_hash))
    winning_score, winner, _ = ranked[0]

    return SelectionResult(
        winner=winner,
        rationale={
            "method": "weighted-historical-score",
            "weights": {
                "success": weight_success,
                "cost": weight_cost,
                "latency": weight_latency,
            },
            "scored": [entry[2] for entry in ranked],
            "unscored_excluded": [c.name for c in unscored],
            "winner_content_hash": winner.content_hash,
            "winner_score": winning_score,
        },
    )
