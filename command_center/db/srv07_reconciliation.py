"""Row-count and orphan reconciliation for the runtime-store migration (SRV-07).

`docs/srv01b-schema-map.md` names four reconciliation steps for the one-time
cutover of the SQLite runtime store into PostgreSQL and says plainly that they
are "a plan, not executed work". Two of the four are built here: **row counts
match** and **no dangling foreign-key reference**. The other two -- primary-key
*set* equality and a column-level spot check -- are already covered for every
table this module reconciles by the per-write `divergence()` reconciliation
`command_center/db/mirror_support.py` performs continuously under dual-write
(VOYN-W0-AICC-SRV-01B): it already reports a row present on one side and not
the other, and a field that disagrees. What dual-write reconciliation cannot
tell you, because it runs one write at a time, is answered here instead:

* **Volume.** Loading every row of a table with a real production population
  (the doc's own worked example is a table with roughly 1.22 million rows) to
  diff it in Python, the way `divergence()` does, is not a plan a cutover can
  run twice under pressure. A row *count*, checked on both sides with a real
  `SELECT COUNT(*)` (`PostgresTableMirror.count()`, `authority_row_count`
  below), is cheap enough to run before and after a backfill and catches
  exactly the discrepancy an estimate would paper over -- which is also why
  neither function here will ever read `pg_class.reltuples` or
  `pg_stat_user_tables.n_live_tup`. Those are `ANALYZE`-cadence planner
  statistics, not a measurement, and this module's whole job is to be the
  measurement.
* **Orphans.** PostgreSQL enforces every foreign key this schema declares, so
  a dangling reference cannot exist *there* -- the `INSERT` simply fails.
  SQLite only enforces `PRAGMA foreign_keys` per connection, so a row written
  by a path that never set it (an old script, a direct `sqlite3` shell, a
  connection from before this repository turned the pragma on) can leave a
  legacy orphan sitting in the authority indefinitely. Backfilling one into
  PostgreSQL does not corrupt anything -- it fails loudly -- but discovering
  that mid-backfill on table 19 of 32 is a worse time to learn it than before
  the backfill starts. `find_orphaned_rows` runs the anti-join against the
  SQLite source for exactly that reason.

Both checks read `command_center/db/*_store.py`'s own `MirroredTable`
declarations (`table_mirror.PostgresTableMirror.__subclasses__()`) rather than
a second, hand-maintained table list -- the same reasoning
`tests/db/mirror_discovery.py` gives for doing it that way: a list transcribed
by hand is a list that drifts.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

from command_center.db.table_mirror import MirroredTable, PostgresTableMirror
from command_center.runtime.db.core import connect as sqlite_connect

__all__ = [
    "ForeignKeyOrphans",
    "ReconciliationReport",
    "RowCountResult",
    "TableReconciliation",
    "authority_row_count",
    "discover_specs",
    "find_orphaned_rows",
    "measure_row_count",
    "reconcile",
    "topological_order",
]


# --------------------------------------------------------------------------
# Discovery -- the declared mirrors are the table list, not a second one.
# --------------------------------------------------------------------------


def _modules_declaring_mirrors() -> list[str]:
    """Dotted names of `command_center.db` modules that might declare a mirror.

    A text scan, not an import: it decides only what is worth importing, and
    `PostgresTableMirror.__subclasses__()` below is the authoritative answer.
    Mirrors `tests/db/mirror_discovery.modules_declaring_mirrors` -- the same
    technique, kept independent here because production code should not import
    from `tests/`.
    """
    import command_center.db as db_package

    package_root = Path(db_package.__path__[0])
    found: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        if "PostgresTableMirror" not in path.read_text(encoding="utf-8"):
            continue
        relative = path.relative_to(package_root).with_suffix("")
        found.append("command_center.db." + ".".join(relative.parts))
    return found


def _every_subclass(root: type) -> Iterator[type]:
    for subclass in root.__subclasses__():
        yield subclass
        yield from _every_subclass(subclass)


def discover_specs() -> dict[str, tuple[MirroredTable, type[PostgresTableMirror]]]:
    """`{table: (spec, mirror class)}` for every table with a declared mirror.

    `queue_entry` is the one domain table this never returns: it mirrors by
    whole-list replacement rather than through `PostgresTableMirror`
    (`mirror_support`'s own docstring explains why), so it carries no
    `MirroredTable` declaration for this module to find. It is out of scope
    for the same reason it is out of scope for dual-write reconciliation.
    """
    for module_name in _modules_declaring_mirrors():
        importlib.import_module(module_name)

    found: dict[str, tuple[MirroredTable, type[PostgresTableMirror]]] = {}
    for subclass in _every_subclass(PostgresTableMirror):
        if not subclass.__module__.startswith("command_center.db"):
            continue
        found[subclass.spec.table] = (subclass.spec, subclass)
    return dict(sorted(found.items()))


def topological_order(specs: dict[str, MirroredTable]) -> tuple[str, ...]:
    """`specs`' tables ordered so every table follows every table it references.

    A foreign key naming a table outside `specs` (there are none in this
    schema, but a future partial reconciliation could pass a subset) is
    treated as already satisfied rather than as a missing dependency -- it is
    not this function's table to order.
    """
    remaining = dict(specs)
    placed: set[str] = set()
    ordered: list[str] = []
    while remaining:
        ready = sorted(
            table
            for table, spec in remaining.items()
            if all(
                parent in placed or parent not in specs
                for parent in spec.references.values()
            )
        )
        if not ready:
            raise ValueError(f"reference cycle among {sorted(remaining)}")
        for table in ready:
            ordered.append(table)
            placed.add(table)
            del remaining[table]
    return tuple(ordered)


# --------------------------------------------------------------------------
# Volume -- measured, never extrapolated.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RowCountResult:
    table: str
    authority_count: int
    mirror_count: int

    @property
    def matched(self) -> bool:
        return self.authority_count == self.mirror_count


def authority_row_count(sqlite_path: Path, table: str) -> int:
    """`table`'s row count in the SQLite authority, via a real `COUNT(*)`."""
    with sqlite_connect(sqlite_path) as conn:
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        return int(row["n"])


def measure_row_count(sqlite_path: Path, mirror: PostgresTableMirror) -> RowCountResult:
    """Both sides' row counts for one table, each a real, current `COUNT(*)`."""
    return RowCountResult(
        table=mirror.spec.table,
        authority_count=authority_row_count(sqlite_path, mirror.spec.table),
        mirror_count=mirror.count(),
    )


# --------------------------------------------------------------------------
# Orphans -- found in the source, before PostgreSQL's own foreign keys would
# refuse them.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ForeignKeyOrphans:
    table: str
    column: str
    parent_table: str
    orphaned_keys: tuple[Any, ...]

    @property
    def matched(self) -> bool:
        return not self.orphaned_keys


def find_orphaned_rows(
    sqlite_path: Path,
    spec: MirroredTable,
    specs: dict[str, MirroredTable],
) -> tuple[ForeignKeyOrphans, ...]:
    """`spec`'s rows whose declared foreign keys have no matching parent.

    One `ForeignKeyOrphans` per declared reference (`spec.references`), even
    when it finds nothing -- a table with three references and one orphaned
    column should not read as "clean" because two of the three were fine.

    A reference whose parent has no declared mirror, or whose parent key is
    composite, is skipped rather than guessed at: nothing in this schema hits
    either case today (checked by `test_every_reference_targets_a_single_
    column_key`), and a schema change that did would need this function taught
    the join, not a silent wrong answer from it.
    """
    results: list[ForeignKeyOrphans] = []
    own_keys = ", ".join(f"t.{name}" for name in spec.key_columns)
    with sqlite_connect(sqlite_path) as conn:
        for column, parent_table in sorted(spec.references.items()):
            parent_spec = specs.get(parent_table)
            if parent_spec is None or len(parent_spec.key_columns) != 1:
                continue
            parent_key = parent_spec.key_columns[0]
            rows = conn.execute(
                f"SELECT DISTINCT {own_keys} FROM {spec.table} t "
                f"WHERE t.{column} IS NOT NULL AND NOT EXISTS ("
                f"SELECT 1 FROM {parent_table} p WHERE p.{parent_key} = t.{column})"
            ).fetchall()
            orphaned = tuple(
                row[0] if len(spec.key_columns) == 1 else tuple(row) for row in rows
            )
            results.append(
                ForeignKeyOrphans(
                    table=spec.table,
                    column=column,
                    parent_table=parent_table,
                    orphaned_keys=orphaned,
                )
            )
    return tuple(results)


# --------------------------------------------------------------------------
# The combined report.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TableReconciliation:
    table: str
    row_count: RowCountResult
    orphans: tuple[ForeignKeyOrphans, ...]

    @property
    def matched(self) -> bool:
        return self.row_count.matched and all(o.matched for o in self.orphans)


@dataclass(frozen=True)
class ReconciliationReport:
    tables: tuple[TableReconciliation, ...]

    @property
    def matched(self) -> bool:
        return all(table.matched for table in self.tables)

    @property
    def first_failure(self) -> TableReconciliation | None:
        for table in self.tables:
            if not table.matched:
                return table
        return None


def reconcile(
    sqlite_path: Path, mirror_connection_factory: Callable[[], Any]
) -> ReconciliationReport:
    """Row counts and orphan checks for every declared table, parents first.

    Every table's checks run, even once one has already failed: "any
    discrepancy stops the migration" (`docs/srv01b-schema-map.md`) is a
    decision for whoever reads `first_failure` or `matched` to act on, not a
    reason for this report to show only the first table it happened to reach.
    """
    discovered = discover_specs()
    specs = {table: spec for table, (spec, _cls) in discovered.items()}
    order = topological_order(specs)

    results = []
    for table in order:
        spec, mirror_cls = discovered[table]
        mirror = mirror_cls(connection_factory=mirror_connection_factory)
        results.append(
            TableReconciliation(
                table=table,
                row_count=measure_row_count(sqlite_path, mirror),
                orphans=find_orphaned_rows(sqlite_path, spec, specs),
            )
        )
    return ReconciliationReport(tables=tuple(results))
