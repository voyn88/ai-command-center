"""The SQLite/JSON -> PostgreSQL cutover migration (`VOYN-W0-AICC-SRV-07`).

`VOYN-W0-AICC-SRV-01B` built the shape this depends on: every one of the
33 tables in `docs/srv01b-schema-map.md` already has a `PostgresTableMirror`
(or, for `queue_entry`, a `PostgresQueueMirror`) with the conversions its
target's stricter types need. What that slice did not build is a one-time,
checkable migration of *existing* rows -- the dual-write hooks only ever see
rows written after they were wired in. This module is that migration:

1. `snapshot_sqlite_db` / `snapshot_json_file` freeze a checksummed, read-only
   copy of each legacy source, so the import runs against something that
   cannot change out from under it and whose contents can be proven later.
2. `wave_order` derives a dependency-respecting insertion order from the
   mirrors' own declared `references` -- live, not from the correspondence
   map's table, which the map's own header admits goes stale.
3. `import_snapshot` walks that order and re-uses each table's already-tested
   mirror (`upsert`, `resync_identity`) rather than a second conversion path.
   Every `upsert` is `ON CONFLICT DO UPDATE`, so running this twice against
   the same snapshot reproduces the same rows -- the idempotence the
   acceptance criteria asks for is a property of the existing mirrors, not of
   anything new here.
4. `reconcile` re-uses `mirror_support.divergence` -- the same comparison the
   dual-write hooks are proven against -- to compare the frozen snapshot to
   PostgreSQL: counts, then the primary-key set, then a column-level compare.
5. `lock_legacy_sources` / `unlock_legacy_sources` are the cutover and its
   rollback: a legacy source's own file permissions become the enforced
   "no longer a writable authority" -- a real, OS-level guarantee rather than
   an application flag a stray code path could ignore -- and it is refused
   outright unless the reconciliation handed to it reports zero divergence.
   Rollback is `unlock`; nothing here ever deletes or rewrites a legacy
   source, so the SQLite file and the JSON queue file remain exactly what
   they were, and reverting is restoring writability, not restoring data.

`queue_entry` is the one table whose PostgreSQL-recognized authority is not
the SQLite file: `command_center/db/queue_store.py` documents that
`execution_queue.json` is authoritative and the SQLite `queue_entry` table is
itself a best-effort mirror of it, written by `_mirror_to_runtime_db` and
allowed to fall behind on a failed write. Importing that table from the
SQLite snapshot would migrate a mirror's mirror; every function here that
touches it instead takes the queue's own frozen JSON snapshot.

`data/runs.jsonl` is the JSONL leg of "SQLite/JSON/JSONL", and it reaches
PostgreSQL only indirectly. It is the v1.2 synchronous run journal;
`command_center/runtime/legacy_import.py` drains it into the v2 SQLite store
one way, linking each legacy record to a `session` via `session.legacy_run_id`,
and `runtime/runs_read.list_unified_runs` still merges whatever has not been
drained into the live Runs view. So a legacy run that was never drained is in
no mirrored table, is therefore in no SQLite snapshot, and a snapshot-to-
PostgreSQL comparison cannot notice it is gone -- the reconciliation would
report clean while the cutover stranded it in a file about to be made
read-only. `undrained_legacy_runs` closes that hole by comparing the JSONL's
own ids against `session.legacy_run_id` *in PostgreSQL*, and `reconcile`
reports the result as the `legacy_runs_jsonl` row so the same `clean` flag
that gates `lock_legacy_sources` covers the JSONL leg too. Draining is the
existing, idempotent `import_legacy_runs`, run before the snapshot is taken;
nothing here rewrites `runs.jsonl` itself.

`data/activity.jsonl` is deliberately *not* in scope: it is an append-only UI
activity log with no table on the PostgreSQL side at all (no mirror declares
one), so it is not a legacy *copy* of migrated state and there is nothing to
reconcile it against. Locking it would only break `activity_log.record()`.
Giving it a home in PostgreSQL is a schema change, and so SRV-01's work, not
this migration's.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from command_center.db import mirror_registry
from command_center.db import mirror_support
from command_center.db.queue_store import QUEUE_ENTRY_COLUMNS, PostgresQueueMirror
from command_center.db.table_mirror import MirroredTable

__all__ = [
    "Snapshot",
    "snapshot_sqlite_db",
    "snapshot_json_file",
    "verify_snapshot",
    "wave_order",
    "TableImportReport",
    "MigrationReport",
    "import_snapshot",
    "TableReconciliation",
    "ReconciliationReport",
    "reconcile",
    "legacy_run_ids",
    "undrained_legacy_runs",
    "write_report",
    "read_reconciliation_report",
    "lock_legacy_sources",
    "unlock_legacy_sources",
]

_READ_ONLY = stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH
_OWNER_READ_WRITE = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stamp(taken_at: str) -> str:
    return taken_at.replace(":", "").replace("-", "").replace("+00:00", "Z")


#: SQLite siblings that hold committed data the main file does not yet.
_SQLITE_SIDECARS = ("-wal", "-shm")


def _fingerprint(path: Path) -> dict[str, dict]:
    """Size and mtime of a *live* legacy source, as of right now.

    Recorded in the reconciliation report and re-checked by
    `lock_legacy_sources`, which is the only thing standing between a
    reconciliation and the writes that can land after it: the report proves
    PostgreSQL matched the source at 10:00, and by itself says nothing about
    whether the application kept writing until the operator ran the cutover at
    10:30. Those thirty minutes of rows exist only in the file about to be made
    read-only.

    Not a SHA-256, because the point is to be cheap enough to run on every
    lock of a multi-gigabyte `runtime.db`, and because size-plus-mtime already
    detects the case that actually happens -- a live writer appending -- rather
    than a deliberately size-and-mtime-preserving forgery, which is not this
    check's threat model.

    The `-wal`/`-shm` siblings are fingerprinted with the file itself. A
    WAL-mode commit lands in `runtime.db-wal` and need not touch the main
    file's mtime at all, so a fingerprint of `runtime.db` alone would call a
    database that took writes unchanged -- exactly the reassurance this must
    not give.
    """
    parts: dict[str, dict] = {}
    for candidate in (path, *(path.with_name(path.name + s) for s in _SQLITE_SIDECARS)):
        if not candidate.exists():
            continue
        stats = candidate.stat()
        parts[candidate.name] = {"size": stats.st_size, "mtime_ns": stats.st_mtime_ns}
    return parts


# --------------------------------------------------------------------------
# Snapshot + checksum
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """A frozen, checksummed copy of one legacy source."""

    source: Path
    path: Path
    sha256: str
    taken_at: str
    #: `_fingerprint` of the *live* source as the copy finished. Carried
    #: through to the reconciliation report, so `lock_legacy_sources` compares
    #: against the moment the data being reconciled was actually read --
    #: covering the whole import-and-reconcile window, which on a large
    #: database is where nearly all the writes-after-snapshot risk lives.
    source_fingerprint: dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "source": str(self.source),
            "path": str(self.path),
            "sha256": self.sha256,
            "taken_at": self.taken_at,
            "source_fingerprint": self.source_fingerprint,
        }


def _freeze_and_checksum(dest: Path) -> str:
    """Read-only, then checksummed -- in that order, so nothing between the
    write and the checksum can still change the file."""
    dest.chmod(_READ_ONLY)
    digest = _sha256(dest)
    checksum_path = dest.with_name(dest.name + ".sha256")
    checksum_path.write_text(f"{digest}  {dest.name}\n", encoding="utf-8")
    checksum_path.chmod(_READ_ONLY)
    return digest


def snapshot_sqlite_db(source_path: Path, snapshot_dir: Path, *, name: str = "runtime") -> Snapshot:
    """Freeze a self-consistent copy of the SQLite authority.

    Uses SQLite's own online backup API rather than a byte-level file copy.
    `runtime.db` runs in WAL mode and stays open under concurrent writers (see
    `command_center.runtime.db.core.connect`), so copying the bytes of the
    main file alone can miss pages still sitting in the `-wal` sibling; the
    backup API is SQLite's own answer to "copy a database that is live right
    now," and it produces one self-contained file with no companion to track.
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    taken_at = _now_iso()
    dest = snapshot_dir / f"{name}-{_stamp(taken_at)}.sqlite3"
    source_conn = sqlite3.connect(str(source_path))
    try:
        dest_conn = sqlite3.connect(str(dest))
        try:
            source_conn.backup(dest_conn)
        finally:
            dest_conn.close()
    finally:
        source_conn.close()
    digest = _freeze_and_checksum(dest)
    return Snapshot(
        source=source_path,
        path=dest,
        sha256=digest,
        taken_at=taken_at,
        source_fingerprint=_fingerprint(source_path),
    )


def snapshot_json_file(source_path: Path, snapshot_dir: Path, *, name: str) -> Snapshot:
    """Freeze a copy of a JSON/JSONL legacy authority (the execution queue).

    A plain copy is enough here: every write to one of these files goes
    through `storage.atomic_write_json` (temp file + `os.replace`), so there
    is no torn-write window on disk the way there is for a live WAL-mode
    SQLite file.
    """
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    taken_at = _now_iso()
    dest = snapshot_dir / f"{name}-{_stamp(taken_at)}{source_path.suffix}"
    shutil.copy2(source_path, dest)
    digest = _freeze_and_checksum(dest)
    return Snapshot(
        source=source_path,
        path=dest,
        sha256=digest,
        taken_at=taken_at,
        source_fingerprint=_fingerprint(source_path),
    )


def verify_snapshot(snapshot: Snapshot) -> None:
    """Raise if a snapshot file no longer matches the checksum taken with it.

    Every read of a snapshot in this module calls this first: a snapshot is
    the thing the reconciliation report and the cutover decision are about,
    so silently reading a changed file would make both meaningless.
    """
    actual = _sha256(snapshot.path)
    if actual != snapshot.sha256:
        raise ValueError(
            f"snapshot {snapshot.path} no longer matches its checksum: "
            f"recorded {snapshot.sha256}, found {actual}. It was modified "
            "after being taken and cannot be trusted for import or reconciliation."
        )


# --------------------------------------------------------------------------
# Dependency order
# --------------------------------------------------------------------------


def _discovered_specs() -> dict[str, MirroredTable]:
    return {table: mirror.spec for table, (mirror, _module) in mirror_registry.mirror_classes().items()}


def wave_order(specs: dict[str, MirroredTable] | None = None) -> list[list[str]]:
    """Tables in dependency order, derived from `MirroredTable.references`.

    Deliberately not read from `docs/srv01b-schema-map.md`'s five-wave table:
    that document's own header says the snapshot is static and already stale,
    and `tests/db/test_schema_correspondence.py` derives this same order live
    against the real schema on every run. An importer hard-coding the
    document's wave sizes would drift from both the moment either schema next
    changes.

    Every table with no unplaced parent starts a wave; the next wave holds
    every table whose parents are now all placed, and so on. Raises if the
    reference graph has a cycle, which would mean no insertion order exists
    -- a design fact that has to surface before a backfill starts, not
    partway through one.
    """
    if specs is None:
        specs = _discovered_specs()
    dependencies = {
        table: {parent for parent in spec.references.values() if parent != table}
        for table, spec in specs.items()
    }
    waves: list[list[str]] = []
    placed: set[str] = set()
    while len(placed) < len(dependencies):
        wave = sorted(t for t, deps in dependencies.items() if t not in placed and deps <= placed)
        if not wave:
            raise ValueError(
                f"foreign-key cycle among {sorted(set(dependencies) - placed)}: "
                "no import order exists"
            )
        waves.append(wave)
        placed |= set(wave)
    return waves


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


def _read_table_rows(conn: sqlite3.Connection, spec: MirroredTable) -> list[dict]:
    columns = ", ".join(spec.columns)
    keys = ", ".join(spec.key_columns)
    cursor = conn.execute(f"SELECT {columns} FROM {spec.table} ORDER BY {keys}")
    return [dict(row) for row in cursor.fetchall()]


def _open_snapshot_readonly(snapshot: Snapshot) -> sqlite3.Connection:
    verify_snapshot(snapshot)
    conn = sqlite3.connect(f"file:{snapshot.path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class TableImportReport:
    table: str
    source_rows: int
    imported: int
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class MigrationReport:
    started_at: str
    finished_at: str
    tables: list[TableImportReport]
    identity_resyncs: dict[str, int] = field(default_factory=dict)
    queue_imported: int | None = None

    @property
    def ok(self) -> bool:
        return all(t.ok for t in self.tables)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "tables": [
                {"table": t.table, "source_rows": t.source_rows, "imported": t.imported, "error": t.error}
                for t in self.tables
            ],
            "identity_resyncs": self.identity_resyncs,
            "queue_imported": self.queue_imported,
        }


def import_snapshot(
    sqlite_snapshot: Snapshot,
    *,
    connection_factory: Callable[[], Any] | None = None,
    queue_entries: list[dict] | None = None,
) -> MigrationReport:
    """Deterministically load every mirrored table from a frozen SQLite
    snapshot into PostgreSQL, in dependency order, through each table's own
    declared mirror -- no second conversion path for `timestamptz`/`jsonb`/
    `boolean` to drift from the one the dual-write hooks already use.

    Idempotent by construction: every mirror's `upsert` is `ON CONFLICT DO
    UPDATE`, so importing the same snapshot twice reproduces the same rows
    rather than failing or duplicating -- an operator can re-run this after
    fixing a reconciliation finding without a separate reset step.

    A row that fails to convert (bad JSON, a `CHECK` violation, a missing
    parent) stops *that table's* loop and is recorded as the table's `error`
    rather than raised past this function: a live operator run migrates many
    tables in one call, and one table's precondition failure should not hide
    every other table's result. `MigrationReport.ok` is `False` whenever any
    table recorded one, and the caller must not treat a not-`ok` report as a
    green light to reconcile or cut over.

    `queue_entries` is `queue_entry`'s own case: its authority is
    `execution_queue.json`, not the SQLite snapshot (see this module's
    docstring), so pass the parsed contents of a frozen
    `snapshot_json_file` copy of that file. Omit it to skip the queue --
    useful for a partial run, never for a real cutover.
    """
    started_at = _now_iso()
    specs = _discovered_specs()
    mirrors = {table: mirror for table, (mirror, _module) in mirror_registry.mirror_classes().items()}
    waves = wave_order(specs)

    conn = _open_snapshot_readonly(sqlite_snapshot)
    reports: list[TableImportReport] = []
    try:
        for wave in waves:
            for table in wave:
                spec = specs[table]
                rows = _read_table_rows(conn, spec)
                instance = mirrors[table](connection_factory=connection_factory)
                imported = 0
                error: str | None = None
                try:
                    for row in rows:
                        instance.upsert(row)
                        imported += 1
                except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                    error = f"{type(exc).__name__}: {exc}"
                reports.append(
                    TableImportReport(table=table, source_rows=len(rows), imported=imported, error=error)
                )
    finally:
        conn.close()

    # Sequences are advanced after every table has loaded, not per-table: an
    # identity table earlier in wave order could still receive rows from a
    # later wave's retry, and resyncing early would leave the sequence behind
    # again. See `PostgresTableMirror.resync_identity`.
    identity_resyncs: dict[str, int] = {}
    for table, spec in specs.items():
        if spec.identity:
            identity_resyncs[table] = mirrors[table](connection_factory=connection_factory).resync_identity()

    queue_imported: int | None = None
    if queue_entries is not None:
        PostgresQueueMirror(connection_factory=connection_factory).replace_entries(queue_entries)
        queue_imported = len(queue_entries)

    return MigrationReport(
        started_at=started_at,
        finished_at=_now_iso(),
        tables=reports,
        identity_resyncs=identity_resyncs,
        queue_imported=queue_imported,
    )


# --------------------------------------------------------------------------
# The JSONL leg: data/runs.jsonl
# --------------------------------------------------------------------------


def legacy_run_ids(runs_snapshot: Snapshot) -> list[str]:
    """Distinct v1.2 run ids in a frozen `runs.jsonl` snapshot.

    Read exactly the way the application reads it -- `storage.read_jsonl` then
    `fold_latest_by_id` -- which is what `agent_runner.load_runs` does and
    therefore what `legacy_import` drained and what `list_unified_runs` still
    shows. `runs.jsonl` is append-only, one snapshot per write, so the raw line
    count is not the run count and a malformed line is skipped rather than
    fatal. Deriving the id set any other way would let this check disagree with
    the reader whose data it is supposed to be protecting.
    """
    from command_center import storage

    verify_snapshot(runs_snapshot)
    records = storage.read_jsonl(runs_snapshot.path)
    return sorted(storage.fold_latest_by_id(records))


def undrained_legacy_runs(
    runs_snapshot: Snapshot, *, connection_factory: Callable[[], Any] | None = None
) -> list[str]:
    """v1.2 run ids that no PostgreSQL `session` claims via `legacy_run_id`.

    Asked of PostgreSQL rather than of the SQLite snapshot on purpose: the
    question a cutover needs answered is "is this record safe in the new
    authority," and a record can be in the SQLite store yet absent from
    PostgreSQL because its table's import failed. Checking the destination
    answers the real question in one pass instead of chaining two.

    A non-empty result means `runs.jsonl` is still the only writable home of
    those runs. `import_legacy_runs` is the fix, and it is idempotent, so the
    remedy for a dirty result is simply to run it and reconcile again.
    """
    return _undrained(legacy_run_ids(runs_snapshot), connection_factory=connection_factory)


def _undrained(ids: list[str], *, connection_factory: Callable[[], Any] | None) -> list[str]:
    """`undrained_legacy_runs` once the ids are already in hand.

    Split out so `reconcile` can report the total and the outstanding set
    without reading and re-checksumming the snapshot twice -- `runs.jsonl` is
    an append-only journal that can reach tens of megabytes on an install with
    real v1.2 history, and the SHA-256 is over the whole file.
    """
    if not ids:
        return []
    session_mirror = mirror_registry.mirror_classes()["session"][0]
    drained = {
        row.get("legacy_run_id")
        for row in session_mirror(connection_factory=connection_factory).list_records()
        if row.get("legacy_run_id")
    }
    return [run_id for run_id in ids if run_id not in drained]


# --------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------


class _StaticReader:
    """Adapts an already-fetched row list to the `list_records()` protocol
    `mirror_support.divergence` expects, so the queue's own frozen JSON
    snapshot can be compared with the same function every other table uses."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def list_records(self) -> list[dict]:
        return self._rows


def _queue_divergence(authority_entries: list[dict], mirror_entries: list[dict]) -> list[dict]:
    """`queue_entry`'s own reconciliation: field-level, via the shared
    `divergence`, plus an order check `divergence` cannot express. Order is
    data for this table (see `queue_store`'s docstring) and `list_entries()`
    already returns rows in `position` order, so two entry lists with
    identical fields but a different order are still a real divergence.
    """
    diffs = mirror_support.divergence(
        authority_entries, _StaticReader(mirror_entries), QUEUE_ENTRY_COLUMNS, key="id"
    )
    authority_order = [entry.get("id") for entry in authority_entries]
    mirror_order = [entry.get("id") for entry in mirror_entries]
    if authority_order != mirror_order:
        diffs.append(
            {
                "id": "__order__",
                "fields": ["position"],
                "authority": authority_order,
                "mirror": mirror_order,
            }
        )
    return diffs


@dataclass
class TableReconciliation:
    table: str
    source_rows: int
    mirror_rows: int
    differences: list[dict]

    @property
    def clean(self) -> bool:
        return not self.differences


@dataclass
class ReconciliationReport:
    generated_at: str
    snapshot_sha256: dict[str, str]
    tables: list[TableReconciliation]
    #: `{str(live source path): _fingerprint(...)}` as of the moment each
    #: snapshot was taken -- not of report time, so the window this covers is
    #: the whole of import-and-reconcile. `lock_legacy_sources` refuses any
    #: path missing from here or whose fingerprint has since moved, so a report
    #: can only authorize the cutover of the exact files it actually read.
    sources: dict[str, dict] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return all(t.clean for t in self.tables)

    def to_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "snapshot_sha256": self.snapshot_sha256,
            "sources": self.sources,
            "clean": self.clean,
            "tables": [
                {
                    "table": t.table,
                    "source_rows": t.source_rows,
                    "mirror_rows": t.mirror_rows,
                    "clean": t.clean,
                    "differences": t.differences,
                }
                for t in self.tables
            ],
        }


def reconcile(
    sqlite_snapshot: Snapshot,
    *,
    connection_factory: Callable[[], Any] | None = None,
    queue_snapshot: Snapshot | None = None,
    queue_entries: list[dict] | None = None,
    runs_snapshot: Snapshot | None = None,
) -> ReconciliationReport:
    """Compare a frozen snapshot against PostgreSQL, table by table.

    Implements the procedure `docs/srv01b-schema-map.md` describes and
    defers to this task: row counts, the primary-key *set* (not just its
    size -- an equal count with a different set is a simultaneous loss and
    substitution `divergence` still reports as a difference), and a
    column-level compare for a table's own declared conversions. A dangling
    foreign key cannot survive an `import_snapshot` that reported `ok`
    -- PostgreSQL's own constraint would have refused the child row -- so it
    is not re-derived here as a fourth pass over the same rows.

    Re-uses `mirror_support.divergence`, the same comparison the dual-write
    hooks are already proven against, rather than a second implementation
    that could disagree with it about what "the same row" means.

    Verifies every snapshot's checksum before reading it, so a
    reconciliation report can never describe a snapshot that has since
    changed on disk.

    `runs_snapshot` adds the JSONL leg (see this module's docstring) as the
    `legacy_runs_jsonl` row: a v1.2 run that was never drained into the v2
    store is in no mirrored table and so in no SQLite snapshot, which makes it
    invisible to every other check here. Reporting it as an ordinary dirty row
    means the single `clean` flag `lock_legacy_sources` already gates on covers
    it too, rather than adding a second gate an operator could satisfy one of.
    Omit it only for a partial run, never for a real cutover.
    """
    verify_snapshot(sqlite_snapshot)
    if queue_snapshot is not None:
        verify_snapshot(queue_snapshot)
    if runs_snapshot is not None:
        verify_snapshot(runs_snapshot)

    specs = _discovered_specs()
    mirrors = {table: mirror for table, (mirror, _module) in mirror_registry.mirror_classes().items()}

    conn = _open_snapshot_readonly(sqlite_snapshot)
    tables: list[TableReconciliation] = []
    try:
        for table in sorted(specs):
            spec = specs[table]
            rows = _read_table_rows(conn, spec)
            instance = mirrors[table](connection_factory=connection_factory)
            mirror_rows = instance.list_records()
            diffs = mirror_support.divergence(rows, instance, spec.columns, spec.codec, key=spec.key_columns)
            tables.append(
                TableReconciliation(
                    table=table, source_rows=len(rows), mirror_rows=len(mirror_rows), differences=diffs
                )
            )
    finally:
        conn.close()

    if queue_entries is not None:
        mirror_rows = PostgresQueueMirror(connection_factory=connection_factory).list_entries()
        diffs = _queue_divergence(queue_entries, mirror_rows)
        tables.append(
            TableReconciliation(
                table="queue_entry",
                source_rows=len(queue_entries),
                mirror_rows=len(mirror_rows),
                differences=diffs,
            )
        )

    if runs_snapshot is not None:
        all_ids = legacy_run_ids(runs_snapshot)
        undrained = _undrained(all_ids, connection_factory=connection_factory)
        tables.append(
            TableReconciliation(
                table="legacy_runs_jsonl",
                source_rows=len(all_ids),
                mirror_rows=len(all_ids) - len(undrained),
                differences=[
                    {
                        "id": run_id,
                        "fields": ["legacy_run_id"],
                        "authority": run_id,
                        "mirror": None,
                    }
                    for run_id in undrained
                ],
            )
        )

    checksums = {"sqlite": sqlite_snapshot.sha256}
    sources = {str(sqlite_snapshot.source): sqlite_snapshot.source_fingerprint}
    if queue_snapshot is not None:
        checksums["queue_json"] = queue_snapshot.sha256
        sources[str(queue_snapshot.source)] = queue_snapshot.source_fingerprint
    if runs_snapshot is not None:
        checksums["runs_jsonl"] = runs_snapshot.sha256
        sources[str(runs_snapshot.source)] = runs_snapshot.source_fingerprint

    return ReconciliationReport(
        generated_at=_now_iso(), snapshot_sha256=checksums, tables=tables, sources=sources
    )


def write_report(report: MigrationReport | ReconciliationReport, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_reconciliation_report(path: Path) -> ReconciliationReport:
    """The inverse of `write_report` for a `ReconciliationReport`.

    `lock_legacy_sources` takes a report object, not a file, but the operator
    who ran the reconciliation and the operator who decides to cut over are
    not necessarily the same invocation of this module -- a real cutover
    reviews the written report before acting on it. This reconstructs the
    dataclass from exactly what `write_report` wrote, so that review-then-act
    split does not need its own second, hand-rolled parse of the report shape.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    tables = [
        TableReconciliation(
            table=table["table"],
            source_rows=table["source_rows"],
            mirror_rows=table["mirror_rows"],
            differences=table["differences"],
        )
        for table in payload["tables"]
    ]
    return ReconciliationReport(
        generated_at=payload["generated_at"],
        snapshot_sha256=payload["snapshot_sha256"],
        tables=tables,
        sources=payload.get("sources", {}),
    )


# --------------------------------------------------------------------------
# Cutover / rollback
# --------------------------------------------------------------------------


def lock_legacy_sources(paths: Iterable[Path], *, reconciliation: ReconciliationReport) -> list[Path]:
    """Revoke write access to every legacy source, at the filesystem level.

    This is the concrete answer to "no legacy copy remains writable
    authority": a chmod is enforced by the kernel for every process on the
    machine, including ones this migration knows nothing about, where an
    application-level flag would only stop code paths that remembered to
    check it.

    Refuses outright unless `reconciliation.clean` -- locking a source that
    reconciliation found still ahead of PostgreSQL would remove the only
    writable copy of rows PostgreSQL does not yet have. Returns only the
    paths this call actually changed, so an operator's cutover log does not
    claim to have locked a file that was already read-only.

    Refuses just as hard when the report does not *cover* a path, or covers it
    with a fingerprint that has since moved. "Clean" is a claim about specific
    files at a specific moment, and both halves of that are load-bearing: a
    clean report from another install, or from before the last half hour of
    application writes, is not evidence about the file in front of this call.
    Without the check the gate reads as protection while accepting any clean
    report at all -- see `_fingerprint`.
    """
    if not reconciliation.clean:
        raise ValueError(
            "refusing to lock legacy sources: the reconciliation report is not clean "
            "(pass a report with `clean == True`, produced after every finding is resolved)"
        )
    paths = list(paths)
    for path in paths:
        recorded = reconciliation.sources.get(str(path))
        if recorded is None:
            raise ValueError(
                f"refusing to lock {path}: the reconciliation report does not cover it "
                f"(it reconciled {sorted(reconciliation.sources) or 'nothing'}). A report "
                "that never looked at this file is not evidence that PostgreSQL has its rows."
            )
        current = _fingerprint(path)
        if current != recorded:
            raise ValueError(
                f"refusing to lock {path}: it changed since the reconciliation at "
                f"{reconciliation.generated_at} (recorded {recorded}, found {current}). "
                "Those writes are not in PostgreSQL; re-run the migration and reconcile again."
            )
    locked: list[Path] = []
    for path in paths:
        mode = path.stat().st_mode
        if mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH):
            path.chmod(_READ_ONLY)
            locked.append(path)
    return locked


def unlock_legacy_sources(paths: Iterable[Path]) -> list[Path]:
    """Reverse `lock_legacy_sources` -- the rollback path.

    Nothing in this module ever deletes or rewrites a legacy source, so
    rollback is exactly this: restore owner write access and the legacy
    store is, byte for byte, what it was before cutover.
    """
    unlocked: list[Path] = []
    for path in paths:
        path.chmod(_OWNER_READ_WRITE)
        unlocked.append(path)
    return unlocked
