"""The scheduled entrypoint that closes the self-assessment loop
(VOYN-MIN-AGT-EVO2).

Three pieces already exist and are each deliberately narrow:

* ``self_assessment.py`` is pure — given outcomes, a routing matrix and
  "now", it decides whether a quarter is due and what it would recommend.
* ``self_assessment_store.py`` remembers, across restarts, when each domain
  was last assessed and what was recommended, so "due" has something to be
  due relative to.
* ``self_assessment_source.py`` reconstructs :class:`AttemptOutcome` rows
  from work items shaped like ``WorkQueueReadStore.get_item``'s return
  value, because the queue schema records attempts, not executor names.

None of the three, alone, is "the fleet's best configuration per domain is
updated every quarter" — that requires something to actually fetch real
work items from the live queue, on a schedule, and hand them through the
other three in order. This module is that missing call site: it reads every
task class ``routing.ROUTING_MATRIX`` knows about, pulls its finished
(``succeeded``/``dead``) work items via :class:`WorkQueueReadStore`, and
feeds the resulting evidence to ``run_quarterly_self_assessment``.

Follows the same "standalone DB-aware module with a ``main()``, invoked by
a oneshot systemd timer" shape ``worktree_sweep.py`` and
``command_center/db/cli.py``'s ``queue-reap`` already use — no new
scheduling infrastructure. Unlike ``worktree_sweep.py`` this module DOES
read PostgreSQL (through ``WorkQueueReadStore``, the same read-only surface
the control plane's status endpoints already use), because "what actually
happened on the fleet" only lives there; the assessment's own memory
(:class:`SelfAssessmentStore`) stays local SQLite, matching
``daily_audit.py``'s scheduling bookkeeping.

Safe to run on every tick regardless of cadence: ``run_quarterly_self_
assessment`` is a no-op read (``store.last_assessed_at()`` plus the pure
gate) whenever the quarter is not yet due, so invoking this module hourly,
daily or via an operator's ad-hoc run all behave identically to invoking it
exactly once per quarter — the store, not the caller, is what makes the
cadence correct.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from command_center import storage
from command_center.db.work_queue_read import WorkQueueReadStore
from command_center.orchestrator.routing import ROUTING_MATRIX
from command_center.orchestrator.self_assessment import AttemptOutcome
from command_center.orchestrator.self_assessment_source import outcomes_from_work_items
from command_center.orchestrator.self_assessment_store import (
    SelfAssessmentStore,
    run_quarterly_self_assessment,
)

logger = logging.getLogger(__name__)

__all__ = [
    "resolve_db_path",
    "collect_outcomes",
    "run_self_assessment",
    "main",
]

#: Finished states worth reading back out of the queue as evidence. Mirrors
#: `self_assessment_source.FINISHED_ATTEMPT_STATES` — items outside these two
#: states carry only in-flight/never-attempted attempts, so listing them
#: would fetch work `get_item` cannot yet turn into an outcome.
_FINISHED_ITEM_STATES = ("succeeded", "dead")

#: Bound on how many finished items per (task_class, state) inform one run.
#: The scoring itself only needs enough samples to clear `min_samples`
#: (default 20 per executor); a bound this generous is about keeping one
#: scheduler tick's query cost predictable, not about starving the evidence.
_ITEMS_PER_QUERY_LIMIT = 500

ROOT = Path(__file__).resolve().parent.parent.parent


def resolve_db_path(root: Path | None = None) -> Path:
    """`<data dir>/self_assessment.db`, honoring `AICC_DATA_DIR` like every
    other local store in this codebase (see `storage.resolve_data_dir`)."""
    return storage.resolve_data_dir(root or ROOT) / "self_assessment.db"


def collect_outcomes(
    read_store: WorkQueueReadStore, *, task_classes: Any = None
) -> list[AttemptOutcome]:
    """Real evidence for every task class named in ``task_classes`` (default:
    every key ``routing.ROUTING_MATRIX`` currently has a cascade for).

    A task class maps onto ``queue`` one-for-one (the worker dispatches by
    queue name, and `routing.cascade_for` is keyed the same way), so this
    lists each queue's finished item ids, fetches each item's full attempt
    trail via ``get_item`` (``list_items`` itself does not carry attempts),
    and hands the lot to ``outcomes_from_work_items``. Best-effort per task
    class: a queue with no finished work yet simply contributes no outcomes,
    it does not abort the run for every other domain.
    """
    classes = list(task_classes) if task_classes is not None else list(ROUTING_MATRIX)
    outcomes: list[AttemptOutcome] = []
    for task_class in classes:
        items: list[dict[str, Any]] = []
        for state in _FINISHED_ITEM_STATES:
            summaries = read_store.list_items(
                queue=task_class, state=state, limit=_ITEMS_PER_QUERY_LIMIT
            )
            for summary in summaries:
                item = read_store.get_item(summary["work_item_id"])
                if item is not None:
                    items.append(item)
        outcomes.extend(outcomes_from_work_items(items, task_class=task_class))
    return outcomes


def run_self_assessment(
    *,
    store: SelfAssessmentStore | None = None,
    read_store: WorkQueueReadStore | None = None,
    now: datetime | None = None,
    db_path: Path | None = None,
) -> dict:
    """One scheduler tick: collect real evidence, run the quarterly gate,
    persist a recommendation per domain when (and only when) a new quarter
    is due. Returns the same report shape ``quarterly_self_assessment``
    produces, so a caller (this module's ``main``, or a test) can log or
    print it directly."""
    resolved_store = store or SelfAssessmentStore(db_path or resolve_db_path())
    resolved_read_store = read_store or WorkQueueReadStore()
    outcomes = collect_outcomes(resolved_read_store)
    return run_quarterly_self_assessment(
        resolved_store,
        outcomes,
        ROUTING_MATRIX,
        now=now or datetime.now(timezone.utc),
    )


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    report = run_self_assessment()
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, default=str))
    if report["due"]:
        logger.info(
            "self-assessment ran for quarter %s: %d domain(s) assessed",
            report["quarter"],
            len(report["recommendations"]),
        )
    else:
        logger.info("self-assessment not yet due")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
