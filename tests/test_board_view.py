"""Coverage for the Board/Investor view (`command_center.ui.board_view`),
VOYN-MIN-BOARD-LAUNCH: a weekly, one-page, jargon-free summary and risks for
a board member or investor.

Two layers, mirroring `test_live_board.py`:

1. Pure `build_weekly_summary` unit tests — no Streamlit runtime, so the
   business rule ("what counts as a risk this week") is pinned down directly.
2. An `AppTest.from_file` smoke test that the real `app.py` page renders in
   plain language, without exposing the app's internal jargon.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center import tasks_repository
from command_center.ui import board_view

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")

NOW = datetime(2026, 9, 10, 12, 0, 0)  # a Thursday


def _task(task_id: str, title: str, **fields) -> dict:
    return {
        "id": task_id,
        "title": title,
        "project": "AIOS",
        "status": "Backlog",
        "priority": "Medium",
        "created_at": "2026-09-01T09:00:00",
        "updated_at": "2026-09-01T09:00:00",
        **fields,
    }


# --------------------------------------------------------------------------
# 1. Pure `build_weekly_summary` — no Streamlit involved
# --------------------------------------------------------------------------


def test_no_tasks_is_on_track_with_no_risks_or_wins():
    summary = board_view.build_weekly_summary([], {}, now=NOW)

    assert summary.overall_health == board_view.HEALTH_ON_TRACK
    assert summary.risks == ()
    assert summary.completed_titles == ()
    assert summary.decisions_needed == ()
    assert summary.projects == ()


def test_done_task_updated_within_the_week_counts_as_a_win():
    tasks = [
        _task("t1", "Задача 1", status="Done", updated_at="2026-09-08T10:00:00"),
        _task("t2", "Задача 2", status="Done", updated_at="2026-08-01T10:00:00"),  # too old
    ]
    summary = board_view.build_weekly_summary(tasks, {}, now=NOW)

    assert summary.completed_titles == ("Задача 1",)
    assert summary.completed_total == 1


def test_blocked_task_becomes_a_plain_language_risk_naming_its_blocker():
    tasks = [
        _task("dep", "Зависимость", status="In Progress"),
        _task("blocked", "Заблокированная задача", status="Blocked", depends_on=["dep"]),
    ]
    summary = board_view.build_weekly_summary(tasks, {}, now=NOW)

    assert len(summary.risks) == 1
    risk = summary.risks[0]
    assert risk.title == "Заблокированная задача"
    assert "Зависимость" in risk.reason
    assert risk.severity == "Средний"


def test_high_priority_blocked_task_drives_overall_health_to_at_risk_and_needs_a_decision():
    tasks = [_task("t1", "Критичная задача", status="Blocked", priority="Critical")]
    summary = board_view.build_weekly_summary(tasks, {}, now=NOW)

    assert summary.overall_health == board_view.HEALTH_AT_RISK
    assert summary.risks[0].severity == "Высокий"
    assert any("Критичная задача" in item for item in summary.decisions_needed)


def test_low_priority_blocked_task_is_a_watch_not_a_hard_risk():
    tasks = [_task("t1", "Мелкая задача", status="Blocked", priority="Low")]
    summary = board_view.build_weekly_summary(tasks, {}, now=NOW)

    assert summary.overall_health == board_view.HEALTH_WATCH
    assert summary.decisions_needed == ()


def test_requires_attention_task_surfaces_as_a_risk_without_being_blocked():
    tasks = [_task("t1", "Ждёт решения", status="Review", launch_status="Requires Attention")]
    summary = board_view.build_weekly_summary(tasks, {}, now=NOW)

    assert len(summary.risks) == 1
    assert "решение" in summary.risks[0].reason.lower()


def test_done_task_is_never_a_risk_even_with_a_stale_launch_status():
    """Mirrors `read_model.task_snapshot`'s rule: a resolved task needs a real
    regression flag, not a stale `launch_status`, to count against it."""
    tasks = [_task("t1", "Готово", status="Done", launch_status="Requires Attention")]
    summary = board_view.build_weekly_summary(tasks, {}, now=NOW)

    assert summary.risks == ()
    assert summary.overall_health == board_view.HEALTH_ON_TRACK


def test_risks_are_capped_for_a_one_page_view_but_the_true_total_is_kept():
    tasks = [
        _task(f"t{i}", f"Задача {i}", status="Blocked", priority="Critical")
        for i in range(board_view._MAX_RISKS_SHOWN + 3)
    ]
    summary = board_view.build_weekly_summary(tasks, {}, now=NOW)

    assert len(summary.risks) == board_view._MAX_RISKS_SHOWN
    assert summary.risks_total == len(tasks)


def test_project_rows_reflect_status_file_and_per_project_counts():
    tasks = [
        _task("t1", "A", project="AIOS", status="In Progress"),
        _task("t2", "B", project="AIOS", status="Done"),
        _task("t3", "C", project="AICC", status="Blocked", priority="Critical"),
    ]
    summary = board_view.build_weekly_summary(tasks, {"AIOS": "В графике"}, now=NOW)

    by_id = {row.project_id: row for row in summary.projects}
    assert by_id["AIOS"].status_label == "В графике"
    assert by_id["AIOS"].active == 1
    assert by_id["AIOS"].done == 1
    assert by_id["AIOS"].health == board_view.HEALTH_ON_TRACK
    assert by_id["AICC"].health == board_view.HEALTH_AT_RISK
    assert "AML" not in by_id  # projects with no tasks are omitted


# --------------------------------------------------------------------------
# 2. Full page smoke test via AppTest.from_file (real app.py)
# --------------------------------------------------------------------------


def _at_on_board_view() -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "board_view"
    at.run()
    return at


def test_board_view_page_renders_and_nav_entry_exists():
    at = _at_on_board_view()
    assert not at.exception
    assert any(b.key == "nav_btn_board_view" for b in at.sidebar.button)


def test_board_view_page_shows_plain_language_summary_of_real_tasks(isolated_data_dir):
    tasks_repository.save_tasks(
        isolated_data_dir,
        [
            _task("dep", "Подготовка данных", status="In Progress"),
            _task("blocked", "Важный релиз", status="Blocked", priority="Critical", depends_on=["dep"]),
        ],
    )

    at = _at_on_board_view()
    assert not at.exception
    body = "".join(m.value for m in at.markdown)
    assert "Важный релиз" in body
    assert board_view.HEALTH_AT_RISK in body
    # No operator jargon leaks into the board member's page.
    for jargon in ("Blocker", "worktree", "Execution Center", "Kanban"):
        assert jargon not in body
