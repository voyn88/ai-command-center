"""The tick-stall watchdog end to end on live PostgreSQL (VOYN-W0-AICC-TICK-
STALL-WATCHDOG): skip rows recorded under the real ``aicc_app`` grants, the
episode ledger's UNIQUE identity doing the exactly-once work, and the
escalation reaching the backlog inbox as a new OPEN task while the stalled
task itself stays untouched.

Skipped wholesale unless ``AICC_TEST_PG_ADMIN_DSN`` is set — see ``conftest``.
"""

from __future__ import annotations

import pytest

from command_center.db.backlog_parser import ParsedTask
from command_center.orchestrator.watchdog import (
    WatchdogConfig,
    record_skips,
    watchdog_once,
)
from tests.db.test_backlog_planner import (  # noqa: F401 — pytest fixtures
    _test_repo_routes,
    rig,
)

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

STALLED = "VOYN-W0-WD-STALLED"
REASON = "no_review_result_yet"


def _stalled_task(store) -> None:
    ok, reason, _changed = store.upsert_task(
        ParsedTask(
            task_id=STALLED, wave="0", priority="P1", status="READY_TO_REVIEW",
            kind="task", title="A task the marker tick keeps skipping",
            body="", repo="repo-one", line_no=1,
        )
    )
    assert ok, reason


def _record_ticks(app_factory, count: int, *, kind: str = "review_marker") -> None:
    with app_factory() as conn:
        for index in range(count):
            record_skips(conn, kind, f"tick-{index:04}", [(STALLED, REASON)])


def _escalations(app_factory) -> list[tuple]:
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT tick_kind, task_id, reason, consecutive_ticks,"
            "       escalation_task_id FROM tick_stall_escalation ORDER BY id"
        )
        return cur.fetchall()


def _task_row(app_factory, task_id: str):
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, revision, title FROM backlog_task WHERE task_id = %s",
            (task_id,),
        )
        return cur.fetchone()


def test_five_consecutive_skips_escalate_exactly_once(rig) -> None:  # noqa: F811
    app_factory, store, _queue = rig
    _stalled_task(store)
    _record_ticks(app_factory, 5)

    report = watchdog_once(app_factory, WatchdogConfig(threshold=5))
    assert len(report.escalated) == 1 and not report.refused
    episode, new_task_id = report.escalated[0]
    assert (episode.task_id, episode.reason, episode.consecutive) == (STALLED, REASON, 5)

    rows = _escalations(app_factory)
    assert [row[:4] for row in rows] == [("review_marker", STALLED, REASON, 5)]
    status, _revision, title = _task_row(app_factory, new_task_id)
    assert status == "OPEN"
    assert STALLED in title


def test_a_second_run_over_the_same_episode_does_not_duplicate(rig) -> None:  # noqa: F811
    app_factory, store, _queue = rig
    _stalled_task(store)
    _record_ticks(app_factory, 6)

    first = watchdog_once(app_factory, WatchdogConfig(threshold=5))
    second = watchdog_once(app_factory, WatchdogConfig(threshold=5))
    assert len(first.escalated) == 1
    assert second.escalated == [] and len(second.already_escalated) == 1
    assert len(_escalations(app_factory)) == 1

    # The streak growing does not reopen the episode either.
    with app_factory() as conn:
        record_skips(conn, "review_marker", "tick-9998", [(STALLED, REASON)])
    third = watchdog_once(app_factory, WatchdogConfig(threshold=5))
    assert third.escalated == []
    assert len(_escalations(app_factory)) == 1


def test_fewer_than_threshold_ticks_do_not_escalate(rig) -> None:  # noqa: F811
    app_factory, store, _queue = rig
    _stalled_task(store)
    _record_ticks(app_factory, 4)

    report = watchdog_once(app_factory, WatchdogConfig(threshold=5))
    assert report.escalated == [] and report.already_escalated == []
    assert _escalations(app_factory) == []


def test_the_watchdog_never_mutates_the_stalled_task(rig) -> None:  # noqa: F811
    app_factory, store, _queue = rig
    _stalled_task(store)
    before = _task_row(app_factory, STALLED)
    _record_ticks(app_factory, 5)

    watchdog_once(app_factory, WatchdogConfig(threshold=5))
    assert _task_row(app_factory, STALLED) == before


def test_an_interleaved_reason_resets_the_streak_on_live_rows(rig) -> None:  # noqa: F811
    app_factory, store, _queue = rig
    _stalled_task(store)
    with app_factory() as conn:
        for index in range(4):
            record_skips(conn, "review_marker", f"tick-{index:04}", [(STALLED, REASON)])
        record_skips(
            conn, "review_marker", "tick-0004", [(STALLED, "marker_already_posted")]
        )
        for index in range(5, 9):
            record_skips(conn, "review_marker", f"tick-{index:04}", [(STALLED, REASON)])

    report = watchdog_once(app_factory, WatchdogConfig(threshold=5))
    assert report.escalated == []
    assert _escalations(app_factory) == []
