"""The historical backfill (VOYN-W0-AICC-SRV-07): run the mirrors once, on old rows.

VOYN-W0-AICC-SRV-01B built the write path. Thirty-two tables each declare a
`PostgresTableMirror` whose `upsert()` is idempotent by design — "the backfill
is expected to run more than once" is `record_mirror.RecordMirror`'s own
docstring, written before this module existed to be the run it was describing.
What SRV-01B did not build is the run itself: every row a table already held
*before* dual-write started needs the same `upsert()` a live write gets, once,
in an order that never hands a child to PostgreSQL before its parent.

This module is that walk and nothing else. It invents no new conversion: a row
read out of SQLite in its stored shape — raw ISO text, raw `0`/`1`, raw JSON
text — is exactly what `ColumnCodec.to_column` already expects, because that is
what a live dual-write hands it too. Reusing the declared mirror is also why
this carries no time-zone flag of its own: `mirror_support.to_instant` attaches
the *process's own* local zone to a naive timestamp, the same rule a live
dual-write runs under, so the backfill must run on a host in the SQLite
writer's zone for the same reason a dual-write hook must (see
`mirror_support.to_instant`, and `VOYN-W0-AICC-TZ-AWARE-TIMESTAMPS` for the
open gap that is not this task's to close).

`queue_entry` is deliberately absent: it mirrors by whole-list replacement
under its own contract (`command_center/queue_store.py`), not
`PostgresTableMirror`, and stays outside this gate for the reason
`command_center/db/roles.py` already gives it.

Bounded and idempotent. Bounded because each table's rows are read once, at
the moment the pass reaches that table — a snapshot, not a tailing cursor — so
the run has a definite end. Idempotent because every write is `upsert()`, keyed
by the table's own primary key: a rerun after a crash, a partial failure, or an
operator's second pass repeats no work and loses none.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from command_center.db.mirror_registry import mirror_classes
from command_center.db.table_mirror import PostgresTableMirror

__all__ = ["BackfillReport", "TableBackfillReport", "run_historical_backfill", "wave_order"]


@dataclass(frozen=True)
class TableBackfillReport:
    """What happened to one table's historical rows."""

    table: str
    #: Rows read from the SQLite authority.
    source_rows: int
    #: Rows successfully upserted (may be less than `source_rows` on error).
    upserted: int
    #: One entry per row whose upsert raised, keyed by its primary key value.
    errors: tuple[str, ...] = ()
    #: What this table's own `<table>_divergence` found comparing the rows
    #: just read against what PostgreSQL now holds. Non-empty means the copy
    #: is not yet clean, whether or not any row raised.
    divergent: tuple[dict, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors and not self.divergent


@dataclass(frozen=True)
class BackfillReport:
    """The whole run, one table at a time, in the order it executed."""

    tables: tuple[TableBackfillReport, ...]

    @property
    def ok(self) -> bool:
        return all(table.ok for table in self.tables)


def wave_order(
    mirrors: dict[str, tuple[type[PostgresTableMirror], Any]],
) -> list[str]:
    """Every mirrored table, parents before the children that reference them.

    Derived from `MirroredTable.references` rather than from PostgreSQL's own
    foreign-key catalog: that map is the one place this dependency already
    lives — `tests/db/test_mirror_contract.py` uses it to build a valid parent
    row for any child — so this asks the Python declarations a question the
    catalog would only answer the same way, one round trip later.
    """
    dependencies = {
        table: {parent for parent in mirror.spec.references.values() if parent in mirrors}
        for table, (mirror, _module) in mirrors.items()
    }
    order: list[str] = []
    placed: set[str] = set()
    while len(placed) < len(dependencies):
        wave = sorted(
            table
            for table, deps in dependencies.items()
            if table not in placed and deps <= placed
        )
        if not wave:
            raise RuntimeError(
                "foreign-key cycle among mirrored tables: "
                f"{sorted(set(dependencies) - placed)}"
            )
        order.extend(wave)
        placed |= set(wave)
    return order


def _fetch_rows(sqlite_conn: Any, table: str, columns: tuple[str, ...], order_by: tuple[str, ...]) -> list[dict]:
    """Every row of `table`, in its stored shape.

    `SELECT <the mirror's own columns>` rather than `SELECT *`: it fails loudly
    with `sqlite3.OperationalError` if a mirror declares a column this SQLite
    database does not have, instead of silently sending PostgreSQL a row with a
    hole in it, and it returns exactly what `ColumnCodec.to_column` wants —
    never a reader that decodes `jsonb` text into a `dict` first, which is the
    trap `tests/db/test_stored_reader_fitness.py` exists to catch on the
    read-path callers this module deliberately does not reuse.
    """
    cursor = sqlite_conn.execute(
        f"SELECT {', '.join(columns)} FROM {table} ORDER BY {', '.join(order_by)}"
    )
    return [dict(row) for row in cursor.fetchall()]


def _find_divergence(module: Any, table: str) -> Callable[[list, Any], list[dict]] | None:
    """This table's own `<table>_divergence`, found by the name `divergence_against` gives it.

    The same lookup `tests/db/test_stored_reader_fitness.py` uses to prove a
    reconciliation exists and is named after its table, reused rather than
    re-derived so the backfill's report and that gate can never disagree about
    which function is authoritative for a table.
    """
    for value in vars(module).values():
        if getattr(value, "__name__", None) == f"{table}_divergence":
            return value
    return None


def run_historical_backfill(
    sqlite_path: Path,
    connection_factory: Callable[[], Any] | None = None,
    *,
    tables: Iterable[str] | None = None,
) -> BackfillReport:
    """Copy every pre-existing row in `sqlite_path` into its PostgreSQL mirror.

    `connection_factory` is the seam every mirror already takes: `None` opens
    the process pool via `command_center.db.pool.connection()`, and a caller
    (a test, or the CLI reusing one checked-out connection for the whole run)
    supplies its own. `tables` restricts the run to a subset — a resume after a
    partial failure, or a focused check — and defaults to every declared
    mirror, in dependency order. A name that names no declared mirror raises
    `ValueError` rather than being dropped: an operator's `--table` typo must
    not read back as a successful, empty backfill.

    A row whose `upsert()` raises is recorded and does not abort the table: one
    malformed historical row (unparseable JSON, a value a `CHECK` constraint
    refuses) must not lose every other row behind it, and the report names
    exactly which row and why.
    """
    from command_center.runtime.db import core as runtime_core

    mirrors = mirror_classes()
    order = wave_order(mirrors)
    if tables is not None:
        wanted = set(tables)
        unknown = wanted - mirrors.keys()
        if unknown:
            raise ValueError(
                "no such mirrored table(s): "
                f"{', '.join(sorted(unknown))} (declared: {', '.join(sorted(mirrors))})"
            )
        order = [table for table in order if table in wanted]

    reports: list[TableBackfillReport] = []
    with runtime_core.connect(sqlite_path) as sqlite_conn:
        for table in order:
            mirror_cls, module = mirrors[table]
            spec = mirror_cls.spec
            rows = _fetch_rows(sqlite_conn, table, spec.columns, spec.key_columns)
            mirror = mirror_cls(connection_factory)

            errors: list[str] = []
            upserted = 0
            for row in rows:
                try:
                    mirror.upsert(row)
                    upserted += 1
                except Exception as exc:  # noqa: BLE001 — named and reported; one bad row must not abort the table
                    key = tuple(row.get(column) for column in spec.key_columns)
                    errors.append(f"{key}: {type(exc).__name__}: {exc}")

            if spec.identity:
                mirror.resync_identity()

            divergence_fn = _find_divergence(module, table)
            divergent = divergence_fn(rows, mirror) if divergence_fn is not None else []

            reports.append(
                TableBackfillReport(
                    table=table,
                    source_rows=len(rows),
                    upserted=upserted,
                    errors=tuple(errors),
                    divergent=tuple(divergent),
                )
            )
    return BackfillReport(tables=tuple(reports))
