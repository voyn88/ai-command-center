"""VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE: retention deletes must run in
fixed-size batches, not one unbounded `DELETE` (and `archive_and_prune` must
stream rows to the archive instead of `fetchall()`-ing the whole doomed set).

These tests seed enough old events that a single fixed batch size cannot
cover them in one pass, then prove: (a) every doomed row is still archived
and/or deleted, and (b) the work actually happened across more than one
batch/transaction rather than a single unbounded sweep.
"""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from command_center.runtime import db, maintenance

OLD_EVENTS = 25
BATCH_SIZE = 10


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


def _count_transactions(monkeypatch, target_module) -> list[int]:
    """Wrap `target_module.transaction` with a call counter, returning the
    mutable one-element list the count accumulates into."""
    calls = [0]
    original = target_module.transaction

    def _counting_transaction(conn):
        calls[0] += 1
        return original(conn)

    monkeypatch.setattr(target_module, "transaction", _counting_transaction)
    return calls


def test_apply_runtime_retention_deletes_in_multiple_bounded_batches(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "runtime.db"
    old_run, fresh_run = _seed(db_path, old_events=OLD_EVENTS, fresh_events=3)

    calls = _count_transactions(monkeypatch, db)

    removed = db.apply_runtime_retention(
        db_path, retention_days=30, batch_size=BATCH_SIZE
    )

    assert removed == OLD_EVENTS
    assert _event_count(db_path, old_run) == 0
    assert _event_count(db_path, fresh_run) == 3  # fresh history untouched
    # 3 batches of work (10, 10, 5) plus the final empty check that ends the
    # loop — never one transaction covering all 25 rows.
    assert calls[0] == 4


def test_archive_and_prune_archives_and_deletes_in_multiple_bounded_batches(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "runtime.db"
    old_run, fresh_run = _seed(db_path, old_events=OLD_EVENTS, fresh_events=3)

    calls = _count_transactions(monkeypatch, maintenance)

    report = maintenance.archive_and_prune(
        db_path,
        retention_days=30,
        archive_dir=tmp_path / "cold",
        batch_size=BATCH_SIZE,
    )

    assert report["archived_events"] == report["pruned_events"] == OLD_EVENTS
    assert report["integrity_check"] == "ok"
    assert _event_count(db_path, old_run) == 0
    assert _event_count(db_path, fresh_run) == 3
    assert calls[0] == 4

    with gzip.open(report["archive_path"], "rt", encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle]
    assert len(lines) == OLD_EVENTS
    assert {line["run_id"] for line in lines} == {old_run}


def test_archive_and_prune_rejects_non_positive_batch_size(tmp_path):
    db_path = tmp_path / "runtime.db"
    _seed(db_path, old_events=1, fresh_events=0)
    with pytest.raises(maintenance.MaintenanceError, match="batch_size"):
        maintenance.archive_and_prune(
            db_path, retention_days=30, archive_dir=tmp_path / "cold", batch_size=0
        )
