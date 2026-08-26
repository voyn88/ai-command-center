"""The zero-tolerance parity gate the cutover has been waiting on (VOYN-W0-AICC-SRV-07b).

Every mirrored table already carries its own `divergence` function —
`owner_item_store.divergence`, `run_store.run_divergence`, thirty more like
them — and every one of their docstrings says some version of the same thing:
"the cutover is gated on a session with no divergence." None of them said how
that session gets assembled. Each was exercised by hand, one row at a time,
inside a test that builds the single row it wants to check. Nothing pulled
every row a table's SQLite authority actually holds and ran it through that
table's own `divergence` against the real mirror — so a table could disagree
on row one million while every suite stayed green on the row it was told to
look at.

This module is that missing assembly, and "zero tolerance" is its whole
design, not a tuning knob: `ReconciliationReport.clean` is `True` only when
every mirrored table reports zero differences. One diverging row in one table
among thirty-two fails the report exactly as hard as thirty-two diverging
tables would — there is no threshold, no percentage, no "mostly clean" reading
of the result. A gate that tolerated a little drift would let the one row that
matters through with it, and there is no way to know in advance which row that
is.

`command_center.db.mirror_registry.mirror_classes()` supplies the "every
mirrored table" half, discovered rather than listed for the same reason the
contract and the coverage gate already discover it: a table cannot opt out of
a check about the whole set by living in an unexpected file. This module adds
the two things the registry does not carry — a generic reader for the SQLite
side of a table the registry only names, and a lookup for the specific
`divergence` function `divergence_against` attached to it, found by the name
that function sets on itself rather than by the name a caller gave its
variable, because that name varies per module (`divergence`, `run_divergence`,
`task_divergence`, ...) and the one thing every one of them agrees on is what
`divergence_against(spec)` calls the function it returns.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from command_center.db.mirror_registry import mirror_classes
from command_center.db.table_mirror import MirroredTable, PostgresTableMirror

__all__ = [
    "MirrorTarget",
    "ReconciliationReport",
    "TableDivergence",
    "authority_rows",
    "reconcile",
    "targets_from_registry",
]


def authority_rows(conn: sqlite3.Connection, spec: MirroredTable) -> list[dict]:
    """Every row `spec.table` holds in the SQLite authority, as plain dicts.

    Generic on purpose: `spec.columns` names the same columns in the same
    order on both sides of the mirror — `test_mirror_contract.py` pins each
    table's declaration against the live SQLite schema precisely so that this
    query never needs table-specific knowledge. The rows come back in the
    authority's raw shape (ISO text timestamps, `0`/`1` flags, JSON text) —
    exactly what `ColumnCodec` and `divergence` already expect from "the
    authority side," because that is the shape every other caller of
    `divergence` has always handed it.

    Ordered by the table's own key, the same one `divergence` pairs rows on.
    `divergence` does not need the order — it indexes the mirror side by key
    before comparing — but a report a person reads top to bottom should not be
    shuffled by whichever way SQLite happened to walk the table.
    """
    columns = spec.columns
    cur = conn.execute(
        f"SELECT {', '.join(columns)} FROM {spec.table} "
        f"ORDER BY {', '.join(spec.key_columns)}"
    )
    return [dict(zip(columns, row, strict=True)) for row in cur.fetchall()]


def _divergence_function(
    table: str, module: Any
) -> Callable[[Iterable[dict], Any], list[dict]]:
    """The `divergence_against(spec)` closure `module` built for `table`.

    Found by the name the closure gives *itself* (`f"{table}_divergence"`,
    set inside `divergence_against`), not by the name the module bound it to
    — those disagree constantly (`owner_item_store.divergence` is a function
    named `owner_item_divergence`) and only the self-given name is guaranteed
    to match the table the registry says this module mirrors.
    """
    wanted = f"{table}_divergence"
    for value in vars(module).values():
        if callable(value) and getattr(value, "__name__", None) == wanted:
            return value
    raise TypeError(
        f"{module.__name__} declares the mirror for `{table}` but exposes no "
        f"function named `{wanted}` — every mirror built through "
        "`divergence_against` sets that name on the function it returns, so "
        "its absence means the table's own reconciliation was never wired up."
    )


@dataclass(frozen=True)
class MirrorTarget:
    """One mirrored table, resolved to the objects reconciling it needs."""

    table: str
    spec: MirroredTable
    mirror: PostgresTableMirror
    divergence: Callable[[Iterable[dict], Any], list[dict]]


def targets_from_registry(
    connection_factory: Any = None,
    *,
    registry: dict[str, tuple[type[PostgresTableMirror], object]] | None = None,
) -> list[MirrorTarget]:
    """Every declared mirror, resolved into something `reconcile` can run.

    `registry` defaults to the real `mirror_classes()` and exists as a
    parameter only so a test can substitute a small, fully-controlled set
    instead of the whole schema.
    """
    declared = registry if registry is not None else mirror_classes()
    targets: list[MirrorTarget] = []
    for table, (mirror_cls, module) in declared.items():
        targets.append(
            MirrorTarget(
                table=table,
                spec=mirror_cls.spec,
                mirror=mirror_cls(connection_factory),
                divergence=_divergence_function(table, module),
            )
        )
    return targets


@dataclass(frozen=True)
class TableDivergence:
    """One table's reconciliation result: the differences, whatever their shape.

    `differences` carries whatever `mirror_support.divergence` reported —
    a field mismatch, a row missing from the mirror, a row the mirror has and
    the authority does not, or `MIRROR_UNAVAILABLE` for a mirror that could not
    be read. This module does not need to tell those apart: every one of them
    is a reason the table is not clean.
    """

    table: str
    differences: tuple[dict, ...]

    @property
    def clean(self) -> bool:
        return not self.differences


@dataclass(frozen=True)
class ReconciliationReport:
    """The whole cutover gate: clean only when every table is."""

    tables: tuple[TableDivergence, ...]

    @property
    def clean(self) -> bool:
        """Zero tolerance: one diverging row anywhere fails the whole report.

        Not "most tables agree," not "no table is more than a few rows off" —
        `all()` over every table's own `clean`, so a single mismatched field in
        one row of one table among thirty-two returns `False` exactly as it
        would if every table disagreed on every row. See the module docstring
        for why a softer reading defeats the point of running this at all.
        """
        return all(table.clean for table in self.tables)

    @property
    def dirty(self) -> tuple[TableDivergence, ...]:
        return tuple(table for table in self.tables if not table.clean)


def reconcile(
    sqlite_conn: sqlite3.Connection, targets: Iterable[MirrorTarget]
) -> ReconciliationReport:
    """Run every target's own `divergence` against its real SQLite authority.

    One `SELECT` per table plus whatever `divergence` costs — no batching, no
    early exit on the first dirty table, because zero tolerance means every
    table's result is reported, not just the first failure.
    """
    results = tuple(
        TableDivergence(
            table=target.table,
            differences=tuple(
                target.divergence(authority_rows(sqlite_conn, target.spec), target.mirror)
            ),
        )
        for target in targets
    )
    return ReconciliationReport(tables=results)
