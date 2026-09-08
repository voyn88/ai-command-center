"""AppTest coverage for the Agent Metrics dashboard page: confirms it renders
without exceptions, shows the normalized per-agent schema, and never mutates
runtime state (read-only, like the Portfolio Overview page)."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center.runtime import api as runtime_api
from command_center.runtime import db as runtime_db

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _at_agent_metrics() -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "agent_metrics"
    at.run()
    return at


def _run(db_path, *, command, state="COMPLETED"):
    task = runtime_db.create_task(db_path, project="AICC", title="Agent metrics task", task_type="implementation")
    session = runtime_db.create_session(db_path, task_id=task["id"], project="AICC", repository_path="/tmp/x")
    run = runtime_db.create_run(
        db_path, session_id=session["id"], task_id=task["id"], project="AICC",
        task_type="implementation", repository_path="/tmp/x", prompt="p", is_resume=False,
        expected_branch="task/x", command=command,
    )
    v = run["version"]
    for next_state in ("QUEUED", "RUNNING", state):
        run = runtime_db.update_run_state(db_path, run["id"], expected_version=v, new_state=next_state)
        v = run["version"]
    return run


def test_agent_metrics_page_renders_without_runs():
    at = _at_agent_metrics()
    assert not at.exception
    assert any("Нет запусков" in info.value for info in at.info)


def test_agent_metrics_page_shows_normalized_schema_per_agent():
    api = runtime_api.ExecutionCenterAPI()
    run = _run(api.db_path, command=["claude", "run"])
    runtime_db.create_completion(
        api.db_path, run_id=run["id"], task_id=run["task_id"], project="AICC",
        repository_path="/tmp/x", completion_state="COMPLETED", branch="task/x", base_branch="main",
        head_commit="abcdef1234567890", merge_mode="manual", merge_method="squash",
    )

    at = _at_agent_metrics()

    assert not at.exception
    body = " ".join(str(m.value) for m in at.markdown)
    assert "claude" in body
    captions = " ".join(c.value for c in at.caption)
    assert "1 запусков" in captions
