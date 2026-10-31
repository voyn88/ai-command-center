import os
import time
from datetime import datetime

import pytest

from command_center.runtime import project_overview, session_view


@pytest.fixture
def process_tz():
    """Pin the process timezone for a test, then restore it.

    `completed_today_count` buckets by the *operator's* calendar day
    (`project_overview._is_today`), so it is a function of the reading host's
    zone by design. A test that asserts on it has to say which host it is
    reading from, or it is asserting on the machine it happens to run on.
    """
    original = os.environ.get("TZ")

    def _set(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    yield _set
    if original is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original
    time.tzset()


def _session(**overrides) -> dict:
    base = {
        "run_id": "run-1",
        "project_id": "AIOS",
        "status": session_view.STATUS_RUNNING,
        "executor": "claude_code",
        "workspace_path": "/workspace/a",
        "actual_branch": "main",
        "started_at": "2026-01-01T00:00:00",
        "finished_at": None,
    }
    base.update(overrides)
    return base


def test_build_project_overview_counts_running_and_waiting():
    now = datetime(2026, 1, 1, 1, 0, 0)
    sessions = [
        _session(run_id="r1", status=session_view.STATUS_RUNNING),
        _session(run_id="r2", status=session_view.STATUS_RUNNING),
        _session(run_id="r3", status=session_view.STATUS_WAITING),
        _session(run_id="r4", status=session_view.STATUS_REQUIRES_ATTENTION),
    ]
    overview = project_overview.build_project_overview("AIOS", sessions=sessions, project_cfg=None, now=now)
    assert overview["running_count"] == 2
    assert overview["waiting_count"] == 2  # Waiting + Requires Attention


@pytest.mark.parametrize("reader_tz", ["UTC", "Europe/Moscow", "America/Los_Angeles"])
def test_build_project_overview_completed_today_only_counts_todays_completions(
    process_tz, reader_tz
):
    """`now` and the stored `finished_at` are both naive UTC
    (`models.utc_now`/`models.iso_now`); the bucket is the reader's local day.
    Midday UTC is the same local day in every zone under test, so the count is
    the same from all three — the point being that the two sides are localised
    *together*, never one against the other."""
    process_tz(reader_tz)
    now = datetime(2026, 1, 2, 12, 0, 0)
    sessions = [
        _session(run_id="r1", status=session_view.STATUS_COMPLETED, finished_at="2026-01-02T09:00:00"),
        _session(run_id="r2", status=session_view.STATUS_COMPLETED, finished_at="2026-01-01T09:00:00"),
        _session(run_id="r3", status=session_view.STATUS_COMPLETED, finished_at=None),
    ]
    overview = project_overview.build_project_overview("AIOS", sessions=sessions, project_cfg=None, now=now)
    assert overview["completed_today_count"] == 1


def test_completed_today_is_the_readers_day_not_the_utc_one(process_tz):
    """The reason `_is_today` localises at all: a run finished at 21:00 UTC is
    already "tomorrow" for a reader in Moscow, and was "today" for one in
    Los Angeles. Comparing the raw UTC dates would answer the same in both,
    which is the wrong answer in one of them."""
    now = datetime(2026, 1, 2, 21, 30, 0)  # 00:30 Jan 3 in MSK, 13:30 Jan 2 in PT
    sessions = [
        _session(run_id="r1", status=session_view.STATUS_COMPLETED, finished_at="2026-01-02T21:00:00"),
    ]

    process_tz("America/Los_Angeles")
    pacific = project_overview.build_project_overview(
        "AIOS", sessions=sessions, project_cfg=None, now=now
    )
    process_tz("Europe/Moscow")
    moscow = project_overview.build_project_overview(
        "AIOS", sessions=sessions, project_cfg=None, now=now
    )

    # Same instant, same rows, both readers see their own day: still "today"
    # in Los Angeles, and also "today" in Moscow, where that day is Jan 3.
    assert pacific["completed_today_count"] == 1
    assert moscow["completed_today_count"] == 1


def test_build_project_overview_current_fields_from_most_recent_active_session():
    now = datetime(2026, 1, 1, 1, 0, 0)
    sessions = [
        _session(
            run_id="r1", status=session_view.STATUS_RUNNING, executor="claude_code",
            workspace_path="/workspace/old", actual_branch="old-branch", started_at="2026-01-01T00:00:00",
        ),
        _session(
            run_id="r2", status=session_view.STATUS_RUNNING, executor="claude_code",
            workspace_path="/workspace/new", actual_branch="new-branch", started_at="2026-01-01T00:30:00",
        ),
    ]
    overview = project_overview.build_project_overview("AIOS", sessions=sessions, project_cfg=None, now=now)
    assert overview["current_workspace"] == "/workspace/new"
    assert overview["current_branch"] == "new-branch"
    assert overview["current_executor"] == "claude_code"


def test_build_project_overview_falls_back_to_project_defaults_when_no_active_session():
    now = datetime(2026, 1, 1, 1, 0, 0)
    sessions = [_session(status=session_view.STATUS_COMPLETED, finished_at="2026-01-01T00:00:00")]
    cfg = {"default_executor": "claude_code", "default_workspace_path": "/default/ws", "default_branch": "main"}
    overview = project_overview.build_project_overview("AIOS", sessions=sessions, project_cfg=cfg, now=now)
    assert overview["current_executor"] == "claude_code"
    assert overview["current_workspace"] == "/default/ws"
    assert overview["current_branch"] == "main"


def test_build_project_overview_health_ok_when_nothing_wrong():
    now = datetime(2026, 1, 1, 1, 0, 0)
    sessions = [_session(status=session_view.STATUS_RUNNING)]
    overview = project_overview.build_project_overview("AIOS", sessions=sessions, project_cfg=None, now=now)
    assert overview["health"] == project_overview.HEALTH_OK


def test_build_project_overview_health_attention_on_waiting_session():
    now = datetime(2026, 1, 1, 1, 0, 0)
    sessions = [_session(status=session_view.STATUS_WAITING)]
    overview = project_overview.build_project_overview("AIOS", sessions=sessions, project_cfg=None, now=now)
    assert overview["health"] == project_overview.HEALTH_ATTENTION


def test_build_project_overview_health_attention_on_stale_heartbeat():
    now = datetime(2026, 1, 1, 1, 0, 0)
    sessions = [_session(run_id="stale-run", status=session_view.STATUS_RUNNING)]
    overview = project_overview.build_project_overview(
        "AIOS", sessions=sessions, project_cfg=None, now=now, stale_run_ids=frozenset({"stale-run"})
    )
    assert overview["health"] == project_overview.HEALTH_ATTENTION


def test_build_project_overview_health_degraded_on_failed_session():
    now = datetime(2026, 1, 1, 1, 0, 0)
    sessions = [_session(status=session_view.STATUS_FAILED)]
    overview = project_overview.build_project_overview("AIOS", sessions=sessions, project_cfg=None, now=now)
    assert overview["health"] == project_overview.HEALTH_DEGRADED


def test_build_project_overview_health_degraded_wins_over_waiting():
    now = datetime(2026, 1, 1, 1, 0, 0)
    sessions = [
        _session(run_id="r1", status=session_view.STATUS_WAITING),
        _session(run_id="r2", status=session_view.STATUS_REQUIRES_ATTENTION),
    ]
    overview = project_overview.build_project_overview("AIOS", sessions=sessions, project_cfg=None, now=now)
    assert overview["health"] == project_overview.HEALTH_DEGRADED
