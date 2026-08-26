"""The Execution Center's reconciliation entry point (VOYN-W0-AICC-SRV-09-READ-POOL).

`task`/`session`/`run`/`report` are the four Execution Center tables whose
SQLite-vs-PostgreSQL divergence check (`task_divergence`, `session_divergence`,
`run_divergence`, `report_divergence`) has existed since SRV-01B slices 10-12
but has never had a caller outside their own tests: nothing has fed the SQLite
authority's rows into them alongside a real, pooled read of the mirror. That is
what this module is — a caller, not a new mirror or a new comparison.

`run_event` is deliberately not here. Per `run_children_store`'s own docstring
it is "the highest-volume table in the schema" and reconciling it "materialises
the whole journal in one process" — its reconciliation entry point has to page,
which is `VOYN-W0-AICC-MIRROR-RECONCILE-STREAMING`'s job, not this one's.

Every mirror read goes through `PostgresTableMirror`'s own connection
resolution, which is `command_center.db.pool` unless a test injects a
`connection_factory` — the same pooled seam every other mirror read in this
package already uses, so this reconciliation pass costs one checkout per table
rather than one connection per row.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from command_center.db.execution_store import (
    PostgresSessionMirror,
    PostgresTaskMirror,
    session_divergence,
    task_divergence,
)
from command_center.db.run_children_store import PostgresReportMirror, report_divergence
from command_center.db.run_store import PostgresRunMirror, run_divergence
from command_center.runtime import db as runtime_db

__all__ = ["reconcile_execution_center"]


def reconcile_execution_center(
    db_path: Path, *, connection_factory: Any = None
) -> dict[str, list[dict]]:
    """Divergence for `task`/`session`/`run`/`report`, keyed by table name.

    `[]` for a table means the SQLite authority and its PostgreSQL mirror agree
    on every row it holds; a cutover of that table's reads is gated on this
    being `[]` (`mirror_support.divergence`'s own contract). `connection_factory`
    is for tests — production callers (`python -m command_center.db
    mirror-status`) leave it unset and get the process pool.
    """
    return {
        "task": task_divergence(
            runtime_db.list_tasks(db_path),
            PostgresTaskMirror(connection_factory=connection_factory),
        ),
        "session": session_divergence(
            runtime_db.list_sessions(db_path),
            PostgresSessionMirror(connection_factory=connection_factory),
        ),
        "run": run_divergence(
            runtime_db.list_runs(db_path),
            PostgresRunMirror(connection_factory=connection_factory),
        ),
        "report": report_divergence(
            runtime_db.list_reports(db_path),
            PostgresReportMirror(connection_factory=connection_factory),
        ),
    }
