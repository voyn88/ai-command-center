"""Slice 12: `run_event` and `report` — what the shared contract cannot cover.

The contract already enrols both: columns, key, references, round trip, FK
refusal, timestamps, `jsonb`, identity. What is specific here is that
`append_run_event` returns a *sequence number*, not a row — so the hook has to
assemble the stored record itself — and that `report` is keyed by `run_id`.
"""

from __future__ import annotations

import json
from pathlib import Path

from command_center.db.execution_store import PostgresSessionMirror, PostgresTaskMirror
from command_center.db.run_children_store import (
    PostgresReportMirror,
    PostgresRunEventMirror,
    report_divergence,
    run_event_divergence,
)
from command_center.db.run_store import PostgresRunMirror
from command_center.runtime.db import execution as exec_db


def _patch(monkeypatch, factory) -> None:
    from command_center.db import execution_store, run_children_store, run_store

    for module, name, mirror in (
        (execution_store, "PostgresTaskMirror", PostgresTaskMirror),
        (execution_store, "PostgresSessionMirror", PostgresSessionMirror),
        (run_store, "PostgresRunMirror", PostgresRunMirror),
        (run_children_store, "PostgresRunEventMirror", PostgresRunEventMirror),
        (run_children_store, "PostgresReportMirror", PostgresReportMirror),
    ):
        monkeypatch.setattr(
            module, name, lambda mirror=mirror: mirror(connection_factory=factory)
        )


def _launch(db_path: Path) -> dict:
    task = exec_db.create_task(db_path, project="AICC", title="t", task_type="feature")
    session = exec_db.create_session(
        db_path, task_id=task["id"], project="AICC", repository_path="/tmp/repo"
    )
    return exec_db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AICC",
        task_type="feature",
        repository_path="/tmp/repo",
        prompt="p",
        is_resume=False,
        command=["claude"],
    )


def test_the_journal_and_the_report_reconcile_after_every_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """Two runs, not one. A hook that only mirrors the run it was written
    against would still pass a single-run check — the second run's rows are
    the only thing that can catch that, which is why every stage reconciles
    both runs' children against the complete table rather than one run's
    slice of it."""
    _patch(monkeypatch, pg_connection_factory)
    events = PostgresRunEventMirror(connection_factory=pg_connection_factory)
    reports = PostgresReportMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    run = _launch(db_path)
    other_run = _launch(db_path)
    exec_db.append_run_event(db_path, other_run["id"], "stdout", {"line": "the other run's own event"})
    exec_db.create_report(db_path, other_run["id"], "/reports/AICC/other.md")

    def reconciled(stage: str) -> None:
        stored_events = [
            *exec_db.list_run_events_stored(db_path, run["id"]),
            *exec_db.list_run_events_stored(db_path, other_run["id"]),
        ]
        assert run_event_divergence(stored_events, events) == [], stage
        stored_reports = list(
            exec_db.get_reports_for_runs(db_path, [run["id"], other_run["id"]]).values()
        )
        assert report_divergence(stored_reports, reports) == [], stage

    exec_db.append_run_event(db_path, run["id"], "stdout", {"line": "hello"})
    reconciled("first event")
    exec_db.append_run_event(db_path, run["id"], "stderr", {"line": "oops"})
    reconciled("second event")
    exec_db.create_report(db_path, run["id"], "/reports/AICC/run.md")
    reconciled("report written")


def test_the_hook_assembles_the_stored_event_because_the_writer_returns_a_number(
    tmp_path, monkeypatch
) -> None:
    """`append_run_event` returns `seq`, an integer — there is no row for the
    hook to take. So the hook builds the stored record from what it inserted
    plus the id SQLite minted, and this pins that the id is real and the JSON
    is the column's text rather than the caller's dict."""
    from command_center.db import execution_store, run_children_store, run_store

    seen: list[dict] = []

    class Recording:
        def upsert(self, record: dict) -> None:
            seen.append(dict(record))

    for module, name in (
        (execution_store, "PostgresTaskMirror"),
        (execution_store, "PostgresSessionMirror"),
        (run_store, "PostgresRunMirror"),
        (run_children_store, "PostgresRunEventMirror"),
        (run_children_store, "PostgresReportMirror"),
    ):
        monkeypatch.setattr(module, name, lambda: Recording())

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    run = _launch(db_path)
    seq = exec_db.append_run_event(db_path, run["id"], "stdout", {"line": "hello"})

    event = seen[-1]
    assert isinstance(seq, int) and seq == 1
    assert isinstance(event["id"], int) and event["id"] >= 1
    assert json.loads(event["payload_json"]) == {"line": "hello"}


def test_a_mirror_failure_cannot_break_the_journal(tmp_path, monkeypatch) -> None:
    from command_center.db import execution_store, run_children_store, run_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

        def delete_task(self, task_id: str) -> None:
            raise RuntimeError("postgres is down")

    for module, name in (
        (execution_store, "PostgresTaskMirror"),
        (execution_store, "PostgresSessionMirror"),
        (run_store, "PostgresRunMirror"),
        (run_children_store, "PostgresRunEventMirror"),
        (run_children_store, "PostgresReportMirror"),
    ):
        monkeypatch.setattr(module, name, lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    run = _launch(db_path)

    assert exec_db.append_run_event(db_path, run["id"], "stdout", {"line": "x"}) == 1
    assert exec_db.create_report(db_path, run["id"], "/r.md")["path"] == "/r.md"
    assert len(exec_db.list_run_events(db_path, run["id"])) == 1
