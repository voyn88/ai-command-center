"""Copy pre-existing SQLite rows into their PostgreSQL mirrors (VOYN-W0-AICC-SRV-07).

Every dual-write hook mirrors a row at the moment the authority writes it —
which says nothing about the rows already sitting in SQLite before the hook
existed. This module is the one-time (but idempotent, so safe to re-run) catch
up: read each mirrored table from the authority in its *stored* shape, and
run it through the exact `upsert()` the live dual-write path uses, so a row
backfilled here and a row mirrored live are indistinguishable afterwards.

Tables run in dependency order — parents before children — because the target
refuses a child whose foreign key parent it does not have yet, and this reuses
`MirroredTable.references`, the same declaration a live dual-write already
follows and `test_declared_references_match_the_schema` already checks against
the DDL. Nothing here re-derives that order by hand.

**One row's failure must not cost another's.** A malformed row is real —
SQLite's stored text long predates every constraint the mirror declares — and
the backfill's whole reason to exist is to report those rows rather than
silently skip them. But this deliberately reuses one PostgreSQL connection for
the entire run (opening one per row would spend more time connecting than
writing), and a bare `try/except` around each `upsert()` is not enough to make
that safe: PostgreSQL aborts the *whole* transaction on the first error, and
every statement after it on that connection raises
`InFailedSqlTransactionError` until something rolls back — turning one bad row
into every later row failing too, and the run crashing outright once
`resync_identity()` (or reconciliation) hits that poisoned connection outside
any per-row handler. `conn.transaction()` around each row is a `SAVEPOINT`
when the connection is already inside a transaction (autocommit off) and a
standalone transaction when it is not (autocommit on, `pool.connection()`'s
default) — either way, only that row's work rolls back on failure, and the
connection is exactly as usable for the next row as it was before. Found by
independent review, which asked for the fix in those words: "a transaction/
savepoint per row" (VOYN-W0-AICC-SRV-07a-RETRY-REM's REJECT on PR #475).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from command_center.db.mirror_registry import mirror_classes
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror, divergence_against

__all__ = [
    "BackfillReport",
    "TableReport",
    "UnknownTablesError",
    "resolve_tables",
    "run_historical_backfill",
]


class UnknownTablesError(ValueError):
    """Raised when `--table` names something no mirror declares.

    A plain `wanted - mirrors.keys()` check, but the failure mode it replaces
    is silent: without it, an operator typo (`--table taks`) matched nothing,
    the run "succeeded" having copied and reconciled zero rows, and the CLI
    exited 0 -- indistinguishable from a deliberate, successful, focused
    backfill of one table. Raised before either database is touched, so a
    typo costs nothing but the argument parse.
    """

    def __init__(self, unknown: Iterable[str], known: Iterable[str]) -> None:
        self.unknown = tuple(sorted(unknown))
        self.known = tuple(sorted(known))
        super().__init__(
            f"unknown mirrored table(s): {', '.join(self.unknown)}. "
            f"Mirrored tables are: {', '.join(self.known)}"
        )


@dataclass
class TableReport:
    """What happened to one table's rows."""

    table: str
    rows_read: int = 0
    rows_written: int = 0
    #: `(key, "ExceptionType: message")` for every row that did not upsert.
    errors: list[tuple[Any, str]] = field(default_factory=list)
    #: `divergence_against`'s report comparing the read rows to the mirror
    #: afterwards -- non-empty means something other than this run's own
    #: errors disagrees between the two sides.
    divergence: list[dict] = field(default_factory=list)
    identity_resynced_to: int | None = None
    resync_error: str | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.resync_error is None


@dataclass
class BackfillReport:
    """One run across every table it touched, in the order it touched them."""

    tables: list[TableReport] = field(default_factory=list)

    @property
    def total_rows_written(self) -> int:
        return sum(t.rows_written for t in self.tables)

    @property
    def total_errors(self) -> int:
        return sum(len(t.errors) + (1 if t.resync_error else 0) for t in self.tables)


def _dependency_order(specs: dict[str, MirroredTable]) -> list[str]:
    """Every declared table, parents before children (Kahn's algorithm).

    Sorted within each wave so the order is deterministic run to run --
    otherwise two operators backfilling the same database could see their
    error reports list the same rows in a different sequence, which is a
    needless obstacle to diffing one run's report against another's.
    """
    remaining = dict(specs)
    ordered: list[str] = []
    while remaining:
        ready = sorted(
            table
            for table, spec in remaining.items()
            if all(parent not in remaining for parent in spec.references.values())
        )
        if not ready:
            raise ValueError(f"circular mirror dependency among: {sorted(remaining)}")
        ordered.extend(ready)
        for table in ready:
            del remaining[table]
    return ordered


def resolve_tables(
    wanted: set[str] | None = None,
) -> list[tuple[str, type[PostgresTableMirror]]]:
    """Every mirrored table `wanted` names, in dependency order.

    `wanted=None` (the default) means every declared mirror. A `wanted` naming
    anything `mirror_classes()` does not declare raises `UnknownTablesError`
    rather than quietly running the tables it does recognise -- see that
    class's docstring for the failure this refuses to reproduce.
    """
    classes = mirror_classes()
    specs = {table: cls.spec for table, (cls, _module) in classes.items()}
    if wanted is not None:
        unknown = set(wanted) - specs.keys()
        if unknown:
            raise UnknownTablesError(unknown, specs.keys())
    order = _dependency_order(specs)
    if wanted is not None:
        order = [table for table in order if table in wanted]
    return [(table, classes[table][0]) for table in order]


def _read_stored_rows(sqlite_conn: sqlite3.Connection, spec: MirroredTable) -> list[dict]:
    """Every row of `spec.table`, in SQLite's own stored shape.

    The same shape a live dual-write hands `upsert()` -- raw ISO text, JSON
    text, 0/1 flags -- because `ColumnCodec.to_column` is what converts it, and
    handing it anything already converted would exercise a path no dual-write
    ever takes. Selected by name in `spec.columns` order rather than `SELECT
    *`, so the result maps onto that order even if the two schemas' physical
    column order has ever drifted (it should not, but nothing enforces it at
    the SQLite side the way `test_the_column_list_matches_the_accepted_schema`
    does for PostgreSQL).
    """
    columns = ", ".join(spec.columns)
    keys = ", ".join(spec.key_columns)
    cursor = sqlite_conn.execute(f"SELECT {columns} FROM {spec.table} ORDER BY {keys}")
    return [dict(zip(spec.columns, row, strict=True)) for row in cursor.fetchall()]


def run_historical_backfill(
    sqlite_path: str | Path,
    connection_factory: Callable[[], Any],
    *,
    tables: set[str] | None = None,
) -> BackfillReport:
    """Backfill `tables` (default: every mirrored table) from `sqlite_path`.

    `connection_factory` is shaped like `command_center.db.pool.connection` --
    a context manager yielding one PostgreSQL connection -- and is opened
    exactly once for the whole run, then handed to every table's mirror and
    used directly here for the per-row transaction wrapper described in the
    module docstring. Idempotent: `upsert()` is what a re-run relies on, and
    running this twice against the same source reports the same rows written,
    not a second copy of them.
    """
    resolved = resolve_tables(tables)  # UnknownTablesError before either db opens

    sqlite_conn = sqlite3.connect(str(sqlite_path))
    try:
        report = BackfillReport()
        with connection_factory() as conn:
            for table, mirror_cls in resolved:
                mirror = mirror_cls(connection_factory)
                spec = mirror.spec
                table_report = TableReport(table=table)
                rows = _read_stored_rows(sqlite_conn, spec)
                table_report.rows_read = len(rows)

                for row in rows:
                    key = tuple(row.get(column) for column in spec.key_columns)
                    try:
                        with conn.transaction():
                            mirror.upsert(row)
                    except Exception as exc:  # noqa: BLE001 - reported per row, run continues
                        table_report.errors.append((key, f"{type(exc).__name__}: {exc}"))
                        continue
                    table_report.rows_written += 1

                if spec.identity:
                    try:
                        with conn.transaction():
                            table_report.identity_resynced_to = mirror.resync_identity()
                    except Exception as exc:  # noqa: BLE001 - reported, not a crash
                        table_report.resync_error = f"{type(exc).__name__}: {exc}"

                table_report.divergence = divergence_against(spec)(rows, mirror)
                report.tables.append(table_report)
        return report
    finally:
        sqlite_conn.close()
