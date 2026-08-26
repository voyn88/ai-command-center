"""The zero-tolerance parity gate, proven to actually gate (VOYN-W0-AICC-SRV-07b).

`command_center.db.reconcile` assembles pieces every mirrored table already
ships -- its own SQLite rows, its own PostgreSQL mirror, its own `divergence`
function -- into one report. The per-column comparison is `mirror_support
.divergence`'s contract, already proven per table by every `test_*_store.py`
suite in this package, so these tests are about the two things only this
module could get wrong: reading the right rows off the right table, and
refusing to call the overall report clean because most of it agreed.

No PostgreSQL is needed here, deliberately: a mirror only needs to answer
`list_records()`, and a fake that does that exercises the same `divergence`
contract a real one would, without a database on either side.
"""

from __future__ import annotations

import sqlite3
import types

import pytest

from command_center.db.mirror_registry import mirror_classes
from command_center.db.mirror_support import MIRROR_UNAVAILABLE
from command_center.db.reconcile import (
    MirrorTarget,
    authority_rows,
    reconcile,
    targets_from_registry,
)
from command_center.db.table_mirror import MirroredTable, divergence_against

WIDGET = MirroredTable(table="widget", columns=("id", "name", "active"))
GADGET = MirroredTable(table="gadget", columns=("id", "name", "active"))


def _create(conn: sqlite3.Connection, spec: MirroredTable, rows: list[tuple]) -> None:
    conn.execute(f"CREATE TABLE {spec.table} (id TEXT PRIMARY KEY, name TEXT, active INTEGER)")
    conn.executemany(f"INSERT INTO {spec.table} (id, name, active) VALUES (?, ?, ?)", rows)
    conn.commit()


class _FakeMirror:
    """Quacks like `PostgresTableMirror` for exactly what `divergence` needs."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def list_records(self) -> list[dict]:
        return self._rows


class _BrokenMirror:
    def list_records(self) -> list[dict]:
        raise RuntimeError("connection refused")


def test_authority_rows_reads_every_row_in_key_order() -> None:
    conn = sqlite3.connect(":memory:")
    _create(conn, WIDGET, [("b", "second", 1), ("a", "first", 0)])

    assert authority_rows(conn, WIDGET) == [
        {"id": "a", "name": "first", "active": 0},
        {"id": "b", "name": "second", "active": 1},
    ]


def test_reconcile_is_clean_when_every_table_agrees() -> None:
    conn = sqlite3.connect(":memory:")
    _create(conn, WIDGET, [("a", "first", 0), ("b", "second", 1)])
    target = MirrorTarget(
        table="widget",
        spec=WIDGET,
        mirror=_FakeMirror(
            [
                {"id": "a", "name": "first", "active": 0},
                {"id": "b", "name": "second", "active": 1},
            ]
        ),
        divergence=divergence_against(WIDGET),
    )

    report = reconcile(conn, [target])

    assert report.clean is True
    assert report.dirty == ()
    assert report.tables[0].differences == ()


def test_reconcile_is_zero_tolerance_one_bad_row_in_one_table_fails_the_whole_report() -> None:
    """The gate, shown to bite.

    Two tables, one clean and one with a single stale field. A report that
    only flagged the dirty table without failing overall would be a report an
    operator could read as "basically fine" -- exactly the reading zero
    tolerance exists to rule out.
    """
    conn = sqlite3.connect(":memory:")
    _create(conn, WIDGET, [("a", "first", 0)])
    _create(conn, GADGET, [("x", "stale name", 1)])

    clean_target = MirrorTarget(
        table="widget",
        spec=WIDGET,
        mirror=_FakeMirror([{"id": "a", "name": "first", "active": 0}]),
        divergence=divergence_against(WIDGET),
    )
    dirty_target = MirrorTarget(
        table="gadget",
        spec=GADGET,
        # The mirror disagrees with the authority on one field.
        mirror=_FakeMirror([{"id": "x", "name": "current name", "active": 1}]),
        divergence=divergence_against(GADGET),
    )

    report = reconcile(conn, [clean_target, dirty_target])

    assert report.clean is False
    by_table = {t.table: t for t in report.tables}
    assert by_table["widget"].clean is True
    assert by_table["gadget"].clean is False
    assert report.dirty == (by_table["gadget"],)
    assert by_table["gadget"].differences[0]["fields"] == ["name"]


def test_reconcile_treats_an_unreadable_mirror_as_dirty_not_clean() -> None:
    """An absent store has nothing to disagree with -- `divergence` reports
    `MIRROR_UNAVAILABLE` rather than `[]`, and this gate must not read that as
    agreement either."""
    conn = sqlite3.connect(":memory:")
    _create(conn, WIDGET, [("a", "first", 0)])
    target = MirrorTarget(
        table="widget", spec=WIDGET, mirror=_BrokenMirror(), divergence=divergence_against(WIDGET)
    )

    report = reconcile(conn, [target])

    assert report.clean is False
    assert report.tables[0].differences[0]["id"] == MIRROR_UNAVAILABLE


def test_targets_from_registry_fails_loudly_when_a_mirror_has_no_divergence_function() -> None:
    """The positive control for the name-based lookup: a module that declares
    a mirror but never wired up its reconciliation must not be silently
    skipped -- that is a table the cutover gate would have missed entirely."""

    class _OrphanMirror:
        spec = MirroredTable(table="orphan", columns=("id",))

        def __init__(self, connection_factory) -> None:
            pass

    # declares nothing named orphan_divergence
    fake_module = types.SimpleNamespace(__name__="fake_orphan_module")
    with pytest.raises(TypeError, match="orphan_divergence"):
        targets_from_registry(registry={"orphan": (_OrphanMirror, fake_module)})


def test_targets_from_registry_resolves_every_real_mirrored_table() -> None:
    """Every table the schema actually declares a mirror for must resolve --
    this is what would break the moment a store renamed its divergence
    function without updating the convention `divergence_against` relies on.
    """
    targets = targets_from_registry(lambda: None)

    assert {t.table for t in targets} == set(mirror_classes())
