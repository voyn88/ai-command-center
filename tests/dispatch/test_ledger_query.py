"""Repository-tier tests for the real ledger feed (`dispatch.ledger_query`).

Hermetic, matching `tests/test_runtime_db_batch_reads.py`: each test migrates
a brand-new SQLite file under `tmp_path` and drives the real db repository
functions against it directly -- no service, no HTTP, no e2e fixtures.
"""

from __future__ import annotations

import json

from command_center.dispatch.ledger_query import list_ledger_entries
from command_center.runtime import db


def _finished_run(db_path, *, project="AICC", task_type="implementation",
                   command=("claude", "-p", "x"), state="COMPLETED"):
    task = db.create_task(db_path, project=project, title="t", task_type=task_type)
    session = db.create_session(
        db_path, task_id=task["id"], project=project, repository_path="/tmp/w"
    )
    run = db.create_run(
        db_path, session_id=session["id"], task_id=task["id"], project=project,
        task_type=task_type, repository_path="/tmp/w", prompt="p", is_resume=False,
        command=list(command) if command is not None else None,
    )
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                "UPDATE run SET state = ?, started_at = ?, completed_at = ? WHERE id = ?",
                (state, "2026-09-01T10:00:00", "2026-09-01T10:05:00", run["id"]),
            )
    return task, run


def test_only_terminal_runs_are_read(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    _finished_run(db_path, state="COMPLETED")
    still_running_task = db.create_task(
        db_path, project="AICC", title="running", task_type="implementation"
    )
    still_running_session = db.create_session(
        db_path, task_id=still_running_task["id"], project="AICC", repository_path="/tmp/w"
    )
    db.create_run(
        db_path, session_id=still_running_session["id"], task_id=still_running_task["id"],
        project="AICC", task_type="implementation", repository_path="/tmp/w",
        prompt="p", is_resume=False, command=["claude"],
    )  # left in its default in-flight state

    entries = list_ledger_entries(db_path)
    assert len(entries) == 1


def test_accepted_entry_end_to_end(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    task, run = _finished_run(db_path, project="AICC", task_type="review")
    completion = db.create_completion(
        db_path, run_id=run["id"], task_id=task["id"], project="AICC",
        repository_path="/tmp/w", completion_state="MERGED",
        head_commit="deadbeef", last_reason_code="TARGET_VERIFIED",
    )
    db.update_run_provenance(db_path, run["id"], fields={"accepted_sha": "deadbeef"})
    db.append_run_event(
        db_path, run["id"], "stream_event",
        {"type": "result", "total_cost_usd": 0.75},
    )
    db.update_completion(
        db_path, run["id"], expected_version=completion["version"],
        fields={"review_verdict": "approved"},
    )

    entries = list_ledger_entries(db_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.executor_id == "claude"
    assert entry.task_class == "AICC:review"
    assert entry.merged_sha == "deadbeef"
    assert entry.review_verdict == "approved"
    assert entry.accepted is True
    assert entry.cost_usd == 0.75
    assert entry.duration_seconds == 300.0
    assert entry.outcome == "COMPLETED"


def test_run_without_completion_or_provenance_is_attempted_not_accepted(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    _finished_run(db_path, state="FAILED")

    entries = list_ledger_entries(db_path)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.merged_sha is None
    assert entry.review_verdict is None
    assert entry.accepted is False
    assert entry.cost_usd == 0.0
    assert entry.outcome == "FAILED"


def test_costs_are_summed_per_run_not_shared_across_runs(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    _task_a, run_a = _finished_run(db_path, command=("claude",))
    _task_b, run_b = _finished_run(db_path, command=("codex",))
    db.append_run_event(
        db_path, run_a["id"], "stream_event", {"type": "result", "total_cost_usd": 1.0}
    )
    db.append_run_event(
        db_path, run_a["id"], "stream_event", {"type": "result", "total_cost_usd": 0.5}
    )
    db.append_run_event(
        db_path, run_b["id"], "stream_event", {"type": "result", "total_cost_usd": 9.0}
    )

    by_executor = {e.executor_id: e.cost_usd for e in list_ledger_entries(db_path)}
    assert by_executor["claude"] == 1.5
    assert by_executor["codex"] == 9.0


def test_limit_bounds_how_many_runs_are_read(tmp_path):
    db_path = tmp_path / "runtime.db"
    db.migrate(db_path)
    for _ in range(3):
        _finished_run(db_path)

    assert len(list_ledger_entries(db_path)) == 3
    assert len(list_ledger_entries(db_path, limit=1)) == 1
