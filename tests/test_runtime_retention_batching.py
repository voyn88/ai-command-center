"""VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE: retention deletes must run in
fixed-size batches, not one unbounded `DELETE` (and `archive_and_prune` must
stream rows to the archive instead of `fetchall()`-ing the whole doomed set).

These tests seed enough old events that a single fixed batch size cannot
cover them in one pass, then prove: (a) every doomed row is still archived
and/or deleted, and (b) the work actually happened across more than one
batch/transaction rather than a single unbounded sweep.
"""

from __future__ import annotations

import contextlib
import gzip
import json
import sqlite3
import zlib
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


def _recoverable_archive_lines(archive_dir: Path) -> int:
    """Lines readable from the (possibly trailer-less) archive on disk.

    A crash leaves the gzip stream unterminated, so `gzip.open` would raise at
    EOF; decompressing incrementally recovers everything flushed so far, which
    is exactly what an operator would have left to restore from.
    """
    archives = sorted(archive_dir.glob("run-events-*.jsonl.gz"))
    if not archives:
        return 0
    raw = archives[-1].read_bytes()
    data = zlib.decompressobj(zlib.MAX_WBITS | 16).decompress(raw)
    return data.decode("utf-8").count("\n")


def test_archive_and_prune_archive_is_on_disk_before_each_batch_commits(
    tmp_path, monkeypatch
):
    """Batching split one all-or-nothing transaction into many, so the module's
    "archive and deletion can never disagree" invariant has to hold at *every*
    commit point — not just at the end. A hard kill between batches must never
    leave rows deleted that the archive on disk does not already hold.
    """
    db_path = tmp_path / "runtime.db"
    old_run, _fresh_run = _seed(db_path, old_events=OLD_EVENTS, fresh_events=3)
    archive_dir = tmp_path / "cold"

    original = maintenance.transaction
    observed: list[tuple[int, int]] = []

    @contextlib.contextmanager
    def _observing_transaction(conn):
        with original(conn) as opened:
            yield opened
        # The batch is committed now; whatever it deleted must already be
        # durable in the archive file.
        observed.append(
            (
                OLD_EVENTS - _event_count(db_path, old_run),
                _recoverable_archive_lines(archive_dir),
            )
        )

    monkeypatch.setattr(maintenance, "transaction", _observing_transaction)

    maintenance.archive_and_prune(
        db_path,
        retention_days=30,
        archive_dir=archive_dir,
        batch_size=BATCH_SIZE,
    )

    assert observed  # the wrapper actually ran
    for deleted, archived_on_disk in observed:
        assert archived_on_disk >= deleted, (deleted, archived_on_disk, observed)
    assert observed[-1] == (OLD_EVENTS, OLD_EVENTS)


def test_integrity_failure_names_what_was_already_pruned_and_the_backup(
    tmp_path, monkeypatch
):
    """Batching traded the all-or-nothing transaction for bounded ones, so a
    late failure can no longer claim the database was left unmodified. The
    error has to say what is already gone and how to get it back.
    """
    db_path = tmp_path / "runtime.db"
    old_run, _fresh_run = _seed(db_path, old_events=OLD_EVENTS, fresh_events=3)
    archive_dir = tmp_path / "cold"

    monkeypatch.setattr(maintenance, "_integrity_ok", lambda conn: False)

    with pytest.raises(maintenance.MaintenanceError) as excinfo:
        maintenance.archive_and_prune(
            db_path,
            retention_days=30,
            archive_dir=archive_dir,
            batch_size=BATCH_SIZE,
        )

    backup = sorted(archive_dir.glob("runtime-backup-*.db"))[-1]
    message = str(excinfo.value)
    assert str(OLD_EVENTS) in message  # rows already committed as pruned
    assert str(backup) in message  # ...and the way back
    assert _event_count(db_path, old_run) == 0  # they really are gone
    monkeypatch.undo()  # the forced failure was the prune's check, not the backup's
    maintenance.restore_backup(backup, db_path)
    assert _event_count(db_path, old_run) == OLD_EVENTS  # the way back works


class _ShortRowcount:
    """A delete result that under-reports how many rows it removed."""

    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _ShortDeleteConnection:
    """Connection proxy whose `nth` DELETE reports one row fewer than it
    deleted — the archive/delete disagreement `archive_and_prune` refuses to
    accept, injected at a batch boundary that is not the first."""

    def __init__(self, conn, *, fail_on: int) -> None:
        self._conn = conn
        self._fail_on = fail_on
        self._deletes = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, parameters=()):
        cursor = self._conn.execute(sql, parameters)
        if sql.lstrip().upper().startswith("DELETE"):
            self._deletes += 1
            if self._deletes == self._fail_on:
                return _ShortRowcount(cursor.rowcount - 1)
        return cursor


def test_batch_mismatch_rolls_back_only_its_own_batch(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    old_run, _fresh_run = _seed(db_path, old_events=OLD_EVENTS, fresh_events=3)
    archive_dir = tmp_path / "cold"
    original_connect = maintenance.connect

    @contextlib.contextmanager
    def _short_delete_connect(path):
        with original_connect(path) as conn:
            yield _ShortDeleteConnection(conn, fail_on=2)

    monkeypatch.setattr(maintenance, "connect", _short_delete_connect)

    with pytest.raises(maintenance.MaintenanceError) as excinfo:
        maintenance.archive_and_prune(
            db_path,
            retention_days=30,
            archive_dir=archive_dir,
            batch_size=BATCH_SIZE,
        )

    message = str(excinfo.value)
    assert f"{BATCH_SIZE} rows pruned by earlier batches" in message
    assert str(sorted(archive_dir.glob("runtime-backup-*.db"))[-1]) in message
    # The failing batch is intact; only the first, already-committed one is gone.
    assert _event_count(db_path, old_run) == OLD_EVENTS - BATCH_SIZE


def test_apply_runtime_retention_rejects_non_positive_batch_size(tmp_path):
    """A non-positive `LIMIT` is unbounded in SQLite, so accepting one would
    silently restore the single-sweep DELETE this function exists to prevent."""
    db_path = tmp_path / "runtime.db"
    _seed(db_path, old_events=1, fresh_events=0)

    for bad in (0, -1):
        with pytest.raises(ValueError, match="batch_size"):
            db.apply_runtime_retention(db_path, retention_days=30, batch_size=bad)


class _FailingDeleteConnection:
    """Connection proxy whose `nth` DELETE raises the way a batch that outlasts
    its busy budget would — injected past the first batch boundary."""

    def __init__(self, conn, *, fail_on: int) -> None:
        self._conn = conn
        self._fail_on = fail_on
        self._deletes = 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, parameters=()):
        if sql.lstrip().upper().startswith("DELETE"):
            self._deletes += 1
            if self._deletes == self._fail_on:
                raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, parameters)


def test_apply_runtime_retention_failure_keeps_committed_batches_and_resumes(
    tmp_path, monkeypatch
):
    """Batching traded the all-or-nothing sweep for bounded ones, so a raise
    part-way through is not a no-op: the batches that already committed stay
    committed (and the count goes with the exception), and the next call
    resumes from what is left rather than starting over."""
    db_path = tmp_path / "runtime.db"
    old_run, fresh_run = _seed(db_path, old_events=OLD_EVENTS, fresh_events=3)
    original_connect = db.connect

    @contextlib.contextmanager
    def _failing_connect(path):
        with original_connect(path) as conn:
            yield _FailingDeleteConnection(conn, fail_on=3)

    monkeypatch.setattr(db, "connect", _failing_connect)

    with pytest.raises(sqlite3.OperationalError):
        db.apply_runtime_retention(db_path, retention_days=30, batch_size=BATCH_SIZE)

    monkeypatch.undo()
    # Two batches committed before the third raised; the third rolled back.
    assert _event_count(db_path, old_run) == OLD_EVENTS - 2 * BATCH_SIZE
    assert _event_count(db_path, fresh_run) == 3

    # The next call finishes the remainder instead of redoing the whole sweep.
    removed = db.apply_runtime_retention(
        db_path, retention_days=30, batch_size=BATCH_SIZE
    )
    assert removed == OLD_EVENTS - 2 * BATCH_SIZE
    assert _event_count(db_path, old_run) == 0
    assert _event_count(db_path, fresh_run) == 3


class _RecordingCursor:
    """Wraps a cursor to count the rows actually handed back to the caller."""

    def __init__(self, cursor, record: dict):
        self._cursor = cursor
        self._record = record

    def fetchall(self):
        rows = self._cursor.fetchall()
        self._record["rows"] += len(rows)
        return rows

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is not None:
            self._record["rows"] += 1
        return row

    def __iter__(self):
        for row in self._cursor:
            self._record["rows"] += 1
            yield row

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class _RecordingConnection:
    """Records every statement issued, with how many rows it deleted (DML) or
    handed back (reads)."""

    def __init__(self, conn, statements: list[dict]):
        self._conn = conn
        self._statements = statements

    def execute(self, sql, parameters=(), /):
        cursor = self._conn.execute(sql, parameters)
        record = {
            "sql": " ".join(sql.split()),
            "params": len(parameters),
            "rows": 0,
            # Valid immediately after a DML statement; meaningless (-1) for reads.
            "deleted": cursor.rowcount,
        }
        self._statements.append(record)
        return _RecordingCursor(cursor, record)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _record_statements(
    monkeypatch, module, probe_target: str = "connect"
) -> list[dict]:
    """Record every statement the sweep issues through `module`'s `connect`."""
    statements: list[dict] = []
    original = getattr(module, probe_target)

    @contextlib.contextmanager
    def _recording_connect(db_path):
        with original(db_path) as conn:
            yield _RecordingConnection(conn, statements)

    monkeypatch.setattr(module, probe_target, _recording_connect)
    return statements


def _run_event_statements(statements: list[dict]) -> tuple[list[dict], list[dict]]:
    """The `run_event` deletes and the `run_event` reads, in issue order."""
    touching = [s for s in statements if "run_event" in s["sql"]]
    deletes = [s for s in touching if s["sql"].startswith("DELETE")]
    reads = [s for s in touching if s["sql"].startswith("SELECT")]
    return deletes, reads


def test_apply_runtime_retention_never_issues_an_unbounded_statement(
    tmp_path, monkeypatch
):
    """The acceptance criterion, pinned per *statement* rather than per
    transaction: no single `DELETE` may remove more than `batch_size` rows, and
    no single read may hand back more than `batch_size` rows.

    Counting transactions proves the sweep committed more than once, but it
    cannot catch a regression that keeps the loop and widens the statement
    inside it — restoring the predicate `DELETE` (`WHERE run_id IN (SELECT
    ...)`) would bind only a handful of parameters and still delete the entire
    backlog in one statement. Row counts are what actually bound the work.
    """
    db_path = tmp_path / "runtime.db"
    old_run, fresh_run = _seed(db_path, old_events=OLD_EVENTS, fresh_events=3)

    statements = _record_statements(monkeypatch, db)

    removed = db.apply_runtime_retention(
        db_path, retention_days=30, batch_size=BATCH_SIZE
    )

    assert removed == OLD_EVENTS
    assert _event_count(db_path, old_run) == 0
    assert _event_count(db_path, fresh_run) == 3

    deletes, reads = _run_event_statements(statements)
    # 25 rows in batches of 10 cannot be one statement's worth of work.
    assert len(deletes) == 3
    assert [s["deleted"] for s in deletes] == [BATCH_SIZE, BATCH_SIZE, 5]
    assert sum(s["deleted"] for s in deletes) == OLD_EVENTS
    for statement in deletes:
        assert statement["deleted"] <= BATCH_SIZE, statement
        # Deleting by explicit id keeps each batch's delete count equal to what
        # was just read; a predicate delete would bind no ids at all.
        assert statement["params"] == statement["deleted"], statement
    assert reads  # the doomed set is read, never assumed
    for statement in reads:
        assert statement["rows"] <= BATCH_SIZE, statement


def test_archive_and_prune_never_reads_the_whole_doomed_set_into_memory(
    tmp_path, monkeypatch
):
    """`archive_and_prune`'s extra sin in the ticket was `fetchall()`-ing every
    doomed row before writing the archive. Each read must stay within one batch
    — and every archived row must still reach the archive.
    """
    db_path = tmp_path / "runtime.db"
    old_run, fresh_run = _seed(db_path, old_events=OLD_EVENTS, fresh_events=3)

    statements = _record_statements(monkeypatch, maintenance)

    report = maintenance.archive_and_prune(
        db_path,
        retention_days=30,
        archive_dir=tmp_path / "cold",
        batch_size=BATCH_SIZE,
    )

    assert report["archived_events"] == report["pruned_events"] == OLD_EVENTS
    assert _event_count(db_path, old_run) == 0
    assert _event_count(db_path, fresh_run) == 3

    deletes, reads = _run_event_statements(statements)
    assert len(deletes) == 3
    assert [s["deleted"] for s in deletes] == [BATCH_SIZE, BATCH_SIZE, 5]
    for statement in deletes:
        assert statement["deleted"] <= BATCH_SIZE, statement
        assert statement["params"] == statement["deleted"], statement
    assert reads
    for statement in reads:
        # The full-row reads that feed the archive are the ones the ticket
        # named; none of them may span the whole backlog.
        assert statement["rows"] <= BATCH_SIZE, statement

    # Bounding the reads must not cost the archive any rows.
    with gzip.open(report["archive_path"], "rt", encoding="utf-8") as handle:
        assert sum(1 for _ in handle) == OLD_EVENTS
