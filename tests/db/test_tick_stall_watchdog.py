"""detect_and_escalate (VOYN-W0-AICC-TICK-STALL-WATCHDOG) on live PostgreSQL:
fabricated `tick_skip_event` rows drive the same (task_id, reason) pair
through N consecutive ticks and the watchdog is asked to notice -- exactly
the "same task, same reason, tick after tick" pattern the ad-hoc human
monitors caught by hand on 2026-09-07 (PR #774, #707, #766).

Uses the real `next_tick_seq`/`record_tick_skips` write side rather than
hand-inserting rows, so the ordinal allocation these tests rely on is the
same one `backlog-review`/`backlog-merge` use in production."""

from __future__ import annotations

import pytest

from command_center.orchestrator.review_merge import next_tick_seq, record_tick_skips
from command_center.orchestrator.tick_stall_watchdog import (
    WatchdogConfig,
    detect_and_escalate,
)
from tests.db.test_backlog_planner import rig  # noqa: F401 -- pytest fixture

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]


def _tick(app_factory, tick_name: str, skips: list[tuple[str, str]]) -> int:
    """Allocate one tick_seq and record `skips` under it -- one fabricated
    tick invocation, the unit both `_recent_ticks` and `_run_length` count."""
    seq = next_tick_seq(app_factory)
    record_tick_skips(app_factory, seq, tick_name, skips)
    return seq


def test_escalates_after_n_consecutive_ticks_same_reason(rig) -> None:  # noqa: F811
    app_factory, store, _worker = rig
    for _ in range(5):
        _tick(app_factory, "backlog-review", [("TASK-A", "no_accept_marker_on_head")])

    report = detect_and_escalate(app_factory, WatchdogConfig(consecutive_threshold=5))

    assert len(report.escalated) == 1
    task_id, reason, count, escalation_task_id = report.escalated[0]
    assert (task_id, reason, count) == ("TASK-A", "no_accept_marker_on_head", 5)
    assert report.already_escalated == []

    escalation_task = store.get_task(escalation_task_id)
    assert escalation_task is not None
    assert escalation_task["status"] == "OPEN"
    assert escalation_task["priority"] == "P1"
    assert escalation_task["wave"] == "0"


def test_a_second_run_over_the_same_episode_is_a_pure_noop(rig) -> None:  # noqa: F811
    app_factory, store, _worker = rig
    for _ in range(5):
        _tick(app_factory, "backlog-review", [("TASK-B", "no_review_result_yet")])
    cfg = WatchdogConfig(consecutive_threshold=5)
    first = detect_and_escalate(app_factory, cfg)
    assert len(first.escalated) == 1
    _task_id, _reason, _count, escalation_task_id = first.escalated[0]

    second = detect_and_escalate(app_factory, cfg)

    assert second.escalated == []
    assert second.already_escalated == [("TASK-B", "no_review_result_yet", 5)]
    # Still exactly one escalation row and one backlog task -- no duplicate.
    rows = _rows(
        app_factory,
        "SELECT count(*) FROM tick_stall_escalation WHERE task_id = %s AND reason = %s",
        ("TASK-B", "no_review_result_yet"),
    )
    assert rows[0][0] == 1
    assert store.get_task(escalation_task_id) is not None


def test_below_threshold_is_not_escalated(rig) -> None:  # noqa: F811
    app_factory, _store, _worker = rig
    for _ in range(4):
        _tick(app_factory, "backlog-review", [("TASK-C", "review_chunk_verdict_missing:0")])

    report = detect_and_escalate(app_factory, WatchdogConfig(consecutive_threshold=5))

    assert report.escalated == []
    assert report.already_escalated == []
    rows = _rows(
        app_factory, "SELECT count(*) FROM tick_stall_escalation WHERE task_id = %s", ("TASK-C",)
    )
    assert rows[0][0] == 0


def test_a_changed_reason_breaks_the_run(rig) -> None:  # noqa: F811
    app_factory, _store, _worker = rig
    for _ in range(3):
        _tick(app_factory, "backlog-review", [("TASK-D", "no_accept_marker_on_head")])
    _tick(app_factory, "backlog-review", [("TASK-D", "review_chunk_verdict_missing:0")])
    for _ in range(2):
        _tick(app_factory, "backlog-review", [("TASK-D", "no_accept_marker_on_head")])

    report = detect_and_escalate(app_factory, WatchdogConfig(consecutive_threshold=5))

    # The reason-change tick breaks the streak: only the latest 2 ticks carry
    # the original reason again, well under the threshold -- and the 3-tick
    # run before the break is old news, not a current stall.
    assert report.escalated == []
    assert report.already_escalated == []


def test_tick_names_are_counted_independently(rig) -> None:  # noqa: F811
    app_factory, _store, _worker = rig
    for _ in range(5):
        _tick(app_factory, "backlog-review", [("TASK-E", "no_accept_marker_on_head")])
    for _ in range(2):
        _tick(app_factory, "backlog-merge", [("TASK-E", "no_accept_marker_on_head")])

    report = detect_and_escalate(app_factory, WatchdogConfig(consecutive_threshold=5))

    assert len(report.escalated) == 1
    task_id, reason, _count, _escalation_task_id = report.escalated[0]
    assert (task_id, reason) == ("TASK-E", "no_accept_marker_on_head")


def test_distinct_pairs_each_escalate_once_in_the_same_run(rig) -> None:  # noqa: F811
    app_factory, _store, _worker = rig
    for _ in range(5):
        _tick(
            app_factory,
            "backlog-merge",
            [
                ("TASK-F", "review_chunk_not_succeeded:1:dead"),
                ("TASK-G", "no_accept_marker_on_head"),
            ],
        )

    report = detect_and_escalate(app_factory, WatchdogConfig(consecutive_threshold=5))

    escalated_pairs = {(task_id, reason) for task_id, reason, _count, _id in report.escalated}
    assert escalated_pairs == {
        ("TASK-F", "review_chunk_not_succeeded:1:dead"),
        ("TASK-G", "no_accept_marker_on_head"),
    }


def _rows(factory, sql: str, params: tuple = ()) -> list[tuple]:
    with factory() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []
