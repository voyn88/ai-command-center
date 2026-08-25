from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from command_center import storage
from command_center.db.legacy_migration import (
    MigrationRefused,
    _canonical_hash,
    _ordered_mirrors,
    _queue_rows,
    create_snapshot,
    load_snapshot,
    migrate,
    retire_legacy_authority,
    verify_retirement,
)
from command_center.runtime.db import core


def _data_dir(tmp_path: Path) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    core.migrate(data / "runtime.db")
    (data / "execution_queue.json").write_text(
        json.dumps([{"id": "q2", "state": "ready"}, {"id": "q1", "state": "waiting"}]),
        encoding="utf-8",
    )
    (data / "runs.jsonl").write_text('{"id":"legacy-1"}\n', encoding="utf-8")
    return data


def _clean_report(snapshot) -> dict:
    tables = {
        cls.spec.table: {
            "source_count": 0,
            "target_count": 0,
            "source_checksum": "same",
            "target_checksum": "same",
            "relationship_columns": sorted(cls.spec.references),
            "relationships_match": True,
            "event_order_matches": True,
            "clean": True,
        }
        for cls in _ordered_mirrors()
    }
    tables["queue_entry"] = {
        "source_count": 2,
        "target_count": 2,
        "source_checksum": "same",
        "target_checksum": "same",
        "order_matches": True,
        "clean": True,
    }
    return {
        "format": 1,
        "clean": True,
        "tables": tables,
        "source_foreign_key_errors": [],
        "snapshot_manifest_sha256": hashlib.sha256(
            (snapshot.directory / "manifest.json").read_bytes()
        ).hexdigest(),
    }


def test_snapshot_is_consistent_checksummed_and_read_only(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    snapshot = create_snapshot(data, tmp_path / "rollback")

    loaded = load_snapshot(snapshot.directory)
    assert set(loaded.manifest["files"]) == {
        "runtime.db",
        "execution_queue.json",
        "runs.jsonl",
    }
    assert loaded.database.stat().st_mode & 0o222 == 0
    conn = sqlite3.connect(f"file:{loaded.database}?mode=ro", uri=True)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def test_snapshot_archives_unrecognised_json_stores_for_rollback(
    tmp_path: Path,
) -> None:
    data = _data_dir(tmp_path)
    (data / "integration_registry.json").write_text("{}", encoding="utf-8")
    (data / "aios_task_map.json").write_text("{}", encoding="utf-8")

    snapshot = create_snapshot(data, tmp_path / "rollback")

    assert "integration_registry.json" in snapshot.manifest["files"]
    assert "aios_task_map.json" in snapshot.manifest["files"]
    archived = snapshot.directory / "integration_registry.json"
    assert archived.stat().st_mode & 0o222 == 0


def test_snapshot_tampering_is_refused_before_import(tmp_path: Path) -> None:
    snapshot = create_snapshot(_data_dir(tmp_path), tmp_path / "rollback")
    queue = snapshot.directory / "execution_queue.json"
    queue.chmod(0o640)
    queue.write_text("[]", encoding="utf-8")

    with pytest.raises(MigrationRefused, match="checksum mismatch"):
        load_snapshot(snapshot.directory)


def test_snapshot_manifest_cannot_escape_snapshot_directory(tmp_path: Path) -> None:
    snapshot = create_snapshot(_data_dir(tmp_path), tmp_path / "rollback")
    manifest = snapshot.directory / "manifest.json"
    manifest.chmod(0o640)
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["files"]["../outside"] = "0" * 64
    manifest.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(MigrationRefused, match="invalid file entry"):
        load_snapshot(snapshot.directory)


def test_queue_snapshot_preserves_json_authority_order(tmp_path: Path) -> None:
    snapshot = create_snapshot(_data_dir(tmp_path), tmp_path / "rollback")
    assert [row["id"] for row in _queue_rows(snapshot)] == ["q2", "q1"]


def test_import_order_places_every_parent_before_its_children() -> None:
    ordered = _ordered_mirrors()
    position = {cls.spec.table: index for index, cls in enumerate(ordered)}
    assert len(position) == 32
    for cls in ordered:
        for parent in cls.spec.references.values():
            assert position[parent] < position[cls.spec.table]


def test_reconciliation_checksum_is_deterministic() -> None:
    rows = [{"b": 2, "a": 1}, {"id": "x"}]
    assert _canonical_hash(rows) == _canonical_hash(rows)
    assert _canonical_hash(rows) != _canonical_hash(list(reversed(rows)))


def test_import_invalidates_an_older_green_report_before_target_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snapshot = create_snapshot(_data_dir(tmp_path), tmp_path / "rollback")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_clean_report(snapshot)), encoding="utf-8")
    monkeypatch.setattr(
        "command_center.db.legacy_migration._ordered_mirrors", lambda: []
    )

    class BrokenConnection:
        def transaction(self):
            raise RuntimeError("target unavailable")

    with pytest.raises(RuntimeError, match="target unavailable"):
        migrate(snapshot.directory, BrokenConnection(), report)

    assert json.loads(report.read_text(encoding="utf-8")) == {
        "clean": False,
        "format": 1,
        "snapshot_manifest_sha256": hashlib.sha256(
            (snapshot.directory / "manifest.json").read_bytes()
        ).hexdigest(),
        "state": "import_in_progress",
    }


def test_cutover_requires_clean_report_and_retires_replaced_sources(
    tmp_path: Path,
) -> None:
    data = _data_dir(tmp_path)
    snapshot = create_snapshot(data, tmp_path / "rollback")
    report = tmp_path / "report.json"
    report.write_text(
        json.dumps(_clean_report(snapshot)),
        encoding="utf-8",
    )

    marker = retire_legacy_authority(data, snapshot.directory, report)

    assert marker.is_file()
    # SQLite refuses a write to a mode-440 file itself, so the file stays put.
    assert (data / "runtime.db").stat().st_mode & 0o222 == 0
    # The queue only stops being an authority once the name is gone.
    assert not (data / "execution_queue.json").exists()
    retired = data / "execution_queue.json.retired"
    assert retired.is_file()
    assert retired.stat().st_mode & 0o222 == 0


def test_cutover_refuses_data_written_after_snapshot(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    snapshot = create_snapshot(data, tmp_path / "rollback")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_clean_report(snapshot)), encoding="utf-8")
    (data / "runs.jsonl").write_text('{"id":"late"}\n', encoding="utf-8")

    with pytest.raises(MigrationRefused, match="changed after snapshot: runs.jsonl"):
        retire_legacy_authority(data, snapshot.directory, report)


def test_cutover_refuses_legacy_file_created_after_snapshot(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    snapshot = create_snapshot(data, tmp_path / "rollback")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_clean_report(snapshot)), encoding="utf-8")
    (data / "activity.jsonl").write_text('{"id":"late"}\n', encoding="utf-8")

    with pytest.raises(MigrationRefused, match="appeared after snapshot"):
        retire_legacy_authority(data, snapshot.directory, report)


def test_cutover_leaves_stores_postgres_does_not_replace_writable(
    tmp_path: Path,
) -> None:
    """`activity.jsonl` and friends have no table here; retiring them is breakage.

    `append_jsonl` opens the file for append, so clearing the write bits does
    not "retire" an unmapped store — it takes a working feature offline.
    """
    data = _data_dir(tmp_path)
    extra = data / "integration_registry.json"
    extra.write_text("{}", encoding="utf-8")
    activity = data / "activity.jsonl"
    storage.append_jsonl(activity, {"event": "before"})
    snapshot = create_snapshot(data, tmp_path / "rollback")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_clean_report(snapshot)), encoding="utf-8")

    marker = retire_legacy_authority(data, snapshot.directory, report)

    assert extra.stat().st_mode & 0o222 != 0
    storage.append_jsonl(activity, {"event": "after cutover"})
    recorded = json.loads(marker.read_text(encoding="utf-8"))
    assert set(recorded["retained_writable"]) == {
        "activity.jsonl",
        "integration_registry.json",
        "runs.jsonl",
    }
    # ...and they are still in the snapshot, so rollback stays complete.
    assert "activity.jsonl" in snapshot.manifest["files"]


def test_cutover_refuses_sqlite_rows_written_after_snapshot(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    snapshot = create_snapshot(data, tmp_path / "rollback")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_clean_report(snapshot)), encoding="utf-8")
    with sqlite3.connect(data / "runtime.db") as conn:
        conn.execute(
            "INSERT INTO task (id, project, title, task_type, created_at, updated_at) "
            "VALUES ('late', 'AICC', 'late', 'implementation', 'now', 'now')"
        )

    with pytest.raises(MigrationRefused, match="runtime.db/task"):
        retire_legacy_authority(data, snapshot.directory, report)


def test_real_postgres_import_is_idempotent_and_reconciles(
    tmp_path: Path, admin_conn, pg_connection_factory
) -> None:
    data = _data_dir(tmp_path)
    with sqlite3.connect(data / "runtime.db") as conn:
        conn.execute(
            "INSERT INTO task "
            "(id, project, title, task_type, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "task-1",
                "AICC",
                "migrate me",
                "implementation",
                "2026-08-24T12:00:00",
                "2026-08-24T12:00:00",
            ),
        )
    snapshot = create_snapshot(data, tmp_path / "rollback")

    first = migrate(snapshot.directory, admin_conn, tmp_path / "first.json")
    second = migrate(snapshot.directory, admin_conn, tmp_path / "second.json")

    assert first["clean"] is True
    assert second["clean"] is True
    assert second["tables"]["task"]["source_count"] == 1
    assert second["tables"]["task"]["target_count"] == 1


def _cutover(tmp_path: Path, data: Path):
    snapshot = create_snapshot(data, tmp_path / "rollback")
    report = tmp_path / "report.json"
    report.write_text(json.dumps(_clean_report(snapshot)), encoding="utf-8")
    retire_legacy_authority(data, snapshot.directory, report)
    return snapshot, report


def test_clearing_write_bits_would_not_have_retired_the_queue(tmp_path: Path) -> None:
    """Why the queue is renamed rather than chmod'ed.

    `execution_queue.save_queue` persists through `storage.atomic_write_json`,
    which writes a sibling temp file and `os.replace`s it over the target. POSIX
    checks the *directory* for that rename, never the target's mode — so a
    read-only `execution_queue.json` is still a fully writable authority.
    """
    data = _data_dir(tmp_path)
    queue = data / "execution_queue.json"
    queue.chmod(0o440)

    storage.atomic_write_json(queue, [{"id": "written-through-a-read-only-file"}])

    assert json.loads(queue.read_text(encoding="utf-8"))[0]["id"].startswith("written")


def test_verify_accepts_a_clean_cutover(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    _cutover(tmp_path, data)

    state = verify_retirement(data)

    assert {entry["file"] for entry in state["retired"]} == {
        "runtime.db",
        "execution_queue.json",
    }
    assert state["retained_writable"] == ["runs.jsonl"]


def test_verify_reports_a_queue_a_legacy_writer_re_created(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    _cutover(tmp_path, data)

    # Exactly what a legacy deployment that was never stopped does on its next
    # write: a brand-new authoritative file beside the retired one.
    storage.atomic_write_json(data / "execution_queue.json", [{"id": "q3"}])

    with pytest.raises(MigrationRefused, match="re-created after cutover"):
        verify_retirement(data)


def test_verify_reports_a_retired_store_made_writable_again(tmp_path: Path) -> None:
    data = _data_dir(tmp_path)
    _cutover(tmp_path, data)
    (data / "runtime.db").chmod(0o640)

    with pytest.raises(MigrationRefused, match="runtime.db is writable again"):
        verify_retirement(data)


def test_verify_refuses_before_any_cutover(tmp_path: Path) -> None:
    with pytest.raises(MigrationRefused, match="never retired"):
        verify_retirement(_data_dir(tmp_path))


def test_cutover_is_idempotent(tmp_path: Path) -> None:
    """A re-run must not read the already-moved queue as a changed source."""
    data = _data_dir(tmp_path)
    snapshot, report = _cutover(tmp_path, data)

    marker = retire_legacy_authority(data, snapshot.directory, report)

    assert json.loads(marker.read_text(encoding="utf-8"))["retired"] == [
        {
            "file": "execution_queue.json",
            "how": "renamed",
            "rollback_path": "execution_queue.json.retired",
        },
        {"file": "runtime.db", "how": "read_only_in_place"},
    ]
    verify_retirement(data)
