"""Per-agent quality-drift detection: threshold degradation -> quarantine.

Acceptance (VOYN-MIN-AGT-DRIFT2): threshold degradation over 10 percentage
points across 2 windows triggers quarantine and retraining, with the
consequence that a degrading agent produces 0 further critical errors. This
module is the pure, deterministic decision function; `policy.plan_dispatch`
is what actually structurally enforces "0 further errors" by refusing to
dispatch a quarantined executor (`DEFER_AGENT_QUARANTINED`), and
`service`/`policy_config` persist the resulting `QuarantineRecord`.

No I/O here — `evaluate_degradation` takes whatever windowed outcome tallies
the caller has already built (from run history, review verdicts, or any
other quality signal) and returns a typed verdict, exactly like
`policy.plan_dispatch` stays pure by taking an already-assembled
`ExecutorProfile`/`QueuedTask` list instead of reading the board itself.
"""

from __future__ import annotations

from dataclasses import dataclass

from command_center.dispatch.models import QuarantineRecord

# A window counts as "degraded" once its failure rate exceeds the agent's
# baseline failure rate by more than this many percentage points. 0.10 == the
# acceptance's literal ">10%".
DEGRADATION_THRESHOLD = 0.10

# Two consecutive degraded windows are required before quarantine fires — the
# acceptance's "в 2 окна" (in 2 windows). One bad window is noise a single
# outlier task can produce; two in a row is a trend.
CONSECUTIVE_WINDOWS_REQUIRED = 2

# A window with fewer runs than this is too small to be statistically
# meaningful (a single failure in a 2-run window is a 50% failure rate) and is
# skipped entirely rather than counted toward "consecutive" — so quarantine
# can never fire off noise from a handful of stray failures.
MIN_RUNS_PER_WINDOW = 5

# Typed verdict reasons, mirroring the DEFER_* convention in `models.py`: a
# caller branches on these, never on free-form prose.
REASON_INSUFFICIENT_DATA = "insufficient_data"
REASON_WITHIN_THRESHOLD = "within_threshold"
REASON_DEGRADATION_CONFIRMED = "degradation_confirmed"


@dataclass(frozen=True)
class QualityWindow:
    """One evaluation window's outcome tally for a single executor.

    `window_id` must be stable and orderable by the caller (e.g. an ISO-8601
    window start, or a zero-padded sequence number) — `evaluate_degradation`
    treats the input list's order as chronological (oldest first) and never
    re-sorts it itself, so a caller that hands in the wrong order gets the
    wrong verdict rather than a silently "corrected" one.
    """

    window_id: str
    total_runs: int
    # Runs in this window that were not a clean success — failed/cancelled/
    # blocked/incomplete, whatever the caller's quality signal counts as a
    # non-OK outcome. Always `<= total_runs`; the caller's responsibility.
    non_ok_runs: int

    @property
    def failure_rate(self) -> float | None:
        if self.total_runs <= 0:
            return None
        return self.non_ok_runs / self.total_runs

    @property
    def has_enough_data(self) -> bool:
        return self.total_runs >= MIN_RUNS_PER_WINDOW


@dataclass(frozen=True)
class DegradationVerdict:
    """The outcome of evaluating one executor's recent windows."""

    executor_id: str
    quarantine: bool
    retrain_required: bool
    reason: str
    baseline_failure_rate: float
    breaching_window_ids: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "executor_id": self.executor_id,
            "quarantine": self.quarantine,
            "retrain_required": self.retrain_required,
            "reason": self.reason,
            "baseline_failure_rate": self.baseline_failure_rate,
            "breaching_window_ids": list(self.breaching_window_ids),
        }

    def to_quarantine_record(self, *, quarantined_at: str | None) -> QuarantineRecord | None:
        """The `QuarantineRecord` this verdict authorizes, or `None` when it
        does not confirm degradation — a caller should never construct a
        `QuarantineRecord` from a non-quarantining verdict itself, so that
        decision lives in exactly one place."""
        if not self.quarantine:
            return None
        return QuarantineRecord(
            executor_id=self.executor_id,
            reason=self.reason,
            quarantined_at=quarantined_at,
            retrain_required=self.retrain_required,
            breaching_window_ids=self.breaching_window_ids,
            baseline_failure_rate=self.baseline_failure_rate,
        )


def evaluate_degradation(
    executor_id: str,
    baseline_failure_rate: float,
    windows: list[QualityWindow],
) -> DegradationVerdict:
    """Pure, total: decide whether `executor_id` should be quarantined.

    `windows` must be in chronological order (oldest first); only the most
    recent `CONSECUTIVE_WINDOWS_REQUIRED` windows *with enough data* are
    considered — a window skipped for insufficient data does not break the
    "consecutive" run, it is simply not evidence either way. Quarantine
    fires only when every one of those windows' failure rate exceeds
    `baseline_failure_rate` by more than `DEGRADATION_THRESHOLD`.
    """
    baseline = max(0.0, min(1.0, baseline_failure_rate))
    usable = [w for w in windows if w.has_enough_data]

    if len(usable) < CONSECUTIVE_WINDOWS_REQUIRED:
        return DegradationVerdict(
            executor_id=executor_id,
            quarantine=False,
            retrain_required=False,
            reason=REASON_INSUFFICIENT_DATA,
            baseline_failure_rate=baseline,
        )

    tail = usable[-CONSECUTIVE_WINDOWS_REQUIRED:]
    # `has_enough_data` (the `usable` filter above) guarantees `total_runs > 0`
    # for every window here, so `failure_rate` is never `None`.
    breaching = tuple(
        w.window_id
        for w in tail
        if (w.failure_rate or 0.0) - baseline > DEGRADATION_THRESHOLD
    )
    confirmed = len(breaching) == CONSECUTIVE_WINDOWS_REQUIRED

    return DegradationVerdict(
        executor_id=executor_id,
        quarantine=confirmed,
        retrain_required=confirmed,
        reason=REASON_DEGRADATION_CONFIRMED if confirmed else REASON_WITHIN_THRESHOLD,
        baseline_failure_rate=baseline,
        # Only surfaced once it is the confirming evidence — a lone breaching
        # window inside an otherwise-healthy pair is noise, not something a
        # caller should key off of, so it is not reported here either.
        breaching_window_ids=breaching if confirmed else (),
    )
