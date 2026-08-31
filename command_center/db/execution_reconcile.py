"""Reconciliation across the SQLite authority and its PostgreSQL mirrors for
the execution family: `task`, `session`, `run`, `run_event`, `report`
(VOYN-W0-AICC-SRV-09-READ-POOL).

This is the first thing in the process that checks a pool connection out to
*read* a mirror back rather than to dual-write one — `command_center.runtime`
never imported `command_center.db.pool` before this, so the read path the
pool exists for had never actually been wired up or exercised.

Three defects closed three prior rounds of review, and the constraints they
left behind are why this module is shaped the way it is:

* **One connection, not five.** Each `Postgres*Mirror` checks out its own
  connection from `command_center.db.pool` by default, and instantiating five
  of them inside a caller's already-open `pool.connection()` block exhausts a
  pool sized `AICC_PG_POOL_MAX=1` before a single query runs. This module
  takes one `pg_connection_factory` and hands the *same* factory to every
  mirror, so a caller can pass `lambda: nullcontext(conn)` around its own
  already-checked-out connection and reconciling all five tables never asks
  the pool for a second one.

* **A snapshot, not a straddle.** The SQLite authority and each PostgreSQL
  mirror are read by separate queries at separate instants. A write that
  lands between them — authority commits, mirror write fails — can make a
  *stale* authority read agree with a mirror read taken after the failure,
  reporting clean when the two stores currently disagree (independent review
  demonstrated this with a task deleted from SQLite whose mirrored delete
  failed). Closed by re-reading the authority after the mirror snapshot and
  retrying whenever it moved: only an authority snapshot *confirmed
  unchanged* across the whole mirror read is ever compared against it.

* **No test double, no signature to get wrong.** Earlier attempts patched
  each mirror class with a stand-in whose constructor did not match
  `Postgres*Mirror.__init__(connection_factory=None)`, so the test suite could
  not fail the way production would (production calls
  `PostgresTaskMirror(pg_connection_factory)` — one positional argument, not a
  keyword). This module makes no such substitution itself; its tests drive
  the real mirror classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import command_center.runtime.db as runtime_db

__all__ = ["ExecutionReconciliationReport", "reconcile_execution_center"]

#: Table name -> the column SQLite is queried `ORDER BY`, matching each
#: table's own mirror key (`report`/`run_event` are not keyed by `id` the way
#: `task`/`session`/`run` are declared, but every table in this family in
#: fact still happens to expose `id` as an orderable column except `report`,
#: which is keyed by `run_id`).
_ORDER_BY = {
    "task": "id",
    "session": "id",
    "run": "id",
    "run_event": "id",
    "report": "run_id",
}


@dataclass(frozen=True)
class ExecutionReconciliationReport:
    """One reconciliation pass over the whole execution family.

    `stable` is False when the authority kept changing across every retry
    `reconcile_execution_center` was given — in which case `divergence` is
    empty and must **not** be read as "clean": there is no confirmed snapshot
    pair here to have compared.
    """

    divergence: dict[str, list[dict]]
    attempts: int
    stable: bool

    @property
    def clean(self) -> bool:
        return self.stable and not any(self.divergence.values())


def reconcile_execution_center(
    db_path: Path,
    pg_connection_factory: Callable[[], Any] | None = None,
    *,
    max_attempts: int = 5,
) -> ExecutionReconciliationReport:
    """Reconcile `task`/`session`/`run`/`run_event`/`report` against their
    PostgreSQL mirrors.

    `pg_connection_factory`, when given, is shared by every mirror this
    builds — pass the caller's own already-open connection wrapped in
    `nullcontext` so reconciling all five tables costs the pool exactly one
    checkout. `None` falls back to each mirror's own default
    (`command_center.db.pool.connection`), which is only safe when nothing
    else in the caller holds a pool connection open around this call.

    Never raises on a divergent or unreachable mirror — that is reported, not
    thrown (see `command_center.db.mirror_support.divergence`). Raises
    `ValueError` only for a nonsensical `max_attempts`.
    """
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts!r}")

    authority = _read_authority_snapshot(db_path)
    attempt = 0
    mirror_rows: dict[str, _Frozen] = {}
    while True:
        attempt += 1
        mirror_rows = _read_mirror_snapshot(pg_connection_factory)
        confirmed = _read_authority_snapshot(db_path)
        if confirmed == authority:
            break
        if attempt >= max_attempts:
            return ExecutionReconciliationReport(divergence={}, attempts=attempt, stable=False)
        authority = confirmed

    divergence = _compute_divergence(authority, mirror_rows)
    return ExecutionReconciliationReport(divergence=divergence, attempts=attempt, stable=True)


def _read_authority_snapshot(db_path: Path) -> dict[str, list[dict]]:
    """Every row of the execution family, from one SQLite read transaction.

    A plain `BEGIN` rather than `db.transaction`'s `BEGIN IMMEDIATE`: under
    WAL, the first read inside any transaction fixes a snapshot the rest of
    that transaction keeps seeing regardless of what commits afterward
    (sqlite.org's documented WAL reader isolation), and unlike `BEGIN
    IMMEDIATE` this takes no write lock — reconciliation must never be why a
    launch stalls. That snapshot is what makes the five tables below
    mutually consistent with each other; the *retry* around this function is
    what makes the whole read consistent with the mirror snapshot taken
    between two calls to it.
    """
    with runtime_db.connect(db_path) as conn:
        conn.execute("BEGIN")
        try:
            return {
                table: [
                    dict(row)
                    for row in conn.execute(
                        f"SELECT * FROM {table} ORDER BY {order_by}"
                    ).fetchall()
                ]
                for table, order_by in _ORDER_BY.items()
            }
        finally:
            conn.execute("COMMIT")


class _Frozen:
    """Replays one already-fetched `list_records()` result.

    `mirror_support.divergence` calls `mirror.list_records()` itself; handing
    it the *live* mirror here would re-read PostgreSQL after the authority's
    confirm read already happened, silently reopening the exact race this
    module exists to close. This replays what was captured during the one
    window that was confirmed stable, so the divergence check never performs
    I/O of its own.
    """

    def __init__(self, records: list[dict] | None = None, error: Exception | None = None) -> None:
        self._records = records
        self._error = error

    def list_records(self) -> list[dict]:
        if self._error is not None:
            raise self._error
        assert self._records is not None
        return self._records


def _read_mirror_snapshot(
    pg_connection_factory: Callable[[], Any] | None,
) -> dict[str, _Frozen]:
    from command_center.db.execution_store import PostgresSessionMirror, PostgresTaskMirror
    from command_center.db.run_children_store import (
        PostgresReportMirror,
        PostgresRunEventMirror,
    )
    from command_center.db.run_store import PostgresRunMirror

    mirrors: dict[str, Any] = {
        "task": PostgresTaskMirror(pg_connection_factory),
        "session": PostgresSessionMirror(pg_connection_factory),
        "run": PostgresRunMirror(pg_connection_factory),
        "run_event": PostgresRunEventMirror(pg_connection_factory),
        "report": PostgresReportMirror(pg_connection_factory),
    }
    frozen: dict[str, _Frozen] = {}
    for name, mirror in mirrors.items():
        try:
            frozen[name] = _Frozen(records=mirror.list_records())
        except Exception as exc:  # noqa: BLE001 - reported by `divergence`, never raised
            frozen[name] = _Frozen(error=exc)
    return frozen


def _compute_divergence(
    authority: dict[str, list[dict]], mirror_rows: dict[str, _Frozen]
) -> dict[str, list[dict]]:
    from command_center.db.execution_store import session_divergence, task_divergence
    from command_center.db.run_children_store import report_divergence, run_event_divergence
    from command_center.db.run_store import run_divergence

    return {
        "task": task_divergence(authority["task"], mirror_rows["task"]),
        "session": session_divergence(authority["session"], mirror_rows["session"]),
        "run": run_divergence(authority["run"], mirror_rows["run"]),
        "run_event": run_event_divergence(authority["run_event"], mirror_rows["run_event"]),
        "report": report_divergence(authority["report"], mirror_rows["report"]),
    }
