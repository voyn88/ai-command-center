"""Offline schema-upgrade window: backup first, time each step, backfill later.

VOYN-W0-AICC-SQLITE-SCHEMA-16-TO-23 named a real gap: the live ``data/runtime.db``
sat at schema 16 while ``origin/main`` had already shipped 23. The seven steps
17..23 had never been timed against that ~346 MB file, and the cutover needs its
own maintenance window with a SQLite-API backup **before** any provenance or
mirror backfill runs.

``migrate()`` still owns the everyday path (schema + a bounded provenance
backfill). This module is the deliberate offline window:

* take a consistent backup first;
* apply each pending migration one at a time and record wall time;
* optionally stop at a named version (the ticket's 23, or 24 before the
  offline finalization cutover);
* leave provenance backfill off — that is a separate post-schema step the
  operator schedules after the backup exists.

``rehearse_schema_upgrade`` runs the identical sequence against a copy and
proves the original file is byte-identical afterwards, the same contract
``runtime.maintenance.rehearse`` uses for retention.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from command_center.runtime import db


class SchemaUpgradeError(RuntimeError):
    """A schema-upgrade step failed; the target database was left unmodified
    relative to the failed step (prior successful steps may already be recorded)."""


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def backup_database(db_path: Path, target: Path) -> None:
    """Consistent snapshot via the SQLite backup API (safe with live WAL)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as source, sqlite3.connect(target) as destination:
        source.backup(destination)


def _integrity_ok(conn: sqlite3.Connection) -> bool:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return bool(row) and row[0] == "ok"


def apply_pending_migrations(
    db_path: Path,
    *,
    stop_after_version: int | None = None,
    run_backfill: bool = False,
    backfill_limit: int = 500,
) -> dict:
    """Apply each pending migration one-by-one and return a timing report.

    Unlike ``db.migrate``, this refuses to run provenance backfill unless the
    caller opts in — the upgrade window's backup must exist before that step.
    Crossing into schema 25 still requires the explicit offline finalization
    cutover; this helper raises the same gate ``migrate`` does.
    """
    if stop_after_version is not None and stop_after_version < 1:
        raise SchemaUpgradeError("stop_after_version must be a positive schema version")
    if backfill_limit < 0:
        raise SchemaUpgradeError("backfill_limit must be non-negative")

    db_path = Path(db_path)
    steps: list[dict] = []
    with db._migration_file_lock(db_path):
        with db.connect(db_path) as conn:
            db._retry_on_busy(lambda: conn.execute(db._SCHEMA_VERSION_TABLE_SQL))
            row = conn.execute(
                "SELECT MAX(version) AS v FROM schema_version"
            ).fetchone()
            current = int(row["v"]) if row and row["v"] is not None else 0
            initial_version = current
            for version, step in db.MIGRATIONS:
                if version <= current:
                    continue
                if stop_after_version is not None and version > stop_after_version:
                    break
                if version == 25 and 0 < initial_version < 25:
                    raise db.FinalizationClaimCutoverRequired(
                        "existing pre-v25 runtime schema requires the explicit "
                        "offline finalization cutover before v25"
                    )
                started = time.monotonic()
                if callable(step):
                    db._retry_on_busy(lambda step=step: step(conn))
                else:
                    db._retry_on_busy(lambda step=step: conn.executescript(step))
                try:
                    with db.transaction(conn):
                        conn.execute(
                            "INSERT INTO schema_version (version, applied_at) "
                            "VALUES (?, ?)",
                            (version, db.iso_now()),
                        )
                except sqlite3.IntegrityError:
                    pass
                elapsed = time.monotonic() - started
                current = version
                steps.append(
                    {
                        "version": version,
                        "elapsed_seconds": round(elapsed, 6),
                    }
                )
            if current > db.SCHEMA_VERSION:
                raise SchemaUpgradeError(
                    f"runtime schema v{current} is newer than supported "
                    f"v{db.SCHEMA_VERSION}"
                )
            if current == 25:
                db._validate_finalization_claim_schema(conn)
            db._stamp_timestamp_zone(conn)
            integrity = _integrity_ok(conn)

    backfilled = 0
    if run_backfill and current >= 13:
        backfilled = db.backfill_run_provenance(db_path, limit=backfill_limit)

    return {
        "db_path": str(db_path),
        "initial_version": initial_version,
        "final_version": current,
        "stop_after_version": stop_after_version,
        "steps": steps,
        "total_elapsed_seconds": round(
            sum(step["elapsed_seconds"] for step in steps), 6
        ),
        "backfill_run": bool(run_backfill),
        "backfill_inserted": backfilled,
        "integrity_check": "ok" if integrity else "failed",
    }


def rehearse_schema_upgrade(
    db_path: Path,
    *,
    archive_dir: Path,
    stop_after_version: int | None = 23,
    run_backfill: bool = False,
) -> dict:
    """Time the upgrade on a copy; prove the live database is untouched.

    Default ``stop_after_version=23`` matches the ticket's named gap (16→23).
    Pass 24 to include the ``finalized_at`` partial index, or ``None`` to walk
    as far as this binary can without the offline v25 cutover.
    """
    db_path = Path(db_path)
    archive_dir = Path(archive_dir)
    archive_dir.mkdir(parents=True, exist_ok=True)
    if not db_path.is_file():
        raise SchemaUpgradeError(f"database does not exist: {db_path}")

    before = hashlib.sha256(db_path.read_bytes()).hexdigest()
    stamp = _timestamp()
    backup_path = archive_dir / f"runtime-schema-backup-{stamp}.db"
    rehearsal_db = archive_dir / f"schema-rehearsal-{stamp}.db"
    backup_database(db_path, backup_path)
    backup_database(db_path, rehearsal_db)
    try:
        report = apply_pending_migrations(
            rehearsal_db,
            stop_after_version=stop_after_version,
            run_backfill=run_backfill,
        )
    finally:
        after = hashlib.sha256(db_path.read_bytes()).hexdigest()

    report.update(
        {
            "mode": "rehearsal",
            "rehearsal_db": str(rehearsal_db),
            "backup_path": str(backup_path),
            "original_db": str(db_path),
            "original_untouched": before == after,
            "original_sha256": before,
        }
    )
    if not report["original_untouched"]:
        raise SchemaUpgradeError("rehearsal modified the original database")
    if report["integrity_check"] != "ok":
        raise SchemaUpgradeError("rehearsal copy failed integrity_check")
    return report
