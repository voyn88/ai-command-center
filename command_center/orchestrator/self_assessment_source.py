"""Turns real work-attempt history into self-assessment evidence
(VOYN-MIN-AGT-EVO2).

``self_assessment.py``'s :class:`~command_center.orchestrator.self_assessment.AttemptOutcome`
names an executor and a success/failure. ``work_queue_read.py``'s
``WorkQueueReadStore.get_item`` returns work items whose attempts carry
``attempt_no`` and ``state`` — not an executor name, because the worker
resolves ``cascade[attempt_no - 1]`` (clamped) at dispatch time, exactly as
``routing.py``'s own docstring describes. The executor a real attempt used
is therefore not a column the queue schema carries at all; it is the
``task_class``'s cascade at the position the attempt already lived at. This
module is the mechanical join between the two: for each finished attempt of
a work item, it reconstructs which executor served it from
``routing.cascade_for(task_class)`` and records whether that attempt
succeeded, producing the evidence ``score_executors``/``recommend_cascade``
need — without this module guessing at, or duplicating, the worker's own
clamped-index resolution rule.

Deliberately conservative about what counts as evidence: only attempts in a
finished state (``succeeded`` or ``dead``, the same two states
``work_queue_read``'s ``_STATES`` treats as terminal) are counted. A
``claimed``/``ready`` attempt has not yet produced an outcome and must not
be scored as either a success or a failure — an in-flight attempt is not
evidence of anything yet.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from command_center.orchestrator.routing import cascade_for
from command_center.orchestrator.self_assessment import AttemptOutcome

__all__ = ["outcomes_from_work_items"]

#: The two terminal attempt states — matching `work_queue_read.py`'s own
#: `_STATES` minus the in-flight `ready`/`claimed` pair. An attempt in
#: either of those two has not yet produced an outcome to score.
FINISHED_ATTEMPT_STATES = frozenset({"succeeded", "dead"})


def _parse_timestamp(value: Any) -> datetime:
    """Best-effort parse of whatever timestamp shape the caller has on hand.

    `work_queue_read._rows_to_dicts` already stringifies `*_at` columns, so
    the common case is an ISO string; a caller supplying a `datetime`
    directly (e.g. in a test) is accepted as-is. A missing/unparseable value
    falls back to "now" rather than raising — an attempt's outcome is worth
    recording even when its own timestamp is absent, and `AttemptOutcome`
    has no way to represent "unknown time".
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return datetime.now(timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def outcomes_from_work_items(
    items: Iterable[dict[str, Any]], *, task_class: str
) -> list[AttemptOutcome]:
    """Build one domain's evidence from work items shaped like
    ``WorkQueueReadStore.get_item``'s return value (each carrying an
    ``"attempts"`` list of dicts with ``attempt_no``, ``state`` and an
    ``updated_at``/``created_at`` timestamp).

    Every finished attempt across every item becomes one
    :class:`AttemptOutcome`, its executor resolved positionally from
    ``routing.cascade_for(task_class)`` at ``attempt_no`` (clamped to the
    cascade's own length, matching how the worker itself resolves it) —
    never guessed from any column the queue schema does not carry.

    Callers are responsible for having already selected only the items
    belonging to ``task_class`` (however that domain maps onto their own
    queue naming); this function does not filter items by queue itself, so
    it stays usable regardless of that mapping. An unknown ``task_class``
    (no cascade configured) yields no evidence rather than raising, the
    same "absence is not an error" stance ``routing.cascade_for`` itself
    does not take — but this function's job is only to describe what
    happened, and nothing happened for a domain with no cascade to score
    against.
    """
    cascade = cascade_for(task_class)
    if not cascade:
        return []

    outcomes: list[AttemptOutcome] = []
    for item in items:
        for attempt in item.get("attempts", []):
            state = attempt.get("state")
            if state not in FINISHED_ATTEMPT_STATES:
                continue
            attempt_no = attempt.get("attempt_no") or 1
            index = min(max(int(attempt_no) - 1, 0), len(cascade) - 1)
            executor = cascade[index]["executor"]
            occurred_at = _parse_timestamp(
                attempt.get("updated_at") or attempt.get("created_at")
            )
            outcomes.append(
                AttemptOutcome(
                    task_class=task_class,
                    executor=executor,
                    succeeded=state == "succeeded",
                    occurred_at=occurred_at,
                )
            )
    return outcomes
