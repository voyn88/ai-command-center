"""SRV-07 reconciliation: real measurement, not extrapolation (VOYN-W0-AICC-SRV-07).

Discovery, topological order, and the SQLite-side orphan anti-join need no
PostgreSQL server and run on every laptop. `measure_row_count` and `reconcile`
talk to a real database and follow the rest of `tests/db`'s convention: they
skip without `AICC_TEST_PG_ADMIN_DSN` and run for real in CI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.db.srv07_reconciliation import (
    authority_row_count,
    discover_specs,
    find_orphaned_rows,
    measure_row_count,
    reconcile,
    topological_order,
)
from command_center.db.table_mirror import MirroredTable
from command_center.runtime.db.core import connect as sqlite_connect

# --- discovery ---------------------------------------------------------------


def test_discover_specs_matches_the_declared_mirror_set() -> None:
    """Two independent scans of the same `MirroredTable` declarations must
    agree, or one of them is wrong about what a mirror is."""
    from tests.db.mirror_discovery import mirror_classes

    assert set(discover_specs()) == set(mirror_classes())


def test_queue_entry_is_out_of_scope() -> None:
    """`queue_entry` mirrors by whole-list replacement rather than through
    `PostgresTableMirror` (`mirror_support`'s documented boundary) -- it has
    no `MirroredTable` declaration for this module to find, and that is by
    design, not an omission."""
    assert "queue_entry" not in discover_specs()


# --- topological order ---------------------------------------------------


def test_topological_order_places_every_table_after_what_it_references() -> None:
    specs = {table: spec for table, (spec, _cls) in discover_specs().items()}

    order = topological_order(specs)

    assert set(order) == set(specs)
    position = {table: index for index, table in enumerate(order)}
    for table, spec in specs.items():
        for parent in spec.references.values():
            if parent in specs:
                assert position[parent] < position[table], (table, parent)


def test_topological_order_raises_on_a_reference_cycle() -> None:
    specs = {
        "a": MirroredTable(table="a", columns=("id", "b_id"), references={"b_id": "b"}),
        "b": MirroredTable(table="b", columns=("id", "a_id"), references={"a_id": "a"}),
    }

    with pytest.raises(ValueError, match="cycle"):
        topological_order(specs)


# --- row counts: measured, never estimated --------------------------------


def test_authority_row_count_reflects_the_live_table_not_a_cached_figure(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    with sqlite_connect(db_path) as conn:
        conn.execute("CREATE TABLE widget (id TEXT PRIMARY KEY)")
        conn.execute("BEGIN IMMEDIATE")
        for i in range(5):
            conn.execute("INSERT INTO widget (id) VALUES (?)", (f"w{i}",))
        conn.execute("COMMIT")

    assert authority_row_count(db_path, "widget") == 5

    with sqlite_connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM widget WHERE id = 'w0'")
        conn.execute("COMMIT")

    # No statistic cached anywhere to go stale -- the very next call already
    # reflects the deletion, which is the whole property this function exists
    # to guarantee.
    assert authority_row_count(db_path, "widget") == 4


# --- orphans: found in the source, before PostgreSQL's own foreign keys
# would ever see the row --------------------------------------------------


def _parent_child_specs() -> dict[str, MirroredTable]:
    return {
        "parent": MirroredTable(table="parent", columns=("id",)),
        "child": MirroredTable(
            table="child",
            columns=("id", "parent_id"),
            references={"parent_id": "parent"},
        ),
    }


def _seed_parent_child(db_path: Path, *, children: list[tuple[str, str | None]]) -> None:
    with sqlite_connect(db_path) as conn:
        conn.execute("CREATE TABLE parent (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE child (id TEXT PRIMARY KEY, parent_id TEXT)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO parent (id) VALUES ('p1')")
        for child_id, parent_id in children:
            conn.execute(
                "INSERT INTO child (id, parent_id) VALUES (?, ?)", (child_id, parent_id)
            )
        conn.execute("COMMIT")


def test_find_orphaned_rows_reports_nothing_when_every_reference_holds(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_parent_child(db_path, children=[("c1", "p1")])
    specs = _parent_child_specs()

    orphans = find_orphaned_rows(db_path, specs["child"], specs)

    assert len(orphans) == 1
    assert orphans[0].matched


def test_find_orphaned_rows_detects_a_legacy_dangling_reference(tmp_path: Path) -> None:
    """Exactly what SRV-07 must catch before a backfill: a row SQLite let
    through -- foreign keys are enforced per connection, and a writer that
    never set the pragma (an old script, a direct `sqlite3` shell, a
    connection older than this repository turning it on) can leave one behind
    indefinitely -- that PostgreSQL's real foreign key would reject outright.
    """
    db_path = tmp_path / "runtime.db"
    _seed_parent_child(db_path, children=[("c1", "p1"), ("orphan", "does-not-exist")])
    specs = _parent_child_specs()

    orphans = find_orphaned_rows(db_path, specs["child"], specs)

    assert len(orphans) == 1
    result = orphans[0]
    assert not result.matched
    assert result.orphaned_keys == ("orphan",)
    assert result.column == "parent_id"
    assert result.parent_table == "parent"


def test_find_orphaned_rows_ignores_a_null_foreign_key(tmp_path: Path) -> None:
    """`NULL` is a row with no parent by design -- every nullable reference in
    this schema is `ON DELETE SET NULL` -- not an orphan."""
    db_path = tmp_path / "runtime.db"
    _seed_parent_child(db_path, children=[("c1", None)])
    specs = _parent_child_specs()

    orphans = find_orphaned_rows(db_path, specs["child"], specs)

    assert orphans[0].matched


def test_find_orphaned_rows_reports_once_per_declared_reference(tmp_path: Path) -> None:
    """A table with two foreign keys and one orphaned column should not read
    as clean because the other column was fine."""
    specs = {
        "left": MirroredTable(table="left", columns=("id",)),
        "right": MirroredTable(table="right", columns=("id",)),
        "child": MirroredTable(
            table="child",
            columns=("id", "left_id", "right_id"),
            references={"left_id": "left", "right_id": "right"},
        ),
    }
    db_path = tmp_path / "runtime.db"
    with sqlite_connect(db_path) as conn:
        conn.execute("CREATE TABLE left (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE right (id TEXT PRIMARY KEY)")
        conn.execute("CREATE TABLE child (id TEXT PRIMARY KEY, left_id TEXT, right_id TEXT)")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO left (id) VALUES ('l1')")
        conn.execute("INSERT INTO right (id) VALUES ('r1')")
        conn.execute(
            "INSERT INTO child (id, left_id, right_id) VALUES ('c1', 'l1', 'missing')"
        )
        conn.execute("COMMIT")

    orphans = find_orphaned_rows(db_path, specs["child"], specs)

    by_column = {o.column: o for o in orphans}
    assert set(by_column) == {"left_id", "right_id"}
    assert by_column["left_id"].matched
    assert not by_column["right_id"].matched
    assert by_column["right_id"].orphaned_keys == ("c1",)


# --- the real schema, real Postgres -----------------------------------------


def test_measure_row_count_agrees_when_the_backfill_is_current(
    pg_connection_factory, tmp_path
) -> None:
    from command_center.db.execution_store import PostgresTaskMirror
    from command_center.runtime.db import execution as exec_db

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    tasks = PostgresTaskMirror(connection_factory=pg_connection_factory)
    # `create_task` dual-writes best-effort against the *default* pool, which
    # is not this test's throwaway database, so the mirror is seeded directly
    # instead -- the same way tests/db/test_mirror_probe.py does.
    with exec_db.db.connect(db_path) as conn, exec_db.db.transaction(conn):
        for i in range(3):
            now = exec_db.db.iso_now()
            row = {
                "id": exec_db.db.new_id(),
                "project": "AICC",
                "title": f"t{i}",
                "task_type": "feature",
                "legacy_task_id": None,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                "INSERT INTO task (id, project, title, task_type, legacy_task_id, "
                "created_at, updated_at) VALUES (:id, :project, :title, :task_type, "
                ":legacy_task_id, :created_at, :updated_at)",
                row,
            )
            tasks.upsert(row)

    result = measure_row_count(db_path, tasks)

    assert result.matched
    assert result.authority_count == 3
    assert result.mirror_count == 3


def test_measure_row_count_catches_a_gap_a_planner_estimate_would_miss(
    pg_connection_factory, tmp_path
) -> None:
    """The property this whole module exists for: a live `COUNT(*)` sees a
    deletion a stale `ANALYZE` snapshot would not.

    `pg_class.reltuples` only refreshes on `ANALYZE`/`VACUUM`/autovacuum, so a
    delete run right after one is invisible to it until the next one runs --
    exactly the class of "1.22m rows, unmeasured" gap SRV-07's reconciliation
    exists to close.
    """
    from command_center.db.execution_store import PostgresTaskMirror
    from command_center.runtime.db import execution as exec_db

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    tasks = PostgresTaskMirror(connection_factory=pg_connection_factory)

    with exec_db.db.connect(db_path) as conn, exec_db.db.transaction(conn):
        rows = []
        for i in range(10):
            now = exec_db.db.iso_now()
            row = {
                "id": exec_db.db.new_id(),
                "project": "AICC",
                "title": f"t{i}",
                "task_type": "feature",
                "legacy_task_id": None,
                "created_at": now,
                "updated_at": now,
            }
            conn.execute(
                "INSERT INTO task (id, project, title, task_type, legacy_task_id, "
                "created_at, updated_at) VALUES (:id, :project, :title, :task_type, "
                ":legacy_task_id, :created_at, :updated_at)",
                row,
            )
            rows.append(row)
            tasks.upsert(row)

    with pg_connection_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("ANALYZE task")
            cur.execute("SELECT reltuples FROM pg_class WHERE oid = 'task'::regclass")
            analyzed_estimate = cur.fetchone()[0]
            # Remove the source row too, so the authority and mirror agree on
            # 9 -- only the planner's cached figure is now wrong.
            cur.execute("DELETE FROM task WHERE id = %s", (rows[0]["id"],))
    with exec_db.db.connect(db_path) as conn, exec_db.db.transaction(conn):
        conn.execute("DELETE FROM task WHERE id = ?", (rows[0]["id"],))

    result = measure_row_count(db_path, tasks)

    assert analyzed_estimate == 10  # the stale figure this test is guarding against
    assert result.matched
    assert result.authority_count == 9
    assert result.mirror_count == 9  # measured live, not read from pg_class


def test_reconcile_flags_a_table_the_backfill_never_reached(
    pg_connection_factory, tmp_path
) -> None:
    from command_center.runtime.db import execution as exec_db

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    exec_db.create_task(db_path, project="AICC", title="never backfilled", task_type="feature")

    report = reconcile(db_path, pg_connection_factory)

    assert not report.matched
    task_result = next(t for t in report.tables if t.table == "task")
    assert not task_result.matched
    assert task_result.row_count.authority_count == 1
    assert task_result.row_count.mirror_count == 0


def test_reconcile_runs_every_table_even_after_the_first_failure(
    pg_connection_factory, tmp_path
) -> None:
    """`docs/srv01b-schema-map.md` says a discrepancy halts the migration --
    that is a decision for the caller, not a reason this report should stop
    checking after the first table it happens to reach."""
    from command_center.runtime.db import execution as exec_db

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    exec_db.create_task(db_path, project="AICC", title="unbackfilled", task_type="feature")

    report = reconcile(db_path, pg_connection_factory)

    assert len(report.tables) == len(discover_specs())
    assert report.first_failure is not None
    assert report.first_failure.table == "task"
