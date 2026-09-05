"""The SQLite/JSON -> PostgreSQL cutover migration (`VOYN-W0-AICC-SRV-07`).

Two halves, like the rest of `tests/db`. The first needs no database: snapshot
+ checksum, dependency-order derivation, and the cutover file-permission
machinery are all pure filesystem/graph logic and are proven against a
throwaway directory. The second drives a real SQLite runtime store through its
own creation functions (so the data is shaped exactly like a real legacy
install, not a hand-built fixture) and a real PostgreSQL database through
`pg_connection_factory`, and proves the acceptance criteria directly: counts
and ids match, the same snapshot imports twice without duplicating anything,
and a clean reconciliation is what a cutover requires.
"""

from __future__ import annotations

import json
import sqlite3
import stat
from pathlib import Path

import pytest

from command_center.db import legacy_migration as lm
from command_center.db.table_mirror import MirroredTable
from command_center.runtime.db import core as runtime_core
from command_center.runtime.db import execution as runtime_execution
from command_center.runtime.db import wave1 as runtime_wave1


# --- snapshot + checksum: no database needed --------------------------------


def test_snapshot_sqlite_db_is_read_only_and_checksummed(tmp_path: Path) -> None:
    source = tmp_path / "runtime.db"
    runtime_core.migrate(source)
    runtime_wave1.create_owner_item(source, title="a legacy item")

    snapshot = lm.snapshot_sqlite_db(source, tmp_path / "snapshots")

    assert snapshot.path.exists()
    assert snapshot.path.stat().st_mode & stat.S_IWUSR == 0
    assert snapshot.sha256 == lm._sha256(snapshot.path)
    lm.verify_snapshot(snapshot)  # does not raise

    # The snapshot has its own copy of the row, independent of the source file.
    with sqlite3.connect(f"file:{snapshot.path}?mode=ro", uri=True) as conn:
        (count,) = conn.execute("SELECT COUNT(*) FROM owner_item").fetchone()
    assert count == 1


def test_verify_snapshot_catches_a_changed_file(tmp_path: Path) -> None:
    source = tmp_path / "runtime.db"
    runtime_core.migrate(source)
    snapshot = lm.snapshot_sqlite_db(source, tmp_path / "snapshots")

    # Reach past the read-only permission to simulate tampering/corruption.
    snapshot.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    snapshot.path.write_bytes(snapshot.path.read_bytes() + b"\x00")

    with pytest.raises(ValueError, match="no longer matches its checksum"):
        lm.verify_snapshot(snapshot)


def test_snapshot_json_file_freezes_a_copy(tmp_path: Path) -> None:
    source = tmp_path / "execution_queue.json"
    entries = [{"id": "q1", "task_id": "t1", "project": "p", "state": "waiting"}]
    source.write_text(json.dumps(entries), encoding="utf-8")

    snapshot = lm.snapshot_json_file(source, tmp_path / "snapshots", name="execution_queue")

    assert snapshot.path.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")
    assert snapshot.path.stat().st_mode & stat.S_IWUSR == 0
    lm.verify_snapshot(snapshot)

    # The source file is untouched: still present, still writable.
    assert source.stat().st_mode & stat.S_IWUSR != 0


# --- dependency order: no database needed ------------------------------------


def _spec(table: str, references: dict[str, str] | None = None) -> MirroredTable:
    return MirroredTable(table=table, columns=("id",), references=references or {})


def test_wave_order_respects_declared_references() -> None:
    specs = {
        "task": _spec("task"),
        "session": _spec("session", {"task_id": "task"}),
        "run": _spec("run", {"task_id": "task", "session_id": "session"}),
        "run_event": _spec("run_event", {"run_id": "run"}),
    }
    waves = lm.wave_order(specs)
    assert waves == [["task"], ["session"], ["run"], ["run_event"]]


def test_wave_order_puts_independent_tables_in_one_wave() -> None:
    specs = {"owner_item": _spec("owner_item"), "digest_item": _spec("digest_item")}
    waves = lm.wave_order(specs)
    assert waves == [["digest_item", "owner_item"]]


def test_wave_order_raises_on_a_cycle() -> None:
    specs = {
        "a": _spec("a", {"b_id": "b"}),
        "b": _spec("b", {"a_id": "a"}),
    }
    with pytest.raises(ValueError, match="foreign-key cycle"):
        lm.wave_order(specs)


def test_wave_order_over_the_real_registry_is_acyclic_and_covers_every_mirror() -> None:
    """The live equivalent of `tests/db/test_schema_correspondence.py`'s
    acyclic-order proof, but sourced from the mirrors' own declarations
    rather than re-deriving the graph from `information_schema` -- this is
    what the importer actually walks."""
    from command_center.db import mirror_registry

    waves = lm.wave_order()
    flattened = [table for wave in waves for table in wave]
    assert sorted(flattened) == sorted(mirror_registry.mirror_classes())
    assert len(flattened) == len(set(flattened))


# --- cutover / rollback: no database needed ----------------------------------


def _clean_report() -> lm.ReconciliationReport:
    return lm.ReconciliationReport(generated_at="now", snapshot_sha256={}, tables=[])


def _dirty_report() -> lm.ReconciliationReport:
    table = lm.TableReconciliation(table="task", source_rows=1, mirror_rows=0, differences=[{"id": "x"}])
    return lm.ReconciliationReport(generated_at="now", snapshot_sha256={}, tables=[table])


def test_lock_legacy_sources_refuses_without_a_clean_reconciliation(tmp_path: Path) -> None:
    target = tmp_path / "runtime.db"
    target.write_bytes(b"not touched")
    with pytest.raises(ValueError, match="not clean"):
        lm.lock_legacy_sources([target], reconciliation=_dirty_report())
    # Refused before touching the filesystem.
    assert target.stat().st_mode & stat.S_IWUSR != 0


def test_lock_and_unlock_legacy_sources_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "runtime.db"
    target.write_bytes(b"legacy contents")

    locked = lm.lock_legacy_sources([target], reconciliation=_clean_report())
    assert locked == [target]
    assert target.stat().st_mode & stat.S_IWUSR == 0
    with pytest.raises(PermissionError):
        target.open("ab")

    # Locking again changes nothing further and reports no newly-locked paths.
    assert lm.lock_legacy_sources([target], reconciliation=_clean_report()) == []

    unlocked = lm.unlock_legacy_sources([target])
    assert unlocked == [target]
    assert target.stat().st_mode & stat.S_IWUSR != 0
    with target.open("ab"):
        pass  # writable again; the legacy source's contents were never touched
    assert target.read_bytes() == b"legacy contents"


def test_write_report_persists_json(tmp_path: Path) -> None:
    report = _dirty_report()
    out = tmp_path / "reports" / "reconciliation.json"
    lm.write_report(report, out)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["clean"] is False
    assert payload["tables"][0]["table"] == "task"


def test_read_reconciliation_report_round_trips_through_write_report(tmp_path: Path) -> None:
    report = _dirty_report()
    out = tmp_path / "reconciliation-report.json"
    lm.write_report(report, out)

    restored = lm.read_reconciliation_report(out)

    assert restored.generated_at == report.generated_at
    assert restored.snapshot_sha256 == report.snapshot_sha256
    assert restored.clean is False and report.clean is False
    assert [t.table for t in restored.tables] == [t.table for t in report.tables]
    assert restored.tables[0].differences == report.tables[0].differences


# --- against a real PostgreSQL ------------------------------------------------


def _seed_runtime_db(db_path: Path) -> dict:
    """A small, dependency-connected slice of real legacy data: a standalone
    table (`owner_item`), an identity-keyed child journal (`run_event`, whose
    ids are SQLite's own `lastrowid`), and the family that makes `run` wave
    3's bottleneck (`task` -> `session` -> `run` -> `run_event`)."""
    runtime_core.migrate(db_path)
    owner_item = runtime_wave1.create_owner_item(db_path, title="ship the migration")
    task = runtime_execution.create_task(db_path, project="P", title="T", task_type="implementation")
    session = runtime_execution.create_session(
        db_path, task_id=task["id"], project="P", repository_path="/repo"
    )
    run = runtime_execution.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="P",
        task_type="implementation",
        repository_path="/repo",
        prompt="do the thing",
        is_resume=False,
    )
    runtime_execution.append_run_event(db_path, run["id"], "lifecycle", {"lifecycle": "started"})
    runtime_execution.append_run_event(db_path, run["id"], "lifecycle", {"lifecycle": "finished"})
    return {"owner_item": owner_item, "task": task, "session": session, "run": run}


def test_import_snapshot_loads_a_dependency_chain_and_resyncs_identity(
    tmp_path: Path, pg_connection_factory
) -> None:
    db_path = tmp_path / "runtime.db"
    seeded = _seed_runtime_db(db_path)
    snapshot = lm.snapshot_sqlite_db(db_path, tmp_path / "snapshots")

    report = lm.import_snapshot(snapshot, connection_factory=pg_connection_factory)

    assert report.ok, [t for t in report.tables if not t.ok]
    by_table = {t.table: t for t in report.tables}
    assert by_table["owner_item"].imported == 1
    assert by_table["task"].imported == 1
    assert by_table["session"].imported == 1
    assert by_table["run"].imported == 1
    assert by_table["run_event"].imported == 2

    # run_event.id came from SQLite's own autoincrement; the sequence must be
    # advanced past it or the first native PostgreSQL insert after cutover
    # collides with an imported row.
    assert "run_event" in report.identity_resyncs
    assert report.identity_resyncs["run_event"] >= 2

    with pg_connection_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM task")
            assert [row[0] for row in cur.fetchall()] == [seeded["task"]["id"]]
            cur.execute("SELECT run_id, seq FROM run_event ORDER BY seq")
            assert [row[0] for row in cur.fetchall()] == [seeded["run"]["id"], seeded["run"]["id"]]


def test_import_snapshot_is_idempotent(tmp_path: Path, pg_connection_factory) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_runtime_db(db_path)
    snapshot = lm.snapshot_sqlite_db(db_path, tmp_path / "snapshots")

    first = lm.import_snapshot(snapshot, connection_factory=pg_connection_factory)
    second = lm.import_snapshot(snapshot, connection_factory=pg_connection_factory)

    assert first.ok and second.ok
    with pg_connection_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM task")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT COUNT(*) FROM run_event")
            assert cur.fetchone()[0] == 2


def test_reconcile_is_clean_after_import_and_dirty_before_it(
    tmp_path: Path, pg_connection_factory
) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_runtime_db(db_path)
    snapshot = lm.snapshot_sqlite_db(db_path, tmp_path / "snapshots")

    before = lm.reconcile(snapshot, connection_factory=pg_connection_factory)
    assert not before.clean
    by_table = {t.table: t for t in before.tables}
    assert by_table["task"].differences  # nothing imported yet: every row missing

    lm.import_snapshot(snapshot, connection_factory=pg_connection_factory)

    after = lm.reconcile(snapshot, connection_factory=pg_connection_factory)
    assert after.clean, [t for t in after.tables if not t.clean]
    assert after.snapshot_sha256["sqlite"] == snapshot.sha256


def test_reconcile_refuses_a_snapshot_that_changed_since_it_was_taken(
    tmp_path: Path, pg_connection_factory
) -> None:
    db_path = tmp_path / "runtime.db"
    _seed_runtime_db(db_path)
    snapshot = lm.snapshot_sqlite_db(db_path, tmp_path / "snapshots")
    snapshot.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    snapshot.path.write_bytes(snapshot.path.read_bytes() + b"\x00")

    with pytest.raises(ValueError, match="no longer matches its checksum"):
        lm.reconcile(snapshot, connection_factory=pg_connection_factory)


def test_queue_entry_imports_and_reconciles_from_its_own_json_authority(
    tmp_path: Path, pg_connection_factory
) -> None:
    """`queue_entry`'s authority is JSON, not the SQLite snapshot -- see
    `command_center/db/queue_store.py`. Order is part of what is compared:
    two identical entries in a different order must not reconcile clean."""
    queue_path = tmp_path / "execution_queue.json"
    entries = [
        {
            "id": "q1",
            "task_id": "t1",
            "project": "P",
            "state": "waiting",
            "reason": None,
            "run_id": None,
            "added_at": "2026-08-13T00:00:00",
            "evaluated_at": None,
            "launched_at": None,
        },
        {
            "id": "q2",
            "task_id": "t2",
            "project": "P",
            "state": "ready",
            "reason": None,
            "run_id": None,
            "added_at": "2026-08-13T00:00:01",
            "evaluated_at": "2026-08-13T00:00:02",
            "launched_at": None,
        },
    ]
    queue_path.write_text(json.dumps(entries), encoding="utf-8")
    queue_snapshot = lm.snapshot_json_file(queue_path, tmp_path / "snapshots", name="execution_queue")

    empty_db = tmp_path / "runtime.db"
    runtime_core.migrate(empty_db)
    sqlite_snapshot = lm.snapshot_sqlite_db(empty_db, tmp_path / "snapshots")

    lm.import_snapshot(
        sqlite_snapshot, connection_factory=pg_connection_factory, queue_entries=entries
    )
    report = lm.reconcile(
        sqlite_snapshot,
        connection_factory=pg_connection_factory,
        queue_snapshot=queue_snapshot,
        queue_entries=entries,
    )
    assert report.clean, [t for t in report.tables if not t.clean]
    assert report.snapshot_sha256["queue_json"] == queue_snapshot.sha256

    reordered = list(reversed(entries))
    dirty = lm.reconcile(
        sqlite_snapshot,
        connection_factory=pg_connection_factory,
        queue_snapshot=queue_snapshot,
        queue_entries=reordered,
    )
    queue_table = next(t for t in dirty.tables if t.table == "queue_entry")
    assert not queue_table.clean
    assert any(d["id"] == "__order__" for d in queue_table.differences)
