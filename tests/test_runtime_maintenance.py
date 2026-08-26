"""W4 #193: rollback-safe retention — backup → archive → prune → integrity."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
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


def test_archive_and_prune_batches_instead_of_one_unbounded_pass(tmp_path, monkeypatch):
    """The whole point of batching: memory and lock-hold are bounded by the
    batch size, not by how much unpruned history has piled up. Force a batch
    size far smaller than the row count and check the fixed-size batches are
    what actually ran, not just that the end state matches a single pass."""
    db_path = tmp_path / "runtime.db"
    old_run, fresh_run = _seed(db_path, old_events=11, fresh_events=2)
    monkeypatch.setattr(maintenance, "_BATCH_SIZE", 3)

    batch_sizes = []
    real_append = maintenance._append_archive_batch

    def spying_append(archive_path, rows, digest):
        batch_sizes.append(len(rows))
        return real_append(archive_path, rows, digest)

    monkeypatch.setattr(maintenance, "_append_archive_batch", spying_append)

    report = maintenance.archive_and_prune(
        db_path, retention_days=30, archive_dir=tmp_path / "cold"
    )

    assert report["archived_events"] == report["pruned_events"] == 11
    assert _event_count(db_path, old_run) == 0
    assert _event_count(db_path, fresh_run) == 2
    # 11 rows at a batch size of 3: four batches of [3, 3, 3, 2].
    assert batch_sizes == [3, 3, 3, 2]

    with gzip.open(report["archive_path"], "rt", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle]
    assert len(lines) == 11
    assert {line["run_id"] for line in lines} == {old_run}


def test_batch_finalize_failure_does_not_lose_already_committed_batches(
    tmp_path, monkeypatch
):
    """Regression for the rejected PR #386 finding: a prior version committed
    every batch's deletion while the gzip stream stayed open, so a failure
    finalizing the archive (footer write, disk full) after the fact could
    lose already-committed rows with an unreadable archive to show for it.

    Failing inside the gzip handle's `close()` -- not `write()` -- targets
    exactly the finalize-on-`__exit__` path the rejected PR's failure test
    did not cover. The second batch's footer write is made to fail; the
    first batch must already be durably archived (readable on its own) and
    deleted, and the second batch's rows must still be in the database (its
    transaction never committed) instead of vanished.
    """
    db_path = tmp_path / "runtime.db"
    old_run, _ = _seed(db_path, old_events=7, fresh_events=0)
    monkeypatch.setattr(maintenance, "_BATCH_SIZE", 3)

    real_close = gzip.GzipFile.close
    calls = {"n": 0}

    def flaky_close(self):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated disk-full while finalizing gzip footer")
        return real_close(self)

    monkeypatch.setattr(gzip.GzipFile, "close", flaky_close)

    with pytest.raises(OSError, match="simulated disk-full"):
        maintenance.archive_and_prune(
            db_path, retention_days=30, archive_dir=tmp_path / "cold"
        )

    # First batch (3 rows) finalized and committed before the second batch's
    # finalize failed -- it must survive, not roll back with everything else.
    assert _event_count(db_path, old_run) == 4  # 7 - 3 (first batch only)
    # The second batch was never deleted, so nothing is lost -- only its
    # archive attempt is an incomplete trailing member in this run's file;
    # a retry starts a fresh, timestamped archive file.

    archive_path = next((tmp_path / "cold").glob("run-events-*.jsonl.gz"))
    lines = []
    with pytest.raises(EOFError):
        with gzip.open(archive_path, "rt", encoding="utf-8") as handle:
            for line in handle:
                lines.append(json.loads(line))
    # The already-finalized first batch is intact and readable up to the
    # truncated second member -- the failure does not corrupt or hide it.
    assert len(lines) == 3


def test_restore_refuses_missing_or_corrupt_backup(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed(db_path, old_events=1, fresh_events=0)
    with pytest.raises(maintenance.MaintenanceError, match="does not exist"):
        maintenance.restore_backup(tmp_path / "nope.db", db_path)
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a database")
    with pytest.raises((maintenance.MaintenanceError, sqlite3.DatabaseError)):
        maintenance.restore_backup(corrupt, db_path)
