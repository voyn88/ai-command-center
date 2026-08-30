"""Read-side reconciliation of the `task`/`session` family against its
PostgreSQL mirror (VOYN-W0-AICC-SRV-09-READ-POOL).

Every mirrored *write* already reaches PostgreSQL through the process pool --
each `PostgresTableMirror` subclass falls back to `command_center.db.pool`
when its caller supplies no connection of its own (see `table_mirror.py`).
What had no caller at all was a *read*: nothing combined the SQLite authority
(`command_center.runtime.db`) with the PostgreSQL mirror
(`command_center.db.execution_store`) through the pool to ask whether the two
sides actually agree. This module is that seam, consumed by the CLI's
`mirror-status` command (`command_center/db/cli.py`).

Deliberately not under `command_center/runtime/`: that package is the frozen
legacy engine ADR-0008 closed to new files (`tests/architecture/
test_aios_boundary_fitness.py`), and this module owns no persistence of its
own -- it delegates every read to the SQLite authority's own reader functions
and to the PostgreSQL mirror classes, exactly the "connection someone else
opened" case the boundary scanner's docstring calls delegation rather than
ownership. `command_center/db/` is where every other mirror/reconciliation
module already lives.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

__all__ = ["ExecutionReconcileReport", "reconcile_execution_center"]


@dataclass(frozen=True)
class ExecutionReconcileReport:
    """One reconciliation pass over `task` and `session`.

    Each divergence list is exactly `mirror_support.divergence`'s own report
    shape (`[]` when the two stores agree), kept separate per table rather
    than concatenated -- `mirror-status` names which table is behind, and a
    caller that only cares about one of the two tables should not have to
    filter the other back out.
    """

    task_divergence: list[dict]
    session_divergence: list[dict]

    @property
    def clean(self) -> bool:
        return not self.task_divergence and not self.session_divergence


def reconcile_execution_center(
    db_path: Path,
    connection_factory: Callable[[], Any] | None = None,
) -> ExecutionReconcileReport:
    """Compare the SQLite `task`/`session` authority against its PostgreSQL mirror.

    `connection_factory`, when given, is used for both mirrors unchanged --
    a caller that already holds one checked-out connection (the CLI's
    `mirror-status` command, which runs inside its own outer
    `pool.connection()` block) passes `lambda: nullcontext(conn)` so this
    never checks out a second connection of its own; checking out a second
    connection from a pool sized `AICC_PG_POOL_MAX=1` is exactly what left an
    earlier `mirror-status` unable to complete (independent-review finding --
    see the CLI wiring). Left `None`, this opens its own checkout against the
    process pool -- the same fallback every write mirror already has,
    extended here to the read path for a caller with no connection of its own
    to lend.

    Read-only: neither this function nor the mirrors it constructs write to
    either store.
    """
    from command_center.db.execution_store import (
        PostgresSessionMirror,
        PostgresTaskMirror,
        session_divergence,
        task_divergence,
    )
    from command_center.runtime.db import execution as execution_db

    if connection_factory is None:
        from command_center.db import pool

        connection_factory = pool.connection

    tasks = PostgresTaskMirror(connection_factory=connection_factory)
    sessions = PostgresSessionMirror(connection_factory=connection_factory)

    return ExecutionReconcileReport(
        task_divergence=task_divergence(execution_db.list_tasks(db_path), tasks),
        session_divergence=session_divergence(execution_db.list_sessions(db_path), sessions),
    )
