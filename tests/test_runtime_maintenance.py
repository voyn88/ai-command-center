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


def test_archive_and_prune_uses_fixed_batches(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    old_run, _ = _seed(db_path, old_events=7, fresh_events=0)
    monkeypatch.setattr(maintenance, "RETENTION_BATCH_SIZE", 2)

    report = maintenance.archive_and_prune(
        db_path, retention_days=30, archive_dir=tmp_path / "cold"
    )

    assert report["retention_batches"] == 4
    assert report["archived_events"] == report["pruned_events"] == 7
    assert _event_count(db_path, old_run) == 0

    with gzip.open(report["archive_path"], "rt", encoding="utf-8") as handle:
        assert [json.loads(line)["seq"] for line in handle] == list(range(1, 8))


def test_startup_retention_uses_fixed_batches(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    old_run, _ = _seed(db_path, old_events=7, fresh_events=0)
    monkeypatch.setattr(db, "RETENTION_BATCH_SIZE", 2)

    assert db.apply_runtime_retention(db_path, retention_days=30) == 7
    assert _event_count(db_path, old_run) == 0


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


class _FailingArchive:
    """A gzip handle that refuses to write past `fail_after` lines.

    Stands in for the archive volume filling up part-way through a prune.
    """

    def __init__(self, inner, fail_after: int) -> None:
        self._inner = inner
        self._fail_after = fail_after
        self.writes = 0

    def write(self, data):
        self.writes += 1
        if self.writes > self._fail_after:
            raise OSError("archive volume full")
        return self._inner.write(data)

    def flush(self):
        return self._inner.flush()

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._inner.__exit__(*exc_info)


def test_archive_and_prune_commits_each_batch_separately(tmp_path, monkeypatch):
    """Failing part-way keeps the batches that already committed — and every
    row it deleted is in the archive. That is what "bounded" buys: the unit of
    work is a batch, not the whole database.
    """
    db_path = tmp_path / "runtime.db"
    old_run, _ = _seed(db_path, old_events=7, fresh_events=0)
    monkeypatch.setattr(maintenance, "RETENTION_BATCH_SIZE", 2)
    real_open = gzip.open
    # Two whole batches (4 rows) get written; the first row of batch 3 fails.
    monkeypatch.setattr(
        maintenance.gzip,
        "open",
        lambda *a, **kw: _FailingArchive(real_open(*a, **kw), fail_after=4),
    )

    with pytest.raises(OSError, match="archive volume full"):
        maintenance.archive_and_prune(
            db_path, retention_days=30, archive_dir=tmp_path / "cold"
        )
    monkeypatch.undo()

    assert _event_count(db_path, old_run) == 3  # batch 3 rolled back, 1-2 kept
    (archive,) = sorted((tmp_path / "cold").glob("run-events-*.jsonl.gz"))
    with real_open(archive, "rt", encoding="utf-8") as handle:
        archived = [json.loads(line)["seq"] for line in handle]
    assert archived == [1, 2, 3, 4]  # nothing was deleted without being archived


def test_startup_retention_commits_each_batch_separately(tmp_path, monkeypatch):
    """Same bound on the automatic startup path, and it resumes where it
    stopped — a neglected database is drained over several runs if need be.
    """
    db_path = tmp_path / "runtime.db"
    old_run, _ = _seed(db_path, old_events=7, fresh_events=0)
    monkeypatch.setattr(db, "RETENTION_BATCH_SIZE", 2)
    real_transaction = db.transaction
    seen = {"batches": 0}

    @contextmanager
    def failing_transaction(conn):
        seen["batches"] += 1
        with real_transaction(conn) as inner:
            yield inner
            if seen["batches"] == 3:
                raise RuntimeError("interrupted")

    monkeypatch.setattr(db, "transaction", failing_transaction)
    with pytest.raises(RuntimeError, match="interrupted"):
        db.apply_runtime_retention(db_path, retention_days=30)
    assert _event_count(db_path, old_run) == 3  # batches 1-2 survived the abort

    monkeypatch.setattr(db, "transaction", real_transaction)
    assert db.apply_runtime_retention(db_path, retention_days=30) == 3
    assert _event_count(db_path, old_run) == 0
