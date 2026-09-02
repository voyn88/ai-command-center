"""Streamlit AppTest coverage for the Agent Leaderboard section on the
"AI-агенты" page — a thin, read-only consumer of
`command_center.leaderboard.compute_leaderboard` over the same unified run
list (`runtime.runs_read.list_unified_runs`) the Runs page already reads.

Seeds runs directly through `runtime.db` (same pattern as
`tests/test_execution_center_ui.py`), setting `command=[...]` so
`runs_read._agent_from_command` derives a stable agent name from argv[0].
"""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center.runtime import api as runtime_api
from command_center.runtime import db as runtime_db

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _at_on_leaderboard_section(**extra_session_state) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "agents"
    at.session_state["agents_section"] = "Лидерборд"
    for key, value in extra_session_state.items():
        at.session_state[key] = value
    at.run()
    return at


def _seed_run(db_path, *, agent: str, state: str, sequence: int) -> None:
    task = runtime_db.create_task(db_path, project="AIOS", title=f"t{sequence}", task_type="review")
    session = runtime_db.create_session(
        db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/leaderboard-ui"
    )
    run = runtime_db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="review",
        repository_path="/tmp/leaderboard-ui",
        prompt="p",
        is_resume=False,
        command=[agent, "-p", "p"],
    )
    run = runtime_db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
    run = runtime_db.update_run_state(
        db_path,
        run["id"],
        expected_version=run["version"],
        new_state="RUNNING",
        fields={"started_at": f"2026-01-{sequence:02d}T00:00:00"},
    )
    runtime_db.update_run_state(
        db_path,
        run["id"],
        expected_version=run["version"],
        new_state=state,
        fields={"completed_at": f"2026-01-{sequence:02d}T00:05:00"},
    )


def test_leaderboard_section_renders_with_no_runs():
    at = _at_on_leaderboard_section()
    assert not at.exception
    assert any("Лидерборд агентов" in md.value for md in at.markdown)
    assert any("Пока нет запусков" in info.value for info in at.info)


def test_leaderboard_section_shows_tier_and_trend_for_a_rated_agent():
    db_path = runtime_api.ExecutionCenterAPI().db_path
    for i in range(1, 7):
        _seed_run(db_path, agent="claude", state="COMPLETED", sequence=i)

    at = _at_on_leaderboard_section()

    assert not at.exception
    text = "\n".join(md.value for md in at.markdown)
    assert "claude" in text
    assert "Top" in text
    assert any(m.label == "Успешность" and m.value == "100%" for m in at.metric)


def test_leaderboard_section_is_read_only():
    db_path = runtime_api.ExecutionCenterAPI().db_path
    _seed_run(db_path, agent="codex", state="FAILED", sequence=1)
    runs_before = runtime_db.list_runs(db_path, limit=100)

    at = _at_on_leaderboard_section()

    assert not at.exception
    runs_after = runtime_db.list_runs(db_path, limit=100)
    assert len(runs_before) == len(runs_after)
