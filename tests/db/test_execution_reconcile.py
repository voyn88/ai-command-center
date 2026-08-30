"""`reconcile_execution_center` (VOYN-W0-AICC-SRV-09-READ-POOL).

The mirrors and their per-table `*_divergence` closures are already proved
against a real database by `test_execution_store.py`; what is specific here is
the thin combining function `command_center/runtime/execution_reconcile.py`
adds on top — that it reads the SQLite authority, feeds both tables to their
own divergence check, and reports clean only when both agree.
"""

from __future__ import annotations

from command_center.runtime.db import execution as exec_db
from command_center.runtime.execution_reconcile import reconcile_execution_center


def _patch(monkeypatch, factory) -> None:
    from command_center.db import execution_store
    from command_center.db.execution_store import (
        PostgresSessionMirror,
        PostgresTaskMirror,
    )

    for name, mirror in (
        ("PostgresTaskMirror", PostgresTaskMirror),
        ("PostgresSessionMirror", PostgresSessionMirror),
    ):
        monkeypatch.setattr(
            execution_store, name, lambda mirror=mirror: mirror(connection_factory=factory)
        )


def test_reconcile_reports_clean_after_a_normal_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    _patch(monkeypatch, pg_connection_factory)
    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)

    task = exec_db.create_task(db_path, project="AICC", title="mirror me", task_type="feature")
    exec_db.create_session(
        db_path, task_id=task["id"], project="AICC", repository_path="/tmp/repo"
    )

    report = reconcile_execution_center(db_path, pg_connection_factory)
    assert report.clean
    assert report.task_divergences == []
    assert report.session_divergences == []


def test_reconcile_reports_a_task_the_mirror_never_received(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """A lost mirror write must show up as a `task` divergence, not silence.

    Writes directly to the authority without going through the dual-write
    hook, which is exactly what a lost/failed mirror write looks like from
    reconciliation's point of view.
    """
    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    exec_db.create_task(db_path, project="AICC", title="never mirrored", task_type="feature")

    report = reconcile_execution_center(db_path, pg_connection_factory)
    assert report.clean is False
    assert len(report.task_divergences) == 1
    assert report.session_divergences == []
