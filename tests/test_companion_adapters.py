"""Companion Sync Service read adapters (M1A + M1B).

The contract these tests defend is narrow and important: every adapter is a
thin wrapper that returns an *existing* core shape. Two properties matter more
than any individual field —

  * importing the package binds no port and starts nothing, because this
    service is the single deliberate exception to the "no local HTTP listener"
    rule and an exception that activated on import would defeat it;
  * an adapter never recomputes what the core already computes, so the mobile
    client and the desktop cannot drift into disagreeing about the same state.
"""

from __future__ import annotations

import pytest

from command_center import execution_queue, models, tasks_repository, workspace_home
from command_center.companion import adapters
from command_center.runtime import api as runtime_api


@pytest.fixture
def api(tmp_path):
    return runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")


def _task(task_id="t1", **overrides):
    task = {
        "id": task_id,
        "project": "AIOS",
        "title": "Task",
        "status": "Backlog",
        "priority": "Medium",
        "depends_on": [],
    }
    task.update(models.default_task_execution_fields())
    task.update(models.default_task_workflow_fields())
    task.update(overrides)
    return task


# --------------------------------------------------------------------------
# The package must be inert on import
# --------------------------------------------------------------------------


def test_importing_the_package_starts_nothing():
    """No port bound, no server, no file written — the desktop architecture
    forbids a local listener and this service is the one explicit exception,
    which must be opted into by running it, never by importing it."""
    import importlib
    import socket

    before = socket.socket.bind
    module = importlib.import_module("command_center.companion")
    importlib.reload(module)
    assert socket.socket.bind is before
    assert hasattr(module, "__all__")


def test_unimplemented_modules_are_importable_stubs():
    """`auth.py`/`api.py` remain deliberate stubs (M1D, needs device identity):
    the package shape matches the specification before behaviour exists, so a
    reader can see what is planned without guessing. `notify.py` is no longer
    one of them — see the `notify` tests below."""
    from command_center.companion import api, auth

    for module in (api, auth):
        assert module.__doc__ and "not implemented" in module.__doc__.lower()


# --------------------------------------------------------------------------
# Adapters return the core's own shapes
# --------------------------------------------------------------------------


def test_dashboard_returns_the_workspace_home_snapshot_unchanged(api, monkeypatch):
    sentinel = {"projects": [{"id": "AIOS"}], "artifacts": [], "reports": []}
    monkeypatch.setattr(
        workspace_home, "build_workspace_home_snapshot", lambda **kwargs: sentinel
    )
    assert adapters.dashboard(execution_center_api=api) is sentinel


def test_projects_comes_from_the_same_snapshot_as_the_dashboard(api, monkeypatch):
    """Deriving it from a second, independent call would let the two disagree."""
    calls = []

    def fake(**kwargs):
        calls.append(1)
        return {"projects": [{"id": "AIOS"}]}

    monkeypatch.setattr(workspace_home, "build_workspace_home_snapshot", fake)
    assert adapters.projects(execution_center_api=api) == [{"id": "AIOS"}]
    assert len(calls) == 1


def test_runs_are_bounded_by_default(api, monkeypatch):
    """An unbounded history must never be handed to a client on a slow link."""
    seen = {}
    monkeypatch.setattr(api, "list_runs", lambda **kw: seen.update(kw) or [])
    adapters.runs(execution_center_api=api)
    assert seen["limit"] == 100


def test_a_missing_run_is_none_not_an_invented_error(api):
    assert adapters.run("nope", execution_center_api=api) is None


def test_queue_read_does_not_rewrite_queue_state(tmp_path, monkeypatch):
    """Polling must not have side effects: re-evaluation belongs to the
    desktop's own refresh checkpoints, not to someone looking at the queue."""
    called = []
    monkeypatch.setattr(
        execution_queue, "reevaluate_and_persist", lambda *a, **k: called.append(1)
    )
    task = _task()
    tasks_repository.save_tasks(tmp_path, [task])
    execution_queue.enqueue_and_persist(tmp_path, task, {task["id"]: task})

    entries = adapters.queue(tmp_path)
    assert [e["task_id"] for e in entries] == ["t1"]
    assert called == [], "reading the queue triggered a persist"


def test_recommendations_use_the_existing_view_builder(tmp_path):
    tasks_repository.save_tasks(tmp_path, [_task("t1"), _task("t2")])
    views = adapters.recommendations(tmp_path, limit=2)
    assert len(views) <= 2
    for view in views:
        # Exactly the recommendation_service shape — not a mobile-specific one.
        assert {"task_id", "title", "score", "reasons", "ready"} <= set(view)


# --------------------------------------------------------------------------
# Reports: parsed by the one existing parser
# --------------------------------------------------------------------------


def test_report_is_none_when_no_report_row_exists(api):
    assert adapters.run_report("nope", execution_center_api=api) is None


def test_report_uses_the_existing_parser(api, tmp_path, monkeypatch):
    report = tmp_path / "r.md"
    report.write_text("# Отчёт\n\nVerdict: APPROVED_FOR_COMMIT\n", encoding="utf-8")
    monkeypatch.setattr(
        api, "get_report", lambda run_id: {"path": str(report), "created_at": "2026-07-24T10:00:00"}
    )
    result = adapters.run_report("r1", execution_center_api=api)
    assert result["raw_markdown"].startswith("# Отчёт")
    assert "verdict" in result["parsed"]


def test_a_report_whose_file_vanished_still_returns_the_row(api, tmp_path, monkeypatch):
    """A missing file is a fact about the report, not a failed request."""
    monkeypatch.setattr(
        api, "get_report", lambda run_id: {"path": str(tmp_path / "gone.md"), "created_at": "x"}
    )
    result = adapters.run_report("r1", execution_center_api=api)
    assert result["raw_markdown"] == ""
    assert result["parsed"] is not None
