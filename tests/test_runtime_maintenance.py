"""W4 #193: rollback-safe retention — backup → archive → prune → integrity."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from command_center.runtime import db, maintenance


def _make_run(db_path: Path, name: str) -> dict:
    task = db.create_task(
        db_path, project="AIOS", title=name, task_type="implementation"
    )
    session = db.create_session(
        db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/repo"
    )
    return db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="implementation",
        repository_path="/tmp/repo",
        prompt=name,
        is_resume=False,
    )


def _seed(db_path: Path, *, old_events: int, fresh_events: int) -> tuple[str, str]:
    db.migrate(db_path)
    old_run = _make_run(db_path, "old")
    fresh_run = _make_run(db_path, "fresh")
    for i in range(old_events):
        db.append_run_event(db_path, old_run["id"], "stream_event", {"n": i})
    for i in range(fresh_events):
        db.append_run_event(db_path, fresh_run["id"], "stream_event", {"n": i})
    stale = (datetime.now() - timedelta(days=90)).isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                "UPDATE run SET state='COMPLETED', completed_at=? WHERE id=?",
                (stale, old_run["id"]),
            )
            conn.execute(
                "UPDATE run SET state='COMPLETED', completed_at=? WHERE id=?",
                (datetime.now().isoformat(timespec="seconds"), fresh_run["id"]),
            )
    return old_run["id"], fresh_run["id"]


def _event_count(db_path: Path, run_id: str) -> int:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM run_event WHERE run_id=?", (run_id,)
        ).fetchone()
        return int(row["c"])


def test_archive_and_prune_archives_exactly_what_it_deletes(tmp_path):
    db_path = tmp_path / "runtime.db"
    old_run, fresh_run = _seed(db_path, old_events=7, fresh_events=3)

    report = maintenance.archive_and_prune(
        db_path, retention_days=30, archive_dir=tmp_path / "cold"
    )

    assert report["archived_events"] == report["pruned_events"] == 7
    assert report["integrity_check"] == "ok"
    assert _event_count(db_path, old_run) == 0
    assert _event_count(db_path, fresh_run) == 3  # fresh history untouched

    with gzip.open(report["archive_path"], "rt", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle]
    assert len(lines) == 7
    assert {line["run_id"] for line in lines} == {old_run}
    digest = hashlib.sha256()
    with gzip.open(report["archive_path"], "rt", encoding="utf-8") as handle:
        for line in handle:
            digest.update(line.rstrip("\n").encode("utf-8"))
    assert digest.hexdigest() == report["archive_sha256"]


def test_rehearsal_runs_on_a_copy_and_proves_original_untouched(tmp_path):
    db_path = tmp_path / "runtime.db"
    old_run, _ = _seed(db_path, old_events=5, fresh_events=2)
    before = db_path.read_bytes()

    report = maintenance.rehearse(
        db_path, retention_days=30, archive_dir=tmp_path / "cold", vacuum=True
    )

    assert report["mode"] == "rehearsal"
    assert report["original_untouched"] is True
    assert report["archived_events"] == 5
    assert db_path.read_bytes() == before
    assert _event_count(db_path, old_run) == 5  # original still has history


def test_restore_backup_is_a_proven_rollback(tmp_path):
    db_path = tmp_path / "runtime.db"
    old_run, _ = _seed(db_path, old_events=4, fresh_events=1)

    report = maintenance.archive_and_prune(
        db_path, retention_days=30, archive_dir=tmp_path / "cold"
    )
    assert _event_count(db_path, old_run) == 0

    maintenance.restore_backup(Path(report["backup_path"]), db_path)
    assert _event_count(db_path, old_run) == 4  # pre-maintenance state restored


def test_zero_or_negative_retention_is_refused(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed(db_path, old_events=1, fresh_events=0)
    with pytest.raises(maintenance.MaintenanceError, match="positive"):
        maintenance.archive_and_prune(
            db_path, retention_days=0, archive_dir=tmp_path / "cold"
        )


def test_restore_refuses_missing_or_corrupt_backup(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed(db_path, old_events=1, fresh_events=0)
    with pytest.raises(maintenance.MaintenanceError, match="does not exist"):
        maintenance.restore_backup(tmp_path / "nope.db", db_path)
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a database")
    with pytest.raises((maintenance.MaintenanceError, sqlite3.DatabaseError)):
        maintenance.restore_backup(corrupt, db_path)


def _count_gzip_members(path: Path) -> int:
    """Count concatenated gzip members by their magic-number headers."""
    return Path(path).read_bytes().count(b"\x1f\x8b\x08")


def test_archive_and_prune_batches_across_multiple_transactions(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    old_run, fresh_run = _seed(db_path, old_events=13, fresh_events=2)

    transaction_calls = []
    real_transaction = maintenance.transaction

    @contextmanager
    def spy_transaction(conn):
        transaction_calls.append(1)
        with real_transaction(conn) as c:
            yield c

    monkeypatch.setattr(maintenance, "transaction", spy_transaction)

    report = maintenance.archive_and_prune(
        db_path, retention_days=30, archive_dir=tmp_path / "cold", batch_size=5
    )

    # 13 rows at 5/batch: three batches of data (5, 5, 3), each its own
    # committed transaction — never one unbounded delete.
    assert len(transaction_calls) == 3
    assert report["archived_events"] == report["pruned_events"] == 13
    assert _event_count(db_path, old_run) == 0
    assert _event_count(db_path, fresh_run) == 2

    # Each batch wrote its own independently finalized gzip member; the
    # archive is a concatenation of them, not one member built in memory.
    assert _count_gzip_members(report["archive_path"]) == 3
    with gzip.open(report["archive_path"], "rt", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle]
    assert len(lines) == 13


def test_apply_runtime_retention_batches_across_multiple_transactions(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "runtime.db"
    old_run, fresh_run = _seed(db_path, old_events=13, fresh_events=2)

    transaction_calls = []
    real_transaction = db.transaction

    @contextmanager
    def spy_transaction(conn):
        transaction_calls.append(1)
        with real_transaction(conn) as c:
            yield c

    monkeypatch.setattr(db, "transaction", spy_transaction)

    removed = db.apply_runtime_retention(db_path, retention_days=30, batch_size=5)

    assert len(transaction_calls) == 3
    assert removed == 13
    assert _event_count(db_path, old_run) == 0
    assert _event_count(db_path, fresh_run) == 2


def test_archive_directory_is_fsynced_before_the_first_batch_commits(
    tmp_path, monkeypatch
):
    """VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE-REM-REM: a power loss must not
    be able to drop the archive file's own directory entry after its paired
    deletion has already committed, so the directory fsync has to happen
    before any batch transaction is allowed to commit."""
    db_path = tmp_path / "runtime.db"
    _seed(db_path, old_events=4, fresh_events=0)

    order = []
    real_fsync_directory = maintenance._fsync_directory

    def spy_fsync_directory(path):
        order.append("fsync_directory")
        return real_fsync_directory(path)

    real_transaction = maintenance.transaction

    @contextmanager
    def spy_transaction(conn):
        order.append("transaction_begin")
        with real_transaction(conn) as c:
            yield c
        order.append("transaction_commit")

    monkeypatch.setattr(maintenance, "_fsync_directory", spy_fsync_directory)
    monkeypatch.setattr(maintenance, "transaction", spy_transaction)

    maintenance.archive_and_prune(
        db_path, retention_days=30, archive_dir=tmp_path / "cold", batch_size=2
    )

    assert "fsync_directory" in order
    first_commit = order.index("transaction_commit")
    assert order.index("fsync_directory") < first_commit


def test_archive_creation_never_appends_to_a_same_second_leftover_file(
    tmp_path, monkeypatch
):
    """VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE-REM-REM: a same-second retry
    (e.g. after the previous run crashed mid-finalization) must claim a fresh
    file rather than opening the existing timestamped name in append mode,
    which would splice new data onto a possibly truncated, possibly
    unrelated prior file."""
    db_path = tmp_path / "runtime.db"
    old_run, _ = _seed(db_path, old_events=3, fresh_events=0)
    cold = tmp_path / "cold"
    cold.mkdir()

    monkeypatch.setattr(maintenance, "_timestamp", lambda: "20260101T000000")

    stale_archive = cold / "run-events-20260101T000000.jsonl.gz"
    stale_archive.write_bytes(b"leftover-truncated-member-from-a-crashed-run")
    stale_backup = cold / "runtime-backup-20260101T000000.db"
    stale_backup.write_bytes(b"leftover-backup-from-a-crashed-run")

    report = maintenance.archive_and_prune(
        db_path, retention_days=30, archive_dir=cold
    )

    assert report["archive_path"] != str(stale_archive)
    assert report["backup_path"] != str(stale_backup)
    # The leftover files were never opened for append or overwritten.
    assert stale_archive.read_bytes() == b"leftover-truncated-member-from-a-crashed-run"
    assert stale_backup.read_bytes() == b"leftover-backup-from-a-crashed-run"

    with gzip.open(report["archive_path"], "rt", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle]
    assert len(lines) == 3
    assert _event_count(db_path, old_run) == 0


def test_finalization_failure_leaves_that_batchs_delete_uncommitted(
    tmp_path, monkeypatch
):
    """VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE-REM: failure must be tested
    from the wrapped gzip handle's finalization (`close()`, where the footer
    is written), not just from a failing `write()` — a footer failure is
    exactly the "archive volume fills while writing the footer" scenario
    that must never leave a committed deletion with no durable archive."""
    db_path = tmp_path / "runtime.db"
    old_run, _ = _seed(db_path, old_events=8, fresh_events=0)
    cold = tmp_path / "cold"

    real_gzip_file = gzip.GzipFile
    close_calls = {"n": 0}

    class FailOnSecondClose(real_gzip_file):
        def close(self):
            close_calls["n"] += 1
            if close_calls["n"] == 2:
                raise OSError("simulated disk-full while writing gzip footer")
            return super().close()

    monkeypatch.setattr(maintenance.gzip, "GzipFile", FailOnSecondClose)

    with pytest.raises(OSError, match="simulated disk-full"):
        maintenance.archive_and_prune(
            db_path, retention_days=30, archive_dir=cold, batch_size=3
        )

    # Batch 1 (3 rows) finalized durably before its delete committed.
    # Batch 2's finalization failed *before* its delete ran, so those rows
    # must still be present — never deleted with no durable archive.
    assert _event_count(db_path, old_run) == 8 - 3

    # The archive file itself has a trailing truncated member (batch 2's
    # header and compressed data, written before its footer failed) — that's
    # fine, since batch 2's rows were never deleted. The complete leading
    # member for batch 1's *deleted* rows must still be fully decompressible.
    [archive_path] = list(cold.glob("run-events-*.jsonl.gz"))
    lines = []
    with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
        try:
            for line in handle:
                lines.append(json.loads(line))
        except EOFError:
            pass
    assert len(lines) == 3
