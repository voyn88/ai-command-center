"""A re-opened monitor finding must not reset the task that is fixing it.

VOYN-MON-WORKER-01-INFRA-DEPLOY-LAG-5ABD205763. A fail-closed monitor only
measures a class healthy once its fix is merged AND deployed, so every
monitor finding re-opens while the work that answers it is still travelling:
in a run, in review, or waiting for the promotion tick. The planner turns an
open finding with no linked task into a task through
`backlog_upsert_task(..., 'OPEN', ...)` -- the importer's reconciliation path,
the one function allowed to set a status directly -- so for the SECOND and
every later opening of the same (source, failure) that call did not "re-use"
the task, it yanked it back to OPEN: the IN_PROGRESS run or the
READY_TO_REVIEW pull request carrying the fix was discarded, and the revision
bump went on to make `backlog_ingest_results` raise `ingest transition
refused` when that run finished -- which aborts the whole ingest tick, not
just that task.

These are unit tests over a recording connection factory: the decision is
planner logic, and the SQL it then issues is pinned here rather than needing
the PostgreSQL rig (`tests/db/test_backlog_planner.py`, which also covers
this end to end under the real grants).
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from command_center.orchestrator.planner import (
    PlanLimits,
    Planner,
    _monitor_task_id,
    _monitor_task_is_in_flight,
)

#: The finding the fleet has been re-opening on worker-01 since 2026-09-13.
_SOURCE = "worker-01:infra"
_FAILURE = "deploy_lag"
_TASK = _monitor_task_id(_SOURCE, _FAILURE)


class _Cursor:
    def __init__(self, calls: list[tuple[str, tuple]], existing_status: str | None):
        self._calls = calls
        self._existing_status = existing_status
        self._result: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._calls.append((" ".join(sql.split()), params))
        self._result = self._answer(sql)

    def fetchall(self) -> list[tuple]:
        return self._result

    def _answer(self, sql: str) -> list[tuple]:
        if "backlog_lease_acquire" in sql:
            return [(True, "granted", 1)]
        if "backlog_lease_release" in sql:
            return [(True, "released", 1)]
        if "backlog_ingest_results" in sql:
            return []
        if "FROM monitor_finding" in sql:
            return [(41336, _SOURCE, _FAILURE)]
        if "SELECT status FROM backlog_task" in sql:
            return [] if self._existing_status is None else [(self._existing_status,)]
        if "backlog_upsert_task" in sql:
            return [(True, "inserted", True, 1)]
        if "FROM backlog_eligible" in sql:
            return []
        return []


def _factory(calls: list[tuple[str, tuple]], existing_status: str | None):
    class _Conn:
        def cursor(self):
            return _Cursor(calls, existing_status)

    @contextmanager
    def factory():
        yield _Conn()

    return factory


def _tick(existing_status: str | None):
    calls: list[tuple[str, tuple]] = []
    report = Planner(_factory(calls, existing_status)).plan_once(
        # Both reconciles off: this tick is only about the finding loop.
        PlanLimits(planner="planner-t", max_resumes_per_tick=0, review_backlog_limit=0)
    )
    return report, calls


def _sql(calls: list[tuple[str, tuple]], fragment: str) -> list[tuple]:
    return [call for call in calls if fragment in call[0]]


def test_a_findings_first_opening_files_the_task() -> None:
    """The ordinary path is unchanged: no task, so one is filed OPEN."""
    report, calls = _tick(None)

    assert report.monitor_tasks == [(_TASK, _FAILURE)]
    assert report.monitor_relinked == []
    upserts = _sql(calls, "backlog_upsert_task")
    assert len(upserts) == 1
    assert upserts[0][1][0] == _TASK and upserts[0][1][3] == "OPEN"
    assert _sql(calls, "monitor_link_task")[0][1] == (41336, _TASK)
    assert _sql(calls, "backlog_set_task_class")


@pytest.mark.parametrize(
    "status",
    ["IN_PROGRESS", "READY_TO_REVIEW", "OPEN", "DEFER_TO_USER", "SPLIT",
     "UNTRIAGED", "NEEDS_REFINEMENT", "DECIDED"],
)
def test_a_reopened_finding_leaves_the_task_carrying_its_fix_alone(status) -> None:
    """Linked, and nothing else touched.

    The link is what stops the loop re-reading this finding every tick (it
    selects `task_id IS NULL`), so a task left alone must still be linked --
    skipping the whole iteration would re-file it two minutes later.
    """
    report, calls = _tick(status)

    assert _sql(calls, "backlog_upsert_task") == []
    assert _sql(calls, "backlog_set_task_class") == []
    assert _sql(calls, "monitor_link_task")[0][1] == (41336, _TASK)
    assert report.monitor_relinked == [(_TASK, _FAILURE)]
    assert report.monitor_tasks == []


def test_a_finding_that_comes_back_after_the_fix_shipped_is_a_new_cycle() -> None:
    """DONE is the one state that re-files: the fix merged, the measurement
    came back, and that is new work rather than an in-flight run to protect.
    The body carries the NEW finding id, which is exactly why this case has
    to reach the upsert."""
    report, calls = _tick("DONE")

    upserts = _sql(calls, "backlog_upsert_task")
    assert len(upserts) == 1
    assert upserts[0][1][3] == "OPEN"
    assert "#41336" in upserts[0][1][6]
    assert report.monitor_tasks == [(_TASK, _FAILURE)]


def test_the_in_flight_predicate_files_only_absent_and_closed_tasks() -> None:
    assert _monitor_task_is_in_flight(None) is False
    assert _monitor_task_is_in_flight("DONE") is False
    for status in (
        "OPEN", "IN_PROGRESS", "READY_TO_REVIEW", "DEFER_TO_USER",
        "SPLIT", "UNTRIAGED", "NEEDS_REFINEMENT", "DECIDED",
    ):
        assert _monitor_task_is_in_flight(status) is True
