"""Why a backlog task came back, classified (VOYN-W0-AICC-PRIVILEGED-TASK-
ROUTED-TO-UNPRIVILEGED-EXECUTOR, acceptance 4).

The acceptance bar is a measurement, not a feature: "the share of
`task_status_failed` caused by missing authority falls to zero, and the
remaining `task_status_failed` are classified separately." Neither half was
answerable before this module. The queue records one reason string per park
(`backlog_event.reason`, written by `backlog_return_to_pool`), and the single
largest bucket on the live queue -- 8 of 13 returns in the measured
2026-08-30 window -- was the completely undifferentiated
`cascade_exhausted: task_status_failed`. "The agent finished and did not
report success" is not a cause; it is the absence of one.

So this module splits that bucket, and it splits it using evidence the store
already holds:

  * the **reason** says HOW the attempt ended;
  * the **task's own text** says whether the work needed a privilege at all
    (`authority_preflight.decide`, the same decision the planner and the
    worker gate make -- one vocabulary, three call sites);
  * `required_authority` in the dispatched **payload** says it outright, for
    everything dispatched since that contract existed.

`AUTHORITY` is therefore attributable retroactively: a task that failed with
`task_status_failed` and whose text declares, orders, or even merely quotes a
privileged command is the population the preflight now stops, and the
acceptance measurement is exactly its share going to zero -- while the rest
keep their own names (`QUOTA`, `PUBLISH`, `INFRASTRUCTURE`, ...) instead of
being averaged into one number nobody can act on.

Pure functions over strings and dicts: no I/O, no database. The CLI
(`db.cli backlog-failure-audit`) supplies the rows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from command_center.orchestrator import authority_preflight

#: The same anchor `backlog_reason_requires_authority` (0018) applies in SQL:
#: the token, not a bare substring, and at a word boundary so a wrapper that
#: prefixes the reason cannot shift it out of view.
_AUTHORITY_TOKEN = re.compile(
    r"(^|[^a-z_])" + re.escape(authority_preflight.PARK_REASON_PREFIX.strip())
)

__all__ = [
    "FailureClass",
    "FailureCounts",
    "classify_reason",
    "classify_park",
    "summarize",
]


class FailureClass:
    """The vocabulary. Deliberately small: every name here is a different
    ACTION, which is the only thing a classification is for."""

    #: A privilege no executor in this fleet grants. Nothing retries away.
    AUTHORITY = "missing_authority"
    #: A model account's rolling window is spent (the 2026-08-23 finding:
    #: 142 of 167 parked `task_status_failed` were session limits, not task
    #: defects). Time, or another account, fixes it.
    QUOTA = "quota_exhausted"
    #: The executor/host failed, not the task.
    INFRASTRUCTURE = "executor_infrastructure"
    #: The work ran but produced no publishable result.
    PUBLISH = "publish_failed"
    #: The agent finished without reporting success and nothing above
    #: explains it — a genuine task-level failure to look at by hand.
    TASK_DEFECT = "task_defect"
    #: An owner parked it, or the park predates the machine.
    OWNER = "owner_decision"
    #: Reason string outside every known shape. Never silently folded into a
    #: neighbouring bucket: an unknown cause must stay visibly unknown.
    UNKNOWN = "unclassified"


#: Substrings that identify a spent model-account window. Matched against the
#: reason and any result text the caller passes; these are the literal shapes
#: the fleet's own executors emit.
_QUOTA_MARKERS = (
    "session limit",
    "usage limit",
    "rate limit",
    "quota",
    "429",
    "resets at",
)


def classify_reason(reason: str | None) -> str | None:
    """The class the reason string alone establishes, or None when the reason
    is `task_status_failed` — the bucket that needs the task's own text to
    become a cause rather than a symptom."""
    if not reason:
        return FailureClass.UNKNOWN
    text = reason.strip()
    lowered = text.lower()
    # Checked first and independently of the `cascade_exhausted:` wrapper:
    # the same reason reaches the store bare (parked by the planner's
    # preflight) and wrapped (refused by the worker gate, folded into
    # `cascade_exhausted: <dead_reason>` by ingest). One cause, one class.
    #
    # Token-anchored, matching `backlog_reason_requires_authority` (0018)
    # exactly: this classifier and that gate must never disagree about
    # whether a reason is an authority reason, or the audited number and the
    # parking decision would be measuring different populations.
    if _AUTHORITY_TOKEN.search(text):
        return FailureClass.AUTHORITY
    if any(marker in lowered for marker in _QUOTA_MARKERS):
        return FailureClass.QUOTA
    if "executor infrastructure failure" in lowered or "executor_unavailable" in lowered:
        return FailureClass.INFRASTRUCTURE
    if "no_pr_published" in lowered or "publish_" in lowered:
        return FailureClass.PUBLISH
    if "task_status_" in lowered:
        return None  # symptom, not cause — see classify_park
    return FailureClass.UNKNOWN


def classify_park(
    reason: str | None,
    *,
    title: str | None = None,
    body: str | None = None,
    required_authority: list[str] | tuple[str, ...] | None = None,
    result_text: str | None = None,
) -> tuple[str, str]:
    """`(class, evidence)` for one parked task.

    `evidence` names WHAT decided it, so a number in the audit can always be
    traced back to the row that produced it rather than taken on faith.
    """
    from_reason = classify_reason(reason)
    if from_reason is not None and from_reason != FailureClass.UNKNOWN:
        return from_reason, f"reason={reason}"

    # `task_status_failed`: the agent finished and did not report success.
    # The dispatched contract is the strongest evidence available, because
    # the planner computed it from the same decision the gate enforces.
    if required_authority:
        return FailureClass.AUTHORITY, (
            "payload.required_authority="
            + authority_preflight.format_authority(required_authority)
        )

    # Retroactive attribution for everything dispatched before that contract
    # existed -- which is the entire measured window this task is about.
    decision = authority_preflight.decide(title, body)
    if decision.required:
        return FailureClass.AUTHORITY, (
            "task requires " + authority_preflight.format_authority(decision.required)
        )
    if decision.suspected:
        # Quoted, not ordered. Still the honest attribution for a run that
        # failed: the agent read the same text and tried the same command.
        return FailureClass.AUTHORITY, (
            "task quotes "
            + authority_preflight.format_authority(decision.suspected)
            + " (suspected)"
        )

    if result_text and any(
        marker in result_text.lower() for marker in _QUOTA_MARKERS
    ):
        return FailureClass.QUOTA, "result text names a spent account window"

    if from_reason is None:
        return FailureClass.TASK_DEFECT, f"reason={reason}"
    return FailureClass.UNKNOWN, f"reason={reason}"


@dataclass
class FailureCounts:
    """The audit's answer: totals per class, plus the two numbers acceptance
    4 asks for by name."""

    total: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    #: Parks whose reason is `task_status_failed` — the bucket under test.
    task_status_failed: int = 0
    #: ...of which are attributable to missing authority. The acceptance bar
    #: is this reaching zero.
    task_status_failed_authority: int = 0

    @property
    def authority_share(self) -> float:
        """The measured share, 0.0 when there is nothing to measure — an
        empty window is not evidence of success, and the caller prints the
        denominator beside it so the difference is never hidden."""
        if not self.task_status_failed:
            return 0.0
        return self.task_status_failed_authority / self.task_status_failed


def summarize(parks) -> tuple[FailureCounts, list[tuple[str, str, str]]]:
    """Classify an iterable of park rows.

    Each row is a mapping with `task_id` and `reason`, optionally `title`,
    `body`, `required_authority`, `result_text`. Returns the counts and one
    `(task_id, class, evidence)` line per row, in input order.
    """
    counts = FailureCounts()
    rows: list[tuple[str, str, str]] = []
    for park in parks:
        reason = park.get("reason")
        failure_class, evidence = classify_park(
            reason,
            title=park.get("title"),
            body=park.get("body"),
            required_authority=park.get("required_authority"),
            result_text=park.get("result_text"),
        )
        counts.total += 1
        counts.by_class[failure_class] = counts.by_class.get(failure_class, 0) + 1
        if reason and "task_status_failed" in reason:
            counts.task_status_failed += 1
            if failure_class == FailureClass.AUTHORITY:
                counts.task_status_failed_authority += 1
        rows.append((park.get("task_id", "?"), failure_class, evidence))
    return counts, rows
