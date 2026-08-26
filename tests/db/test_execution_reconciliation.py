"""VOYN-W0-AICC-SRV-09-READ-POOL: the Execution Center's reconciliation entry
point — the first caller of `task_divergence`/`session_divergence`/
`run_divergence`/`report_divergence` outside their own tests.
"""

from __future__ import annotations

from pathlib import Path

from command_center.db.execution_reconciliation import reconcile_execution_center
from command_center.db.execution_store import PostgresSessionMirror, PostgresTaskMirror
from command_center.db.run_children_store import PostgresReportMirror
from command_center.db.run_store import PostgresRunMirror
from command_center.runtime.db import execution as exec_db


def _patch(monkeypatch, factory) -> None:
    """Point every write-side mirror at the test database.

    `_mirror_task`/`_mirror_session`/`_mirror_run`/`_mirror_report` each
    re-import their `PostgresXMirror` class from its home module at call time,
    so patching the module attribute (rather than a name already bound into
    this test file) is what a write picks up.
    """
    from command_center.db import execution_store, run_children_store, run_store

    for module, name, mirror in (
        (execution_store, "PostgresTaskMirror", PostgresTaskMirror),
        (execution_store, "PostgresSessionMirror", PostgresSessionMirror),
        (run_store, "PostgresRunMirror", PostgresRunMirror),
        (run_children_store, "PostgresReportMirror", PostgresReportMirror),
    ):
        monkeypatch.setattr(
            module, name, lambda mirror=mirror: mirror(connection_factory=factory)
        )


def _launch(db_path: Path) -> dict:
    task = exec_db.create_task(db_path, project="AICC", title="run me", task_type="feature")
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
        prompt="do the thing",
        is_resume=False,
        command=["claude", "--print"],
    )


def test_an_empty_database_reconciles_clean(pg_connection_factory, tmp_path) -> None:
    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)

    report = reconcile_execution_center(db_path, connection_factory=pg_connection_factory)

    assert report == {"task": [], "session": [], "run": [], "report": []}


def test_every_mirrored_table_reconciles_after_a_real_launch(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    _patch(monkeypatch, pg_connection_factory)
    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)

    run = _launch(db_path)
    exec_db.create_report(db_path, run["id"], path="/reports/AICC/run.md")

    report = reconcile_execution_center(db_path, connection_factory=pg_connection_factory)

    assert report == {"task": [], "session": [], "run": [], "report": []}


def test_a_row_the_mirror_never_received_is_reported(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """No `_patch`: writes land only in SQLite, so every table the authority
    holds is a row the mirror never received — the shape `divergence` reports
    when the mirror is simply behind, not merely unreachable."""
    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)

    task = exec_db.create_task(db_path, project="AICC", title="unmirrored", task_type="feature")

    report = reconcile_execution_center(db_path, connection_factory=pg_connection_factory)

    assert [entry["id"] for entry in report["task"]] == [task["id"]]
    assert report["session"] == [] and report["run"] == [] and report["report"] == []


def test_a_mirror_row_edited_out_from_under_the_authority_is_reported(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    _patch(monkeypatch, pg_connection_factory)
    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    task = exec_db.create_task(db_path, project="AICC", title="drift me", task_type="feature")

    with pg_connection_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE task SET title = %s WHERE id = %s", ("drifted", task["id"]))

    report = reconcile_execution_center(db_path, connection_factory=pg_connection_factory)

    assert len(report["task"]) == 1
    assert report["task"][0]["id"] == task["id"]
    assert report["task"][0]["fields"] == ["title"]
    assert report["session"] == [] and report["run"] == [] and report["report"] == []
