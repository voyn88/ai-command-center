"""VOYN-W0-AICC-SRV-07: the historical backfill actually copies rows.

Reuses `tests/db/test_mirror_contract.py`'s `sample_row` — the same generator
that already proves every declared mirror accepts a row shaped like its own
`MirroredTable` — to build one valid row per table, insert it straight into a
throwaway SQLite runtime database, and drive it through
`run_historical_backfill` instead of through `upsert()` directly. What is under
test is the backfill's own path (read the stored shape out of SQLite in
dependency order, hand it to the declared mirror, reconcile), not the mirrors
themselves, which the shared contract already covers table by table.
"""

from __future__ import annotations

import pytest

from command_center.db.historical_backfill import run_historical_backfill, wave_order
from command_center.db.mirror_registry import mirror_classes
from command_center.runtime.db import core as runtime_core
from tests.db.test_mirror_contract import sample_row

MIRRORS = mirror_classes()


def test_wave_order_places_every_parent_before_its_children() -> None:
    order = wave_order(MIRRORS)
    assert set(order) == set(MIRRORS)
    position = {table: index for index, table in enumerate(order)}
    for table, (mirror, _module) in MIRRORS.items():
        for parent in mirror.spec.references.values():
            if parent in position:
                assert position[parent] < position[table], (
                    f"{table} placed before its own parent {parent}"
                )


def test_wave_order_rejects_a_cycle() -> None:
    from command_center.db.table_mirror import MirroredTable

    class _A:
        spec = MirroredTable(table="a", columns=("id",), references={"b_id": "b"})

    class _B:
        spec = MirroredTable(table="b", columns=("id",), references={"a_id": "a"})

    with pytest.raises(RuntimeError, match="cycle"):
        wave_order({"a": (_A, None), "b": (_B, None)})


def _seed_one_row_per_table(sqlite_path) -> None:
    """One row per mirrored table, in dependency order, so every FK resolves.

    `sample_row(spec, f"{table}-parent")` is the exact row shape and row id
    `tests/db/test_mirror_contract.py`'s own `_ensure_parents` uses to seed a
    child's parent, so seeding every table under that same convention makes
    every foreign key line up without this test tracking the graph itself.
    """
    runtime_core.migrate(sqlite_path)
    with runtime_core.connect(sqlite_path) as conn:
        for table in wave_order(MIRRORS):
            mirror_cls, _module = MIRRORS[table]
            spec = mirror_cls.spec
            row = sample_row(spec, f"{table}-parent")
            columns = spec.columns
            conn.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join('?' for _ in columns)})",
                [row[column] for column in columns],
            )


def test_historical_backfill_copies_every_mirrored_table_and_reconciles(
    tmp_path, pg_connection_factory
) -> None:
    sqlite_path = tmp_path / "runtime.db"
    _seed_one_row_per_table(sqlite_path)

    report = run_historical_backfill(sqlite_path, pg_connection_factory)

    assert {t.table for t in report.tables} == set(MIRRORS)
    for table_report in report.tables:
        assert table_report.source_rows == 1, table_report.table
        assert table_report.errors == (), (table_report.table, table_report.errors)
        assert table_report.upserted == 1, table_report.table
        assert table_report.divergent == (), (table_report.table, table_report.divergent)
    assert report.ok


def test_historical_backfill_is_idempotent(tmp_path, pg_connection_factory) -> None:
    sqlite_path = tmp_path / "runtime.db"
    _seed_one_row_per_table(sqlite_path)

    first = run_historical_backfill(sqlite_path, pg_connection_factory)
    second = run_historical_backfill(sqlite_path, pg_connection_factory)

    assert first.ok
    assert second.ok
    for table_report in second.tables:
        assert table_report.upserted == 1, table_report.table


def test_historical_backfill_can_be_restricted_to_a_subset(
    tmp_path, pg_connection_factory
) -> None:
    sqlite_path = tmp_path / "runtime.db"
    _seed_one_row_per_table(sqlite_path)

    report = run_historical_backfill(sqlite_path, pg_connection_factory, tables=["task"])

    assert [t.table for t in report.tables] == ["task"]
    assert report.tables[0].ok


def test_historical_backfill_rejects_an_unknown_table_name(
    tmp_path, pg_connection_factory
) -> None:
    """A `--table` typo must fail loudly, not read back as an empty success:
    an unrecognized name silently dropped from `order` would copy and
    reconcile nothing while still reporting `report.ok`."""
    sqlite_path = tmp_path / "runtime.db"
    _seed_one_row_per_table(sqlite_path)

    with pytest.raises(ValueError, match="typo"):
        run_historical_backfill(
            sqlite_path, pg_connection_factory, tables=["task", "typo"]
        )


def test_identity_tables_resync_their_sequence_after_backfill(
    tmp_path, pg_connection_factory
) -> None:
    """The sequence must be advanced past the imported id, or the first native
    insert after cutover collides with a row the backfill just wrote."""
    sqlite_path = tmp_path / "runtime.db"
    _seed_one_row_per_table(sqlite_path)

    report = run_historical_backfill(sqlite_path, pg_connection_factory)
    identity_tables = [table for table, (m, _mod) in MIRRORS.items() if m.spec.identity]
    assert identity_tables, "no identity table to exercise"

    with pg_connection_factory() as conn, conn.cursor() as cur:
        for table in identity_tables:
            cur.execute("SELECT pg_get_serial_sequence(%s, 'id')", (table,))
            (sequence,) = cur.fetchone()
            cur.execute("SELECT nextval(%s)", (sequence,))
            (next_id,) = cur.fetchone()
            assert next_id > 1, (
                f"{table}: sequence not advanced past the backfilled id=1 row"
            )

    assert all(t.ok for t in report.tables if t.table in identity_tables)
