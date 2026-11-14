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
from command_center.ops.tick_stall_watchdog import (
    WatchdogConfig,
    record_skips,
    recover_ready_without_open_pr_streaks,
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


def test_empty_tick_breaks_streak_and_new_episode_has_new_identity(rig):  # noqa: F811
    app_factory, store, _queue = rig
    _stalled_task(store)
    _record_ticks(app_factory, 5)
    first = watchdog_once(app_factory)
    with app_factory() as conn:
        record_skips(conn, "review_marker", "empty-tick", [])
    assert not watchdog_once(app_factory).escalated
    with app_factory() as conn:
        for index in range(5):
            record_skips(conn, "review_marker", f"next-{index}", [(STALLED, REASON)])
    second = watchdog_once(app_factory)
    assert len(second.escalated) == 1
    assert first.escalated[0][1] != second.escalated[0][1]


def test_long_streak_retains_episode_after_history_window_would_shift(rig):  # noqa: F811
    app_factory, store, _queue = rig
    _stalled_task(store)
    _record_ticks(app_factory, 5)
    first = watchdog_once(app_factory)
    with app_factory() as conn:
        for index in range(5, 57):
            record_skips(conn, "review_marker", f"tick-{index:04}", [(STALLED, REASON)])
    second = watchdog_once(app_factory)
    assert not second.escalated and len(second.already_escalated) == 1
    assert second.already_escalated[0].first_tick_id == first.escalated[0][0].first_tick_id
    assert second.already_escalated[0].consecutive == 57
    assert len(_escalations(app_factory)) == 1


def test_same_task_different_reason_and_kind_do_not_collide(rig):  # noqa: F811
    app_factory, store, _queue = rig
    _stalled_task(store)
    with app_factory() as conn:
        for index in range(5):
            for kind in ("review_marker", "merge"):
                record_skips(conn, kind, f"tick-{index:04}",
                             [(STALLED, REASON), (STALLED, "another_reason")])
    report = watchdog_once(app_factory)
    assert len(report.escalated) == 4
    assert len({task for _, task in report.escalated}) == 4


def test_normal_window_wait_and_completed_task_do_not_create_work(rig):  # noqa: F811
    app_factory, store, _queue = rig
    _stalled_task(store)
    _record_ticks(app_factory, 4)
    with app_factory() as conn:
        record_skips(conn, "review_marker", "normal-wait", [(STALLED, "review_window_inactive")])
        record_skips(conn, "review_marker", "after-wait", [(STALLED, REASON)])
    assert not watchdog_once(app_factory).escalated
    with app_factory() as conn:
        for index in range(4):
            record_skips(conn, "review_marker", f"later-{index}", [(STALLED, REASON)])
        conn.execute("SELECT * FROM backlog_upsert_task(%s,'0','P1','DONE','task','Completed','','repo-one')", (STALLED,))
    assert not watchdog_once(app_factory).escalated
    assert _escalations(app_factory) == []


@pytest.mark.parametrize("suffix", ["", "-REM", "-REM-REM"])
def test_escalations_cannot_recursively_spawn_more_escalations(rig, suffix):  # noqa: F811
    app_factory, store, _queue = rig
    _stalled_task(store)
    _record_ticks(app_factory, 5)
    child = watchdog_once(app_factory).escalated[0][1] + suffix
    with app_factory() as conn:
        conn.execute("SELECT * FROM backlog_upsert_task(%s,'0','P1','READY_TO_REVIEW','task','Child','','repo-one')", (child,))
        for index in range(5):
            record_skips(conn, "review_marker", f"child-{index}", [(child, REASON)])
    assert not watchdog_once(app_factory).escalated
    assert len(_escalations(app_factory)) == 1


def _ready_without_pr(store, task_id: str) -> None:
    ok, reason, _changed = store.upsert_task(
        ParsedTask(
            task_id=task_id,
            wave="0",
            priority="P1",
            status="READY_TO_REVIEW",
            kind="task",
            title="Ready but unpublished",
            body="",
            repo="repo-one",
            line_no=1,
        )
    )
    assert ok, reason


def _pr_evidence_ticks(app_factory, task_id: str, count: int) -> None:
    with app_factory() as conn:
        for index in range(count):
            record_skips(
                conn,
                "pr_evidence",
                f"pr-evidence-{index}",
                [(task_id, "no_open_pr_on_task_branch")],
            )


def test_sustained_missing_pr_requeues_ready_task(rig) -> None:  # noqa: F811
    app_factory, store, _queue = rig
    task_id = "VOYN-W0-WD-PR-RECOVER"
    _ready_without_pr(store, task_id)
    _pr_evidence_ticks(app_factory, task_id, 5)

    with app_factory() as conn:
        report = recover_ready_without_open_pr_streaks(conn, threshold=5)

    assert report.recovered == [task_id]
    assert report.refused == []
    assert _task_row(app_factory, task_id)[0] == "OPEN"


def test_missing_pr_recovery_waits_for_full_streak(rig) -> None:  # noqa: F811
    app_factory, store, _queue = rig
    task_id = "VOYN-W0-WD-PR-WAIT"
    _ready_without_pr(store, task_id)
    _pr_evidence_ticks(app_factory, task_id, 4)

    with app_factory() as conn:
        report = recover_ready_without_open_pr_streaks(conn, threshold=5)

    assert report.recovered == []
    assert _task_row(app_factory, task_id)[0] == "READY_TO_REVIEW"


def test_missing_pr_recovery_uses_only_latest_observation(rig) -> None:  # noqa: F811
    app_factory, store, _queue = rig
    task_id = "VOYN-W0-WD-PR-RESET"
    _ready_without_pr(store, task_id)
    _pr_evidence_ticks(app_factory, task_id, 5)
    with app_factory() as conn:
        record_skips(conn, "pr_evidence", "pr-evidence-reset", [])
        report = recover_ready_without_open_pr_streaks(conn, threshold=5)

    assert report.recovered == []
    assert _task_row(app_factory, task_id)[0] == "READY_TO_REVIEW"
