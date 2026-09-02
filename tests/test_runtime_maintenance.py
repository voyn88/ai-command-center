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


def _seed_bloated(db_path: Path, *, old_events: int) -> str:
    """Like `_seed`, but bulk-inserts so a large `old_events` count stays fast
    (VOYN-W0-AICC-RUNTIME-DB-BLOAT: enough rows to push the freelist share of
    the file well past any threshold under test)."""
    db.migrate(db_path)
    old_run = _make_run(db_path, "old")
    payload_json = json.dumps({"blob": "x" * 512})
    now = datetime.now().isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.executemany(
                """INSERT INTO run_event (run_id, seq, event_type, payload_json, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                [
                    (old_run["id"], i, "stream_event", payload_json, now)
                    for i in range(1, old_events + 1)
                ],
            )
    stale = (datetime.now() - timedelta(days=90)).isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                "UPDATE run SET state='COMPLETED', completed_at=? WHERE id=?",
                (stale, old_run["id"]),
            )
    return old_run["id"]


def test_archive_and_prune_vacuum_free_ratio_threshold_fires_when_bloated(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed_bloated(db_path, old_events=4000)

    report = maintenance.archive_and_prune(
        db_path,
        retention_days=30,
        archive_dir=tmp_path / "cold",
        vacuum_free_ratio=0.3,
    )

    assert report["vacuum"] is True
    assert report["vacuum_free_ratio"] == 0.3
    assert report["free_page_ratio_before_vacuum"] > 0.3
    assert db.free_page_ratio(db_path) < 0.1


def test_archive_and_prune_vacuum_free_ratio_threshold_skips_when_not_bloated(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed(db_path, old_events=5, fresh_events=2)

    report = maintenance.archive_and_prune(
        db_path,
        retention_days=30,
        archive_dir=tmp_path / "cold",
        vacuum_free_ratio=0.9,
    )

    assert report["vacuum"] is False
    assert report["archived_events"] == 5  # pruning still ran


def test_archive_and_prune_vacuum_true_takes_precedence_over_ratio(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed(db_path, old_events=5, fresh_events=2)

    report = maintenance.archive_and_prune(
        db_path,
        retention_days=30,
        archive_dir=tmp_path / "cold",
        vacuum=True,
        vacuum_free_ratio=0.9,  # would not fire on its own
    )

    assert report["vacuum"] is True
    assert report["free_page_ratio_before_vacuum"] is None  # ratio never checked


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
