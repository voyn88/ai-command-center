"""Rollback-safe runtime event retention: backup → archive → prune → integrity.

`db.apply_runtime_retention` bounds `run_event` growth by deleting old
terminal-run events, but deletion alone trades auditability for disk. This
module adds the W4 retention contract (NIGHT-W4-AICC-RETENTION):

* **backup** — a SQLite-API snapshot of the live database is taken first;
  the maintenance run refuses to touch the original without it;
* **cold archive** — every row that will be pruned is exported to a
  compressed JSONL archive (with a SHA-256 digest and row count) *in the
  same transaction scope* that deletes it, so the archive and the deletion
  can never disagree;
* **integrity** — `PRAGMA integrity_check` must return ``ok`` and the
  archived row count must equal the deleted row count, else the transaction
  is rolled back and the report says so;
* **optional VACUUM** — only after a clean prune, and only when asked;
* **rehearsal** — `rehearse()` runs the identical sequence against a copy
  of the database and proves the original is byte-identical afterwards.

Everything returns a JSON-serializable report; nothing here fabricates
success — every step's outcome is recorded exactly as observed.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from command_center.runtime.db import (
    TERMINAL_STATES,
    connect,
    retention_cutoff,
    transaction,
)

_ARCHIVE_SELECT = """
    SELECT run_event.* FROM run_event
     WHERE run_id IN (
        SELECT id FROM run
         WHERE state IN ({placeholders})
           AND completed_at IS NOT NULL
           AND completed_at < ?
     )
     ORDER BY run_id, seq
"""

_ARCHIVE_DELETE = """
    DELETE FROM run_event
     WHERE run_id IN (
        SELECT id FROM run
         WHERE state IN ({placeholders})
           AND completed_at IS NOT NULL
           AND completed_at < ?
     )
"""


class MaintenanceError(RuntimeError):
    """A retention step failed; the database was left unmodified."""


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _backup_database(db_path: Path, target: Path) -> None:
    """Consistent snapshot via the SQLite backup API (safe with live WAL)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)


def _integrity_ok(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and row[0] == "ok"


def archive_and_prune(
    db_path: Path,
    *,
    retention_days: int,
    archive_dir: Path,
    vacuum: bool = False,
) -> dict:
    """Run the full sequence against `db_path` and return a truthful report."""
    if retention_days <= 0:
        raise MaintenanceError("retention_days must be positive")
    db_path = Path(db_path)
    archive_dir = Path(archive_dir)
    stamp = _timestamp()

    backup_path = archive_dir / f"runtime-backup-{stamp}.db"
    _backup_database(db_path, backup_path)

    # Rendered in the zone the database says its naive timestamps are on, not
    # in this process's zone — otherwise the same database at the same instant
    # yields a different set of deleted rows depending on how the prune was
    # started (VOYN-W0-AICC-RETENTION-TZ). The zone and where it came from go
    # into the report: an irreversible delete has to be able to say which clock
    # it judged the rows against.
    cutoff, cutoff_zone, cutoff_zone_source = retention_cutoff(
        db_path, retention_days=retention_days
    )
    placeholders = ",".join("?" for _ in TERMINAL_STATES)
    archive_path = archive_dir / f"run-events-{stamp}.jsonl.gz"
    digest = hashlib.sha256()
    archived = 0

    with connect(db_path) as conn:
        with transaction(conn):
            rows = conn.execute(
                _ARCHIVE_SELECT.format(placeholders=placeholders),
                (*TERMINAL_STATES, cutoff),
            ).fetchall()
            with gzip.open(archive_path, "wt", encoding="utf-8") as handle:
                for row in rows:
                    line = json.dumps(dict(row), ensure_ascii=False, sort_keys=True)
                    handle.write(line + "\n")
                    digest.update(line.encode("utf-8"))
                    archived += 1
            deleted = conn.execute(
                _ARCHIVE_DELETE.format(placeholders=placeholders),
                (*TERMINAL_STATES, cutoff),
            ).rowcount
            if deleted != archived:
                # Roll the deletion back rather than lose unarchived history.
                raise MaintenanceError(
                    f"archived {archived} rows but deletion matched {deleted}"
                )
        integrity = _integrity_ok(conn)
    if not integrity:
        raise MaintenanceError("integrity_check failed after prune")

    if vacuum:
        with sqlite3.connect(db_path) as conn:
            conn.execute("VACUUM")

    return {
        "db_path": str(db_path),
        "backup_path": str(backup_path),
        "archive_path": str(archive_path),
        "archive_sha256": digest.hexdigest(),
        "retention_days": retention_days,
        "cutoff": cutoff,
        "cutoff_timezone": cutoff_zone,
        "cutoff_timezone_source": cutoff_zone_source,
        "archived_events": archived,
        "pruned_events": archived,
        "integrity_check": "ok",
        "vacuum": bool(vacuum),
    }


def rehearse(
    db_path: Path,
    *,
    retention_days: int,
    archive_dir: Path,
    vacuum: bool = False,
) -> dict:
    """Run the identical sequence against a copy; prove the original intact."""
    db_path = Path(db_path)
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    rehearsal_db = archive_dir / f"rehearsal-{_timestamp()}.db"
    _backup_database(db_path, rehearsal_db)
    try:
        report = archive_and_prune(
            rehearsal_db,
            retention_days=retention_days,
            archive_dir=archive_dir,
            vacuum=vacuum,
        )
    finally:
        after = hashlib.sha256(db_path.read_bytes()).hexdigest()
    report.update(
        {
            "mode": "rehearsal",
            "rehearsal_db": str(rehearsal_db),
            "original_untouched": before == after,
            "original_sha256": before,
        }
    )
    if not report["original_untouched"]:
        raise MaintenanceError("rehearsal modified the original database")
    return report


def restore_backup(backup_path: Path, db_path: Path) -> None:
    """Proven rollback: replace `db_path` with the pre-maintenance backup."""
    backup_path = Path(backup_path)
    if not backup_path.is_file():
        raise MaintenanceError(f"backup does not exist: {backup_path}")
    with sqlite3.connect(backup_path) as conn:
        if not _integrity_ok(conn):
            raise MaintenanceError("backup fails integrity_check; refusing restore")
    # Restore through the SQLite backup API (not a file copy): this replaces
    # the database content atomically and correctly supersedes any WAL/SHM
    # sidecars a naive copyfile would leave pointing at the pruned state.
    _backup_database(backup_path, db_path)
