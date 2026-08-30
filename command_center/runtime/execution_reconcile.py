"""Reconcile the Execution Center's SQLite authority against its PostgreSQL
mirrors (VOYN-W0-AICC-SRV-09-READ-POOL).

`task` and `session` are the two tables `command_center/db/execution_store.py`
mirrors; `command_center/runtime/db/execution.py` dual-writes them on every
create and, until now, is the only thing in `command_center/runtime/` that
knows PostgreSQL exists at all — a router deciding whether reads go to SQLite
or the mirror needs a report saying the two agree *before* it can be trusted,
which is what this module produces.

`pool.py`'s connection pooling is the reason this is safe to run against a
live server: a per-call `psycopg.connect()` measured roughly 19.8x slower than
a pooled checkout, which fails the read-switch's own performance gate on every
query regardless of whether the read is correct. Routing this reconciliation
— and, later, the reads it clears — through `pool.connection()` is what makes
the comparison's own cost negligible.

The connection factory is always the caller's to supply or omit, never this
module's default to assume. A CLI that already holds one connection out of a
size-1 pool (`python -m command_center.db mirror-status`) must reuse it rather
than ask the pool for a second one it does not have — that exact mistake is
what sent this task back for rework (adversarial review of PR #438): the
report command ran inside its own `with pool.connection()`, and then handed
each mirror `None`, letting it check out a *second* connection the pool could
not supply.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["ExecutionReconcileReport", "reconcile_execution_center"]


@dataclass(frozen=True)
class ExecutionReconcileReport:
    """One reconciliation pass over `task` and `session`."""

    task_divergences: list[dict]
    session_divergences: list[dict]

    @property
    def clean(self) -> bool:
        """True when both tables agree — the state the read-switch is gated on."""
        return not self.task_divergences and not self.session_divergences


def reconcile_execution_center(
    db_path: Path, pg_connection_factory: Callable[[], Any] | None = None
) -> ExecutionReconcileReport:
    """Compare every `task`/`session` row in `db_path` against its mirror.

    `pg_connection_factory` is passed straight to `PostgresTaskMirror`/
    `PostgresSessionMirror`: omit it only when the caller holds no connection
    of its own and wants each mirror to check one out of the process-wide pool
    (`pool.open_pool()` must already have run). A caller that already has a
    connection — the `mirror-status` CLI command, any future caller checked
    out inside its own `with pool.connection()` — must pass a factory that
    returns *that* connection, or a size-1 pool deadlocks against itself.
    """
    from command_center.db.execution_store import (
        PostgresSessionMirror,
        PostgresTaskMirror,
        session_divergence,
        task_divergence,
    )
    from command_center.runtime import db as runtime_db

    task_mirror = PostgresTaskMirror(pg_connection_factory)
    session_mirror = PostgresSessionMirror(pg_connection_factory)

    tasks = runtime_db.list_tasks(db_path)
    sessions = runtime_db.list_sessions(db_path)

    return ExecutionReconcileReport(
        task_divergences=task_divergence(tasks, task_mirror),
        session_divergences=session_divergence(sessions, session_mirror),
    )
