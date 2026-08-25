"""Verified, repeatable legacy-state import into PostgreSQL.

The migration reads a cold SQLite backup and the JSON queue copied beside it;
it never reads a changing live file while inserting.  Every input is hashed,
rows are imported in foreign-key order with their original keys, and the same
snapshot can be applied repeatedly because all writes are upserts (the queue is
an atomic replacement).  The JSON report is the cutover gate and the snapshot
is the rollback artefact.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import sqlite3
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from command_center.db.queue_store import QUEUE_ENTRY_COLUMNS, PostgresQueueMirror
from command_center.db.table_mirror import PostgresTableMirror

LEGACY_SUFFIXES = frozenset({".json", ".jsonl"})
RETIREMENT_MARKER = ".legacy-authority-retired.json"
RETIRED_SUFFIX = ".retired"

#: The file-shaped legacy stores PostgreSQL is now authoritative for.
#:
#: Deliberately *not* "every JSON file in ``data/``".  Only ``runtime.db`` and
#: this set have a target here: the other stores in that directory —
#: ``activity.jsonl``, ``tasks.json``, ``chats.json``, ``project_config.json``,
#: the v1.2 ``runs.jsonl`` journal ``runs_read`` still merges — have no row
#: mapping in `0001_initial.up.sql` and are still the live authority for
#: features this schema does not cover.  Retiring those would not complete a
#: migration, it would break them; they are archived for rollback and listed in
#: the marker as retained instead.
REPLACED_JSON_STORES = frozenset({"execution_queue.json"})


class MigrationRefused(RuntimeError):
    """The snapshot or reconciliation is not safe enough for cutover."""


@dataclass(frozen=True)
class Snapshot:
    directory: Path
    database: Path
    manifest: dict[str, Any]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _legacy_files(data_dir: Path) -> list[Path]:
    """Every file-shaped legacy store, including stores added by older builds."""
    return sorted(
        path
        for path in data_dir.iterdir()
        if path.is_file()
        and path.suffix in LEGACY_SUFFIXES
        and path.name != RETIREMENT_MARKER
    )


def _valid_snapshot_name(name: Any) -> bool:
    """Accept a single safe basename, never a manifest-controlled path."""
    return (
        isinstance(name, str)
        and Path(name).name == name
        and name not in {"", ".", "..", "manifest.json", RETIREMENT_MARKER}
        and (name == "runtime.db" or Path(name).suffix in LEGACY_SUFFIXES)
    )


def create_snapshot(data_dir: Path, output_dir: Path) -> Snapshot:
    """Take a transactionally consistent SQLite backup and hash every input."""
    source_db = data_dir / "runtime.db"
    if not source_db.is_file():
        raise MigrationRefused(f"missing SQLite authority: {source_db}")
    output_dir.mkdir(parents=True, exist_ok=False)
    copied_db = output_dir / "runtime.db"
    source = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    target = sqlite3.connect(copied_db)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()

    copied: list[str] = ["runtime.db"]
    for candidate in _legacy_files(data_dir):
        shutil.copyfile(candidate, output_dir / candidate.name)
        copied.append(candidate.name)
    manifest = {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": {name: _sha256(output_dir / name) for name in sorted(copied)},
    }
    _write_json(output_dir / "manifest.json", manifest)
    # A rollback copy must not accidentally become another writable authority.
    for name in (*copied, "manifest.json"):
        (output_dir / name).chmod(0o440)
    return Snapshot(output_dir, copied_db, manifest)


def load_snapshot(directory: Path) -> Snapshot:
    manifest_path = directory / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationRefused(f"invalid snapshot manifest: {exc}") from exc
    if manifest.get("format") != 1 or not isinstance(manifest.get("files"), dict):
        raise MigrationRefused("unsupported snapshot manifest format")
    files = manifest["files"]
    if not files or any(
        not _valid_snapshot_name(name)
        or not isinstance(expected, str)
        or len(expected) != 64
        for name, expected in files.items()
    ):
        raise MigrationRefused("snapshot manifest contains an invalid file entry")
    for name, expected in files.items():
        path = directory / name
        if not path.is_file() or _sha256(path) != expected:
            raise MigrationRefused(f"snapshot checksum mismatch: {name}")
    if "runtime.db" not in files:
        raise MigrationRefused("snapshot does not contain runtime.db")
    return Snapshot(directory, directory / "runtime.db", manifest)


def _mirrors() -> dict[str, type[PostgresTableMirror]]:
    package = Path(__file__).resolve().parent
    for path in sorted(package.glob("*_store.py")):
        importlib.import_module(f"command_center.db.{path.stem}")
    found: dict[str, type[PostgresTableMirror]] = {}
    pending = list(PostgresTableMirror.__subclasses__())
    while pending:
        cls = pending.pop()
        pending.extend(cls.__subclasses__())
        if cls.__module__.startswith("command_center.db."):
            previous = found.setdefault(cls.spec.table, cls)
            if previous is not cls:
                raise MigrationRefused(
                    f"duplicate mirror declaration: {cls.spec.table}"
                )
    return found


def _ordered_mirrors() -> list[type[PostgresTableMirror]]:
    remaining = _mirrors()
    ordered: list[type[PostgresTableMirror]] = []
    while remaining:
        ready = sorted(
            name
            for name, cls in remaining.items()
            if set(cls.spec.references.values()).isdisjoint(remaining)
        )
        if not ready:
            raise MigrationRefused(f"cyclic mirror references: {sorted(remaining)}")
        for name in ready:
            ordered.append(remaining.pop(name))
    return ordered


def _source_rows(snapshot: Snapshot, spec: Any) -> list[dict]:
    columns = ", ".join(spec.columns)
    order = ", ".join(spec.key_columns)
    conn = sqlite3.connect(f"file:{snapshot.database}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        statement = f"SELECT {columns} FROM {spec.table} ORDER BY {order}"
        return [dict(row) for row in conn.execute(statement)]
    finally:
        conn.close()


def _queue_rows(snapshot: Snapshot) -> list[dict]:
    path = snapshot.directory / "execution_queue.json"
    if not path.exists():
        # Older installations have only the already dual-written SQLite copy.
        conn = sqlite3.connect(f"file:{snapshot.database}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [
                {key: value for key, value in dict(row).items() if key != "position"}
                for row in conn.execute("SELECT * FROM queue_entry ORDER BY position")
            ]
        finally:
            conn.close()
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise MigrationRefused("execution_queue.json must contain a list of objects")
    return [
        {column: row.get(column) for column in QUEUE_ENTRY_COLUMNS} for row in value
    ]


def _canonical_hash(rows: list[dict]) -> str:
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


def _comparable_rows(rows: list[dict], spec: Any) -> list[dict]:
    return [
        {
            column: spec.codec.comparable(column, row.get(column))
            for column in spec.columns
        }
        for row in rows
    ]


def _event_order(rows: list[dict], spec: Any) -> list[list[Any]] | None:
    """Canonical parent/sequence order for journal tables, otherwise ``None``."""
    if "seq" not in spec.columns:
        return None
    parent_columns = tuple(spec.references)
    return [
        [
            *(row.get(column) for column in parent_columns),
            row.get("seq"),
            *(row.get(key) for key in spec.key_columns),
        ]
        for row in sorted(
            rows,
            key=lambda row: (
                tuple(row.get(column) for column in parent_columns)
                + (row.get("seq"),)
                + tuple(row.get(k) for k in spec.key_columns)
            ),
        )
    ]


def _relationships(rows: list[dict], spec: Any) -> list[list[Any]]:
    """Canonical child-key/foreign-key tuples for explicit relation evidence."""
    reference_columns = tuple(spec.references)
    return [
        [
            *(row.get(key) for key in spec.key_columns),
            *(row.get(column) for column in reference_columns),
        ]
        for row in rows
    ]


def migrate(snapshot_dir: Path, connection: Any, report_path: Path) -> dict[str, Any]:
    """Import one verified snapshot and write a machine-readable reconciliation."""
    snapshot = load_snapshot(snapshot_dir)
    # Invalidate a report left by an earlier successful run before touching the
    # target.  Otherwise a crash (including a failed COMMIT) could leave that
    # old green file available to ``legacy-freeze`` even though this attempt
    # never completed.
    _write_json(
        report_path,
        {
            "format": 1,
            "clean": False,
            "state": "import_in_progress",
            "snapshot_manifest_sha256": _sha256(snapshot.directory / "manifest.json"),
        },
    )
    mirrors = _ordered_mirrors()
    source: dict[str, list[dict]] = {
        cls.spec.table: _source_rows(snapshot, cls.spec) for cls in mirrors
    }
    source["queue_entry"] = _queue_rows(snapshot)

    factory = lambda: nullcontext(connection)  # noqa: E731 - protocol factory
    report: dict[str, Any]
    with connection.transaction():
        for cls in mirrors:
            mirror = cls(connection_factory=factory)
            for row in source[cls.spec.table]:
                mirror.upsert(row)
        PostgresQueueMirror(connection_factory=factory).replace_entries(
            source["queue_entry"]
        )
        for cls in mirrors:
            if cls.spec.identity:
                cls(connection_factory=factory).resync_identity()
        # Reconcile in the same target transaction as the writes. A report
        # produced after commit could describe a different database state.
        report = _reconcile(snapshot, source, mirrors, factory)
        if not report["clean"]:
            _write_json(report_path, report)
            raise MigrationRefused(f"reconciliation failed; see {report_path}")
    # Publish a green gate only after PostgreSQL accepted the commit. If commit
    # itself fails, an earlier green file must not authorize legacy-freeze.
    _write_json(report_path, report)
    return report


def _reconcile(
    snapshot: Snapshot,
    source: dict[str, list[dict]],
    mirrors: list[type[PostgresTableMirror]],
    factory: Any,
) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    clean = True
    for cls in mirrors:
        expected = source[cls.spec.table]
        actual = cls(connection_factory=factory).list_records()
        expected_by_key = {
            tuple(row.get(k) for k in cls.spec.key_columns): row for row in expected
        }
        actual_by_key = {
            tuple(row.get(k) for k in cls.spec.key_columns): row for row in actual
        }
        differences = []
        for key in sorted(set(expected_by_key) | set(actual_by_key), key=repr):
            left, right = expected_by_key.get(key), actual_by_key.get(key)
            if (
                left is None
                or right is None
                or any(
                    cls.spec.codec.comparable(c, left.get(c))
                    != cls.spec.codec.comparable(c, right.get(c))
                    for c in cls.spec.columns
                )
            ):
                differences.append(list(key))
        order_matches = _event_order(expected, cls.spec) == _event_order(
            actual, cls.spec
        )
        relationships_match = _relationships(expected, cls.spec) == _relationships(
            actual, cls.spec
        )
        expected_rows = _comparable_rows(expected, cls.spec)
        actual_rows = _comparable_rows(actual, cls.spec)
        table_clean = (
            expected_rows == actual_rows
            and not differences
            and order_matches
            and relationships_match
        )
        clean &= table_clean
        tables[cls.spec.table] = {
            "source_count": len(expected),
            "target_count": len(actual),
            "source_checksum": _canonical_hash(expected_rows),
            "target_checksum": _canonical_hash(actual_rows),
            "different_keys": differences,
            "relationship_columns": sorted(cls.spec.references),
            "relationships_match": relationships_match,
            "event_order_matches": order_matches,
            "clean": table_clean,
        }

    expected_queue = source["queue_entry"]
    actual_queue = PostgresQueueMirror(connection_factory=factory).list_entries()
    queue_clean = expected_queue == actual_queue
    clean &= queue_clean
    tables["queue_entry"] = {
        "source_count": len(expected_queue),
        "target_count": len(actual_queue),
        "source_checksum": _canonical_hash(expected_queue),
        "target_checksum": _canonical_hash(actual_queue),
        "order_matches": queue_clean,
        "clean": queue_clean,
    }

    with sqlite3.connect(f"file:{snapshot.database}?mode=ro", uri=True) as sqlite_conn:
        foreign_key_errors = [
            list(row) for row in sqlite_conn.execute("PRAGMA foreign_key_check")
        ]
    clean &= not foreign_key_errors
    report = {
        "format": 1,
        "snapshot": str(snapshot.directory),
        "snapshot_manifest_sha256": _sha256(snapshot.directory / "manifest.json"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tables": tables,
        "source_foreign_key_errors": foreign_key_errors,
        "legacy_files_archived": sorted(snapshot.manifest["files"]),
        "clean": clean,
    }
    return report


def _retirement_precheck(data_dir: Path, snapshot: Snapshot, report_path: Path) -> None:
    """Refuse unless the report is green and the sources still match the snapshot."""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationRefused(f"invalid reconciliation report: {exc}") from exc
    tables = report.get("tables")
    if (
        report.get("format") != 1
        or not report.get("clean")
        or not isinstance(tables, dict)
        or set(tables)
        != {cls.spec.table for cls in _ordered_mirrors()} | {"queue_entry"}
        or not all(
            isinstance(row, dict)
            and row.get("clean") is True
            and row.get("source_count") == row.get("target_count")
            and row.get("source_checksum") == row.get("target_checksum")
            and row.get("relationships_match", True) is True
            and row.get("event_order_matches", row.get("order_matches")) is True
            for row in tables.values()
        )
        or report.get("source_foreign_key_errors") != []
    ):
        raise MigrationRefused("refusing cutover without a clean reconciliation")
    if report.get("snapshot_manifest_sha256") != _sha256(
        snapshot.directory / "manifest.json"
    ):
        raise MigrationRefused("reconciliation report belongs to a different snapshot")

    for name, expected in snapshot.manifest["files"].items():
        if name == "runtime.db":
            continue  # SQLite backup bytes are not byte-identical to the live DB.
        live = data_dir / name
        if not live.is_file() and name in REPLACED_JSON_STORES:
            # Already retired by an earlier run of this command: the bytes moved
            # aside rather than changed, so this stays idempotent.
            retired = data_dir / (name + RETIRED_SUFFIX)
            if retired.is_file() and _sha256(retired) == expected:
                continue
        if not live.is_file() or _sha256(live) != expected:
            raise MigrationRefused(f"legacy source changed after snapshot: {name}")
    archived_legacy = set(snapshot.manifest["files"]) - {"runtime.db"}
    for live in _legacy_files(data_dir):
        if live.name not in archived_legacy:
            raise MigrationRefused(
                f"legacy source appeared after snapshot: {live.name}"
            )
    live_database = data_dir / "runtime.db"
    if not live_database.is_file():
        raise MigrationRefused("legacy source changed after snapshot: runtime.db")
    live_snapshot = Snapshot(data_dir, live_database, snapshot.manifest)
    for cls in _ordered_mirrors():
        archived = _comparable_rows(_source_rows(snapshot, cls.spec), cls.spec)
        live = _comparable_rows(_source_rows(live_snapshot, cls.spec), cls.spec)
        if archived != live:
            raise MigrationRefused(
                f"legacy source changed after snapshot: runtime.db/{cls.spec.table}"
            )


def retire_legacy_authority(
    data_dir: Path, snapshot_dir: Path, report_path: Path
) -> Path:
    """Take the stores PostgreSQL now owns out of service after a clean report.

    Separate from import on purpose: operators inspect the reconciliation before
    the authority flip, and the exact pre-cutover bytes stay in ``snapshot_dir``,
    which is the rollback.

    Retirement is *per store*, because one mechanism does not fit both, and the
    difference was measured rather than assumed:

    * ``runtime.db`` is cleared of its write bits and stays where it is. SQLite
      opens the file itself, so the mode bit is a real refusal ("attempt to
      write a readonly database"), and leaving the file in place is what keeps
      `runtime.db` from being silently re-created empty — an empty v2 store the
      app would happily write to is a *new* authority, which is worse than a
      stale one.
    * ``execution_queue.json`` is renamed aside, because clearing its write bits
      changes nothing at all. `execution_queue.save_queue` goes through
      `storage.atomic_write_json`, which writes a fresh temp file and
      `os.replace`s it over the target; POSIX checks the *directory* for that,
      never the target's mode. A chmod'd queue file is still fully writable, so
      only removing the authoritative name actually retires it. Anything that
      re-creates `execution_queue.json` afterwards is a legacy writer that was
      never stopped — which is exactly what `verify_retirement` reports.

    Every other file in ``data/`` is left alone; see `REPLACED_JSON_STORES`.
    """
    snapshot = load_snapshot(snapshot_dir)
    _retirement_precheck(data_dir, snapshot, report_path)

    retired: list[dict[str, str]] = []
    retained: list[str] = []
    for name in sorted(snapshot.manifest["files"]):
        source = data_dir / name
        if name == "runtime.db":
            if source.is_file():
                source.chmod(source.stat().st_mode & ~0o222)
                retired.append({"file": name, "how": "read_only_in_place"})
        elif name in REPLACED_JSON_STORES:
            moved = data_dir / (name + RETIRED_SUFFIX)
            if source.is_file():
                os.replace(source, moved)
            if moved.is_file():
                moved.chmod(moved.stat().st_mode & ~0o222)
                retired.append(
                    {"file": name, "how": "renamed", "rollback_path": moved.name}
                )
        elif source.is_file():
            retained.append(name)

    marker = data_dir / RETIREMENT_MARKER
    _write_json(
        marker,
        {
            "format": 1,
            "retired_at": datetime.now(timezone.utc).isoformat(),
            "snapshot": str(snapshot_dir.resolve()),
            "report": str(report_path.resolve()),
            "retired": retired,
            # Named, not hidden: these stores are still writable *and still
            # correct*, because this schema has no table that replaces them.
            "retained_writable": sorted(retained),
        },
    )
    marker.chmod(0o440)
    return marker


def verify_retirement(data_dir: Path) -> dict[str, Any]:
    """Re-check that no store PostgreSQL owns became writable again.

    Retirement is a standing property, not a one-time event: a legacy deployment
    that was never stopped re-creates `execution_queue.json` on its next write
    and starts serving a queue PostgreSQL knows nothing about. Mode bits alone
    cannot prevent that, so this is how the guarantee is evidenced afterwards.
    """
    marker_path = data_dir / RETIREMENT_MARKER
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MigrationRefused(f"legacy authority was never retired: {exc}") from exc
    if marker.get("format") != 1 or not isinstance(marker.get("retired"), list):
        raise MigrationRefused("unsupported retirement marker format")
    snapshot = load_snapshot(Path(marker["snapshot"]))

    problems: list[str] = []
    for entry in marker["retired"]:
        name = entry.get("file")
        expected = snapshot.manifest["files"].get(name)
        if expected is None:
            problems.append(f"{name} is not in the recorded snapshot")
            continue
        if entry.get("how") == "renamed":
            if (data_dir / name).exists():
                problems.append(f"{name} was re-created after cutover")
            path = data_dir / entry.get("rollback_path", name + RETIRED_SUFFIX)
        else:
            path = data_dir / name
        if not path.is_file():
            problems.append(f"{name} is gone from its rollback path")
        elif path.stat().st_mode & 0o222:
            problems.append(f"{name} is writable again")
        elif name != "runtime.db" and _sha256(path) != expected:
            problems.append(f"{name} no longer matches the snapshot")
    if problems:
        raise MigrationRefused("; ".join(problems))
    return {
        "retired": marker["retired"],
        "retained_writable": marker.get("retained_writable", []),
        "snapshot": marker["snapshot"],
    }
