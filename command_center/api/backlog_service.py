"""Read access to the Postgres-backed autonomous delivery backlog.

Thin by the same rule as :mod:`command_center.db.backlog_store`: every
decision (what a status means, what evidence a transition requires) already
lives in the SQL layer or in ``BacklogStore``. This module only shapes rows
into :mod:`command_center.api.backlog_schemas` and applies the one HTTP-layer
policy that belongs here — the page-size ceiling.

Connection lifecycle: ``BacklogStore()`` with no factory resolves to
``command_center.db.pool.connection`` (see ``backlog_store.py``), so the
caller must have opened the pool (``open_pool()`` at process startup, guarded
by ``AICC_PG_HOST`` — see ``app.py``) before any of these run.
"""

from __future__ import annotations

from command_center.api import backlog_schemas as schemas
from command_center.db.backlog_store import BacklogStore

#: Every status the backlog's own CHECK constraint allows
#: (``backlog_task_status_vocabulary``, migration 0001/0005/0007/0008) —
#: kept here so a status with zero tasks still renders as a 0 column instead
#: of silently vanishing from the dashboard.
_STATUS_VOCABULARY = (
    "OPEN",
    "IN_PROGRESS",
    "READY_TO_REVIEW",
    "DONE",
    "UNTRIAGED",
    "DEFER_TO_USER",
    "SPLIT",
    "NEEDS_REFINEMENT",
    "DECIDED",
)

MAX_LIMIT = 500


def _task_model(row: dict) -> schemas.BacklogTask:
    return schemas.BacklogTask(**row)


def get_status_counts() -> schemas.BacklogStatusCounts:
    live = BacklogStore().counts_by_status()
    counts = {status: live.get(status, 0) for status in _STATUS_VOCABULARY}
    # A status outside the known vocabulary (a future migration's new value)
    # still surfaces rather than being silently dropped.
    for status, count in live.items():
        counts.setdefault(status, count)
    return schemas.BacklogStatusCounts(counts=counts, total=sum(counts.values()))


def list_tasks(
    *, status: str | None = None, limit: int = 100, offset: int = 0
) -> schemas.BacklogTaskList:
    bounded_limit = max(1, min(limit, MAX_LIMIT))
    rows, total = BacklogStore().list_tasks(
        status=status, limit=bounded_limit, offset=max(0, offset)
    )
    return schemas.BacklogTaskList(
        tasks=[_task_model(r) for r in rows],
        limit=bounded_limit,
        offset=offset,
        total=total,
    )


def get_task_detail(task_id: str) -> schemas.BacklogTaskDetail | None:
    store = BacklogStore()
    task = store.get_task(task_id)
    if task is None:
        return None
    events = [
        schemas.BacklogEvent(
            event=e["event"],
            outcome=e["outcome"],
            reason=e["reason"],
            actor=e["actor"],
            detail=e["detail"],
            created_at=str(e["created_at"]),
        )
        for e in store.list_events(task_id)
    ]
    evidence = [
        schemas.BacklogEvidence(
            kind=ev["kind"], value=ev["value"], recorded_at=str(ev["recorded_at"])
        )
        for ev in store.list_evidence(task_id)
    ]
    return schemas.BacklogTaskDetail(
        task=_task_model(task), events=events, evidence=evidence
    )
