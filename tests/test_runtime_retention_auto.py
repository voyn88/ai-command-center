"""VOYN-W0-AICC-RETENTION-NO-DRYRUN: `db.migrate()` runs automatically from
every service construction (supervisor, autonomy, council, digest,
networking, conflicts, marketplace, api services — ~15 call sites). With
`AICC_RUNTIME_RETENTION_DAYS` set, it used to delete `run_event` rows straight
away via the bare `apply_runtime_retention`, with no archive and no way to
rehearse it first — unlike `maintenance.archive_and_prune`/`rehearse`, which
back up, cold-archive and integrity-check before pruning, and can run against
a throwaway copy instead of the original.

These tests assert the automatic path now shares that safety: it refuses to
delete without `AICC_RUNTIME_RETENTION_ARCHIVE_DIR` configured, routes through
`archive_and_prune` when it is, and honors `AICC_RUNTIME_RETENTION_DRY_RUN=1`
by rehearsing against a copy instead of touching the original.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from command_center.runtime import db


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


def _seed_old_run(db_path: Path) -> str:
    db.migrate(db_path)
    run = _make_run(db_path, "old")
    db.append_run_event(db_path, run["id"], "stream_event", {"n": 0})
    stale = (datetime.now() - timedelta(days=90)).isoformat(timespec="seconds")
    with db.connect(db_path) as conn:
        with db.transaction(conn):
            conn.execute(
                "UPDATE run SET state='COMPLETED', completed_at=? WHERE id=?",
                (stale, run["id"]),
            )
    return run["id"]


def _event_count(db_path: Path, run_id: str) -> int:
    with db.connect(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM run_event WHERE run_id=?", (run_id,)
        ).fetchone()
        return int(row["c"])


def test_migrate_does_not_delete_without_an_archive_dir(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    old_run = _seed_old_run(db_path)

    monkeypatch.setenv("AICC_RUNTIME_RETENTION_DAYS", "30")
    monkeypatch.delenv("AICC_RUNTIME_RETENTION_ARCHIVE_DIR", raising=False)

    db.migrate(db_path)  # the path every service construction runs

    assert _event_count(db_path, old_run) == 1


def test_migrate_archives_and_prunes_when_configured(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    old_run = _seed_old_run(db_path)
    archive_dir = tmp_path / "cold"

    monkeypatch.setenv("AICC_RUNTIME_RETENTION_DAYS", "30")
    monkeypatch.setenv("AICC_RUNTIME_RETENTION_ARCHIVE_DIR", str(archive_dir))

    db.migrate(db_path)

    assert _event_count(db_path, old_run) == 0
    assert list(archive_dir.glob("run-events-*.jsonl.gz"))
    assert list(archive_dir.glob("runtime-backup-*.db"))


def test_migrate_dry_run_rehearses_without_touching_the_original(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "runtime.db"
    old_run = _seed_old_run(db_path)
    archive_dir = tmp_path / "cold"

    monkeypatch.setenv("AICC_RUNTIME_RETENTION_DAYS", "30")
    monkeypatch.setenv("AICC_RUNTIME_RETENTION_ARCHIVE_DIR", str(archive_dir))
    monkeypatch.setenv("AICC_RUNTIME_RETENTION_DRY_RUN", "1")

    db.migrate(db_path)

    assert _event_count(db_path, old_run) == 1  # original untouched
    assert list(archive_dir.glob("rehearsal-*.db"))
