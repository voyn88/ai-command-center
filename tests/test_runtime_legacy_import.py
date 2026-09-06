"""v1.2 compatibility: importing `data/runs.jsonl` records into the v2 SQLite
store must never touch the real JSONL file and must be idempotent.
"""

from __future__ import annotations

import pytest

from command_center.runtime import db, legacy_import, reports


def _legacy_run(**overrides) -> dict:
    base = {
        "id": "legacy-run-1",
        "project": "AIOS",
        "task_id": "legacy-task-1",
        "agent": "claude_code",
        "task_type": "implementation",
        "repository_path": "/tmp/legacy-repo",
        "prompt": "do the legacy thing",
        "status": "completed",
        "pre_run": {"branch": "main", "head": "abc123", "status_summary": "(чисто)", "is_git_repo": True},
        "post_run": {"branch": "main", "head": "def456", "status_summary": "(чисто)"},
        "started_at": "2026-01-01T10:00:00",
        "completed_at": "2026-01-01T10:05:00",
        "duration_seconds": 300.0,
        "exit_code": 0,
        "stdout": "some legacy stdout",
        "stderr": "",
        "report_path": "reports/AIOS/legacy-report.md",
        "created_at": "2026-01-01T10:00:00",
    }
    base.update(overrides)
    return base


def test_import_creates_task_session_run_and_report(tmp_path):
    db_path = tmp_path / "runtime.db"
    legacy = [_legacy_run()]

    created = legacy_import.import_legacy_runs(db_path, legacy_runs=legacy)

    assert len(created) == 1
    run = db.get_run(db_path, created[0])
    assert run["state"] == "COMPLETED"
    assert run["project"] == "AIOS"
    assert run["exit_code"] == 0

    session = db.get_session(db_path, run["session_id"])
    assert session["legacy_run_id"] == "legacy-run-1"

    task = db.get_task(db_path, run["task_id"])
    assert task["legacy_task_id"] == "legacy-task-1"

    report = db.get_report(db_path, run["id"])
    assert report["path"] != "reports/AIOS/legacy-report.md"
    assert reports.resolve_report_path(report["path"]).is_file()


def test_import_is_idempotent(tmp_path):
    db_path = tmp_path / "runtime.db"
    legacy = [_legacy_run()]

    first = legacy_import.import_legacy_runs(db_path, legacy_runs=legacy)
    second = legacy_import.import_legacy_runs(db_path, legacy_runs=legacy)

    assert len(first) == 1
    assert second == []
    assert len(db.list_runs(db_path)) == 1


def test_import_never_writes_to_the_real_v1_2_runs_jsonl(tmp_path, monkeypatch):
    """This is the non-destructive guarantee: import must not call anything
    that appends/rewrites `data/runs.jsonl` — it only ever reads from the
    list handed to it (or, by default, `agent_runner.load_runs()`, which
    itself only reads)."""
    from command_center import storage

    def fail_if_called(*args, **kwargs):
        raise AssertionError("legacy import must never write to data/runs.jsonl")

    monkeypatch.setattr(storage, "append_jsonl", fail_if_called)

    db_path = tmp_path / "runtime.db"
    legacy_import.import_legacy_runs(db_path, legacy_runs=[_legacy_run()])


@pytest.mark.parametrize(
    "legacy_status,expected_state",
    [
        ("completed", "COMPLETED"),
        ("failed", "FAILED"),
        ("timed_out", "FAILED"),
        ("cancelled", "CANCELLED"),
        ("queued", "INTERRUPTED"),
        ("running", "INTERRUPTED"),
        ("something_unrecognized", "UNKNOWN"),
    ],
)
def test_legacy_status_mapping(tmp_path, legacy_status, expected_state):
    db_path = tmp_path / "runtime.db"
    legacy = [_legacy_run(id=f"run-{legacy_status}", status=legacy_status)]
    created = legacy_import.import_legacy_runs(db_path, legacy_runs=legacy)
    run = db.get_run(db_path, created[0])
    assert run["state"] == expected_state


def test_import_preserves_full_stdout_as_an_event_without_truncation(tmp_path):
    db_path = tmp_path / "runtime.db"
    huge_stdout = "Y" * 100_000
    legacy = [_legacy_run(stdout=huge_stdout)]
    created = legacy_import.import_legacy_runs(db_path, legacy_runs=legacy)
    events = db.list_run_events(db_path, created[0])
    stdout_events = [e for e in events if e["event_type"] == "legacy_stdout"]
    assert len(stdout_events) == 1
    assert stdout_events[0]["payload"]["stdout"] == huge_stdout


def test_import_multiple_legacy_runs_creates_separate_sessions(tmp_path):
    db_path = tmp_path / "runtime.db"
    legacy = [_legacy_run(id="run-a"), _legacy_run(id="run-b", task_id="legacy-task-1")]
    created = legacy_import.import_legacy_runs(db_path, legacy_runs=legacy)
    assert len(created) == 2
    # Same legacy task_id -> same v2 task, but distinct sessions (v1.2 had no
    # session concept, so each legacy run becomes its own session).
    run_a = db.get_run(db_path, created[0])
    run_b = db.get_run(db_path, created[1])
    assert run_a["task_id"] == run_b["task_id"]
    assert run_a["session_id"] != run_b["session_id"]


def test_import_handles_run_with_no_task_id(tmp_path):
    db_path = tmp_path / "runtime.db"
    legacy = [_legacy_run(task_id=None)]
    created = legacy_import.import_legacy_runs(db_path, legacy_runs=legacy)
    assert len(created) == 1
    run = db.get_run(db_path, created[0])
    assert db.get_task(db_path, run["task_id"]) is not None


def test_import_with_no_report_path_generates_durable_report(tmp_path):
    db_path = tmp_path / "runtime.db"
    legacy = [_legacy_run(report_path=None)]
    created = legacy_import.import_legacy_runs(db_path, legacy_runs=legacy)
    report = db.get_report(db_path, created[0])
    assert report is not None
    assert db.get_run(db_path, created[0])["finalized_at"] is not None


def test_import_defaults_to_agent_runner_load_runs(tmp_path, monkeypatch):
    from command_center import agent_runner

    monkeypatch.setattr(agent_runner, "load_runs", lambda: [_legacy_run(id="from-default-loader")])
    db_path = tmp_path / "runtime.db"
    created = legacy_import.import_legacy_runs(db_path)
    assert len(created) == 1
    run = db.get_run(db_path, created[0])
    session = db.get_session(db_path, run["session_id"])
    assert session["legacy_run_id"] == "from-default-loader"
