"""Formal detection of run-metric manipulation ("fast and dirty" completions).

An agent under completion-rate pressure has an incentive to claim a run is
done — ``APPROVED FOR COMMIT`` / ``READY FOR COMMIT`` / ``READY FOR FINAL
REVIEW`` — faster and with less real verification than the work required. This
module turns the cheap signals the run/report pipeline already produces
(duration, file count, self-reported verdict, ``report_parser`` extraction
confidence, whether a validation section is even present) into a normalized
:class:`GamingScore`, using the same constructor-injected-weights shape as
:class:`command_center.advisor.scorer.ProposalScorer` so detection sensitivity
and the penalty applied to a gamed run are both tunable without a code change —
the formally defined coefficients the acceptance contract requires.

Nothing here touches storage or the network: :func:`signals_from_run` reads a
plain mapping (the shape :func:`command_center.runtime.runs_read.list_unified_runs`
returns), so the detector can be exercised with hand-built dicts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from command_center import models

#: Self-reported verdicts that assert a run is done and safe to commit/merge —
#: the outcomes a manipulator has an incentive to claim without earning them.
_APPROVAL_VERDICTS = frozenset(
    {
        models.VERDICT_APPROVED_FOR_COMMIT,
        models.VERDICT_READY_FOR_COMMIT,
        models.VERDICT_READY_FOR_FINAL_REVIEW,
    }
)


def _clamp(value: float) -> float:
    """Clamp to the ``0.0..1.0`` band every signal and score live in."""
    return 0.0 if value < 0.0 else 1.0 if value > 1.0 else float(value)


@dataclass(frozen=True, slots=True)
class RunSignals:
    """The raw, cheaply-observed inputs a manipulation detector reasons over.

    Every field is derived from data the run/report pipeline already produces
    (see :func:`signals_from_run`) — detecting manipulation requires no new
    instrumentation."""

    duration_seconds: float | None
    files_touched: int
    verdict: str | None
    verdict_contradictory: bool
    confidence: str
    validation_present: bool


def signals_from_run(run: Mapping) -> RunSignals:
    """Build :class:`RunSignals` from a unified run dict (the shape
    :func:`command_center.runtime.runs_read.list_unified_runs` returns; its
    ``parsed`` sub-dict is a :func:`command_center.report_parser.parse_report`
    result). Missing/unparsed fields fall back to the least-suspicious reading
    (zero files, no verdict) rather than raising, so a run with an unreadable
    report is scored as "unknown," not flagged."""
    parsed = run.get("parsed") or {}
    files_touched = (
        len(parsed.get("files_modified") or [])
        + len(parsed.get("files_created") or [])
        + len(parsed.get("files_deleted") or [])
    )
    return RunSignals(
        duration_seconds=run.get("duration_seconds"),
        files_touched=files_touched,
        verdict=parsed.get("verdict"),
        verdict_contradictory=bool(parsed.get("verdict_contradictory")),
        confidence=parsed.get("confidence") or "none",
        validation_present=bool((parsed.get("validation_result") or "").strip()),
    )


@dataclass(frozen=True, slots=True)
class GamingScore:
    """A run's normalized manipulation signals and their weighted composite.

    All fields are in ``0.0..1.0``. ``risk`` is what a caller thresholds
    against; the individual signals stay on the result so a finding can explain
    *why* a run was flagged rather than just asserting a number."""

    velocity: float
    unvalidated_approval: float
    hollow_approval: float
    contradiction: float
    risk: float

    def reasons(self) -> list[str]:
        """Human-readable explanations for every signal that fired, in the same
        order they are weighted — empty when nothing fired (``risk == 0.0``)."""
        reasons = []
        if self.velocity > 0.0:
            reasons.append(f"file-touch rate implausible for its duration (velocity={self.velocity:.2f})")
        if self.unvalidated_approval:
            reasons.append("approved with no validation evidence in the report")
        if self.hollow_approval:
            reasons.append("approved with low/no report-extraction confidence")
        if self.contradiction:
            reasons.append("report asserts contradictory verdicts")
        return reasons


class GamingDetector:
    """Scores a run's manipulation risk and formally defines the penalty a
    caller applies to a gamed run's contribution to a downstream metric.

    Every weight and threshold is a constructor parameter (not a module
    constant hardcoded into the formula), so a project can retune detection —
    or the harshness of the penalty — without touching this class. The four
    signal weights are normalized to sum to 1.0 regardless of the raw numbers a
    caller passes, mirroring :class:`command_center.advisor.scorer.ProposalScorer`.
    """

    def __init__(
        self,
        *,
        velocity_weight: float = 0.35,
        unvalidated_approval_weight: float = 0.30,
        hollow_approval_weight: float = 0.20,
        contradiction_weight: float = 0.15,
        velocity_ceiling_files_per_minute: float = 12.0,
        penalty_coefficient: float = 0.75,
    ) -> None:
        total = (
            velocity_weight
            + unvalidated_approval_weight
            + hollow_approval_weight
            + contradiction_weight
        )
        if total <= 0:
            raise ValueError("the four signal weights must sum to > 0")
        # Normalize so the weights always sum to 1 regardless of the raw
        # numbers a caller passes, same as ProposalScorer's value weights.
        self._velocity_weight = velocity_weight / total
        self._unvalidated_approval_weight = unvalidated_approval_weight / total
        self._hollow_approval_weight = hollow_approval_weight / total
        self._contradiction_weight = contradiction_weight / total
        if velocity_ceiling_files_per_minute <= 0:
            raise ValueError("velocity_ceiling_files_per_minute must be > 0")
        self._velocity_ceiling = float(velocity_ceiling_files_per_minute)
        self._penalty_coefficient = _clamp(penalty_coefficient)

    @property
    def penalty_coefficient(self) -> float:
        """The formally defined discount :meth:`penalize` applies at maximal
        risk (``risk == 1.0``); a clean run (``risk == 0.0``) is never
        discounted regardless of this value."""
        return self._penalty_coefficient

    def detect(self, signals: RunSignals) -> GamingScore:
        approved = signals.verdict in _APPROVAL_VERDICTS

        # "Fast": an *approved* run touching more files per minute of
        # wall-clock time than a genuine review-and-fix pass plausibly
        # manages. Gated on `approved` — a fast run that was not claimed done
        # is not a metrics-manipulation signal, just a small or failed run.
        # Silent (0.0) when the duration is unknown or nothing was touched —
        # an absent signal is never treated as evidence of manipulation.
        velocity = 0.0
        if (
            approved
            and signals.duration_seconds is not None
            and signals.duration_seconds > 0
            and signals.files_touched > 0
        ):
            rate_per_minute = signals.files_touched / signals.duration_seconds * 60.0
            velocity = _clamp(rate_per_minute / self._velocity_ceiling)

        # "Dirty": approved without ever populating the report's Validation
        # section — a claim of done with no stated evidence it was checked.
        unvalidated_approval = 1.0 if approved and not signals.validation_present else 0.0

        # "Dirty": approved but the deterministic report parser could barely
        # corroborate the claim (few/no structured fields extracted).
        if approved and signals.confidence in ("none", "low"):
            hollow_approval = 1.0
        elif approved and signals.confidence == "medium":
            hollow_approval = 0.5
        else:
            hollow_approval = 0.0

        # A report asserting more than one distinct verdict is inherently
        # suspect regardless of which one report_parser's conservative
        # resolution picked.
        contradiction = 1.0 if signals.verdict_contradictory else 0.0

        risk = _clamp(
            self._velocity_weight * velocity
            + self._unvalidated_approval_weight * unvalidated_approval
            + self._hollow_approval_weight * hollow_approval
            + self._contradiction_weight * contradiction
        )
        return GamingScore(
            velocity=velocity,
            unvalidated_approval=unvalidated_approval,
            hollow_approval=hollow_approval,
            contradiction=contradiction,
            risk=risk,
        )

    def penalize(self, value: float, score: GamingScore) -> float:
        """Discount ``value`` (a priority, throughput credit, or any other
        downstream metric a run contributes to) by ``penalty_coefficient``
        scaled by ``score.risk``. A clean run (``risk == 0.0``) keeps its full
        value; a maximal-risk run (``risk == 1.0``) loses exactly
        ``penalty_coefficient`` of it."""
        return value * (1.0 - self._penalty_coefficient * score.risk)
