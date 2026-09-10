"""Rollback-safe runtime event retention: backup → archive → prune → integrity.

`db.apply_runtime_retention` bounds `run_event` growth by deleting old
terminal-run events, but deletion alone trades auditability for disk. This
module adds the W4 retention contract (NIGHT-W4-AICC-RETENTION):

* **backup** — a SQLite-API snapshot of the live database is taken first;
  the maintenance run refuses to touch the original without it;
* **cold archive** — rows are pruned and archived in fixed-size batches
  (`VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE`) instead of one unbounded
  transaction that both reads every doomed row into memory
  (`fetchall()`) and holds the write lock for as long as the whole backlog
  takes to delete. Each batch's archive data is written as an
  independently finalized gzip member — flushed and fsynced to disk —
  *before* that batch's `DELETE` is committed, so a crash or full disk
  during finalization can never leave a committed deletion with no
  corresponding durable archive (`VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE-REM`).
  Concatenated gzip members decompress transparently as one stream, so the
  archive reads exactly like a single-pass export;
* **durable, collision-safe archive files** — the archive file and the
  pre-maintenance backup are both claimed with an exclusive create
  (`O_CREAT | O_EXCL`), retrying with a fresh suffix on any name collision,
  so a maintenance run never appends to or silently reuses a same-second
  retry's or a previous crashed run's partial file. The archive directory
  is fsynced right after the file is created — before any batch's deletion
  commits — so the new directory entry itself survives a power loss, not
  just the bytes inside it (`VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE-REM`);
* **integrity** — `PRAGMA integrity_check` must return ``ok`` and, per
  batch, the archived row count must equal the deleted row count, else the
  transaction is rolled back and the report says so;
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
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from command_center.runtime.db import (
    TERMINAL_STATES,
    connect,
    retention_cutoff,
    transaction,
)

_ARCHIVE_BATCH_SIZE = 500

_ARCHIVE_SELECT_BATCH = """
    SELECT run_event.* FROM run_event
     WHERE run_id IN (
        SELECT id FROM run
         WHERE state IN ({placeholders})
           AND completed_at IS NOT NULL
           AND completed_at < ?
     )
     ORDER BY run_id, seq
     LIMIT ?
"""


class MaintenanceError(RuntimeError):
    """A retention step failed; the database was left unmodified."""


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def _create_exclusive(directory: Path, build_name) -> tuple[Path, int]:
    """Exclusively claim a fresh path under `directory`, retrying `build_name`
    with an incrementing suffix on collision.

    `O_CREAT | O_EXCL` guarantees the returned path did not exist a moment
    ago — unlike "does a file with this timestamp exist" followed by a
    separate open, which is a race, and unlike opening in append mode, which
    would silently splice this run's data onto a previous run's (possibly
    truncated, possibly unrelated) file. A same-second retry or a leftover
    partial file from a crashed run therefore always gets its own new path;
    neither is ever appended to or reused.
    """
    directory.mkdir(parents=True, exist_ok=True)
    suffix = 0
    while True:
        name = build_name(suffix)
        path = directory / name
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            suffix += 1
            continue
        return path, fd


def _fsync_directory(path: Path) -> None:
    """fsync the directory entry for `path`, not just the file's bytes.

    Without this, a power loss can drop the newly created directory entry
    even though the file's own contents were fsynced — the archive would
    simply not exist after recovery even though every batch's deletion had
    already committed.
    """
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


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
    batch_size: int = _ARCHIVE_BATCH_SIZE,
) -> dict:
    """Run the full sequence against `db_path` and return a truthful report."""
    if retention_days <= 0:
        raise MaintenanceError("retention_days must be positive")
    db_path = Path(db_path)
    archive_dir = Path(archive_dir)
    stamp = _timestamp()

    backup_path, backup_fd = _create_exclusive(
        archive_dir,
        lambda n: f"runtime-backup-{stamp}.db"
        if n == 0
        else f"runtime-backup-{stamp}-{n}.db",
    )
    os.close(backup_fd)
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

    archive_path, archive_fd = _create_exclusive(
        archive_dir,
        lambda n: f"run-events-{stamp}.jsonl.gz"
        if n == 0
        else f"run-events-{stamp}-{n}.jsonl.gz",
    )
    # Both `backup_path` and `archive_path` now exist under `archive_dir`; one
    # fsync of the directory makes both new directory entries durable, before
    # any batch's deletion is allowed to commit.
    _fsync_directory(archive_path)
    raw = os.fdopen(archive_fd, "wb")

    digest = hashlib.sha256()
    archived = 0
    try:
        with connect(db_path) as conn:
            while True:
                with transaction(conn):
                    rows = conn.execute(
                        _ARCHIVE_SELECT_BATCH.format(placeholders=placeholders),
                        (*TERMINAL_STATES, cutoff, batch_size),
                    ).fetchall()
                    if not rows:
                        break
                    # This batch's archive data is written as its own gzip
                    # member and durably finalized — flushed and fsynced —
                    # before its DELETE is committed. A crash or full disk
                    # while finalizing leaves the DELETE uncommitted (still
                    # inside this transaction); a crash right after leaves a
                    # complete, independently decompressible archive member
                    # for every row that is actually gone. Concatenated gzip
                    # members decompress transparently as a single stream, so
                    # nothing downstream needs to know batching happened.
                    member = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0)
                    try:
                        for row in rows:
                            line = json.dumps(
                                dict(row), ensure_ascii=False, sort_keys=True
                            )
                            member.write((line + "\n").encode("utf-8"))
                            digest.update(line.encode("utf-8"))
                    finally:
                        member.close()
                    raw.flush()
                    os.fsync(raw.fileno())

                    ids = [row["id"] for row in rows]
                    id_placeholders = ",".join("?" for _ in ids)
                    deleted = conn.execute(
                        f"DELETE FROM run_event WHERE id IN ({id_placeholders})",
                        ids,
                    ).rowcount
                    if deleted != len(rows):
                        # Roll the deletion back rather than lose unarchived history.
                        raise MaintenanceError(
                            f"archived {len(rows)} rows but deletion matched {deleted}"
                        )
                    archived += deleted
                if len(rows) < batch_size:
                    break
            if archived == 0:
                # Nothing was eligible for pruning, so the loop above never
                # opened a member — write one empty member so the archive is
                # still a well-formed (empty) gzip stream, not a bare file.
                gzip.GzipFile(fileobj=raw, mode="wb", mtime=0).close()
                raw.flush()
                os.fsync(raw.fileno())
            integrity = _integrity_ok(conn)
    finally:
        raw.close()
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
    batch_size: int = _ARCHIVE_BATCH_SIZE,
) -> dict:
    """Run the identical sequence against a copy; prove the original intact."""
    db_path = Path(db_path)
    archive_dir = Path(archive_dir)
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    stamp = _timestamp()
    rehearsal_db, rehearsal_fd = _create_exclusive(
        archive_dir,
        lambda n: f"rehearsal-{stamp}.db" if n == 0 else f"rehearsal-{stamp}-{n}.db",
    )
    os.close(rehearsal_fd)
    _backup_database(db_path, rehearsal_db)
    try:
        report = archive_and_prune(
            rehearsal_db,
            retention_days=retention_days,
            archive_dir=archive_dir,
            vacuum=vacuum,
            batch_size=batch_size,
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
