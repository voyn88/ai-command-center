"""VOYN-W0-AICC-RETENTION-UNBOUNDED-DELETE: `scripts/prune_debug_runs.py` is
the third path that deleted run history without bounding one statement's worth
of work.

It is operator-run rather than scheduled, but it is aimed at exactly the
database the ticket describes — one nobody has cleaned in a long time — and it
named every doomed run in a single `IN (...)` list, once per child table, all
inside one transaction. Two consequences: the write lock is held for the whole
backlog, and the statement blows past SQLite's `SQLITE_LIMIT_VARIABLE_NUMBER`
(999 on builds before 3.32) and fails outright.

These tests pin the per-statement bound, not just "it loops".
"""

from __future__ import annotations

import contextlib
import importlib.util
from pathlib import Path

import pytest

from command_center.runtime import db

ROOT = Path(__file__).resolve().parent.parent

DEBUG_RUNS = 1500
BATCH_SIZE = 500

#: The smallest `SQLITE_LIMIT_VARIABLE_NUMBER` a supported build may impose.
#: The default batch size has to stay under it or `--apply` cannot run at all
#: on such a build.
SQLITE_LEGACY_VARIABLE_LIMIT = 999


def _load_script():
    """Import the script by path — `scripts/` is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        "prune_debug_runs", ROOT / "scripts" / "prune_debug_runs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def pdr():
    return _load_script()


_INSERT_RUN = """
    INSERT INTO run (id, session_id, task_id, sequence, is_resume, state, project,
                     task_type, repository_path, prompt, created_at, updated_at,
                     completed_at, failure_reason)
    VALUES (?, ?, ?, ?, 0, ?, 'AIOS', 'implementation', '/tmp/repo', 'p',
            '2025-01-01', '2025-01-01', '2025-01-01', ?)
"""


def _seed(db_path: Path, *, debug_runs: int, classified_runs: int = 3) -> None:
    """`debug_runs` unclassified INTERRUPTED runs (doomed, each with an event)
    plus `classified_runs` FAILED runs that carry a reason (must survive)."""
    db.migrate(db_path)
    task = db.create_task(
        db_path, project="AIOS", title="t", task_type="implementation"
    )
    session = db.create_session(
        db_path, task_id=task["id"], project="AIOS", repository_path="/tmp/repo"
    )
    with db.connect(db_path) as conn, db.transaction(conn):
        for i in range(debug_runs):
            conn.execute(
                _INSERT_RUN,
                (f"dbg-{i}", session["id"], task["id"], i, "INTERRUPTED", None),
            )
            conn.execute(
                "INSERT INTO run_event (run_id, seq, event_type, payload_json, created_at)"
                " VALUES (?, 0, 'stream_event', '{}', '2025-01-01')",
                (f"dbg-{i}",),
            )
        for i in range(classified_runs):
            conn.execute(
                _INSERT_RUN,
                (f"keep-{i}", session["id"], task["id"], 10_000 + i, "FAILED", "timeout"),
            )


def _counts(db_path: Path) -> tuple[int, int]:
    with db.connect(db_path) as conn:
        runs = conn.execute("SELECT COUNT(*) AS c FROM run").fetchone()["c"]
        events = conn.execute("SELECT COUNT(*) AS c FROM run_event").fetchone()["c"]
    return int(runs), int(events)


class _RecordingConnection:
    def __init__(self, conn, statements: list[dict]):
        self._conn = conn
        self._statements = statements

    def execute(self, sql, parameters=(), /):
        self._statements.append(
            {"sql": " ".join(sql.split()), "params": len(tuple(parameters))}
        )
        return self._conn.execute(sql, parameters)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _record_statements(monkeypatch, module) -> list[dict]:
    statements: list[dict] = []
    original = module.db.connect

    @contextlib.contextmanager
    def _recording_connect(db_path):
        with original(db_path) as conn:
            yield _RecordingConnection(conn, statements)

    monkeypatch.setattr(module.db, "connect", _recording_connect)
    return statements


def test_prune_bounds_every_statement_and_commits_per_batch(
    tmp_path, monkeypatch, pdr
):
    """The acceptance criterion, pinned per statement: no `DELETE` may name
    more runs than one batch, however long the backlog."""
    db_path = tmp_path / "runtime.db"
    _seed(db_path, debug_runs=DEBUG_RUNS)

    doomed = [run["id"] for run in pdr.debug_artifacts(db_path)]
    assert len(doomed) == DEBUG_RUNS

    statements = _record_statements(monkeypatch, pdr)
    pdr.prune(db_path, doomed, batch_size=BATCH_SIZE)

    deletes = [s for s in statements if s["sql"].startswith("DELETE")]
    assert deletes
    for statement in deletes:
        assert statement["params"] <= BATCH_SIZE, statement

    # 1500 runs in batches of 500 is three transactions, not one sweep.
    begins = [s for s in statements if s["sql"].startswith("BEGIN")]
    assert len(begins) == 3

    runs_left, events_left = _counts(db_path)
    assert runs_left == 3  # the classified failures are history, not residue
    assert events_left == 0  # children go with their parents, never orphaned


def test_default_batch_size_fits_the_legacy_sqlite_variable_limit(tmp_path, pdr):
    """A default that exceeded the 999-variable floor would make `--apply`
    unrunnable on older SQLite builds rather than merely slow."""
    db_path = tmp_path / "runtime.db"
    _seed(db_path, debug_runs=1200)

    assert pdr.PRUNE_BATCH_SIZE <= SQLITE_LEGACY_VARIABLE_LIMIT

    doomed = [run["id"] for run in pdr.debug_artifacts(db_path)]
    pdr.prune(db_path, doomed)  # default batch size

    assert _counts(db_path) == (3, 0)


def test_failed_batch_rolls_back_only_itself(tmp_path, monkeypatch, pdr):
    """Batching gave up all-or-nothing. Prove the trade is the one documented:
    the failing batch is undone, earlier batches stay committed."""
    db_path = tmp_path / "runtime.db"
    _seed(db_path, debug_runs=1000)
    doomed = [run["id"] for run in pdr.debug_artifacts(db_path)]

    original = pdr.db.connect
    run_deletes = [0]

    @contextlib.contextmanager
    def _failing_connect(path):
        with original(path) as conn:

            class _Failing(_RecordingConnection):
                def execute(self, sql, parameters=(), /):
                    if sql.strip().upper().startswith("DELETE FROM RUN "):
                        run_deletes[0] += 1
                        if run_deletes[0] == 2:
                            raise RuntimeError("disk full")
                    return conn.execute(sql, parameters)

            yield _Failing(conn, [])

    monkeypatch.setattr(pdr.db, "connect", _failing_connect)

    with pytest.raises(RuntimeError, match="disk full"):
        pdr.prune(db_path, doomed, batch_size=BATCH_SIZE)

    monkeypatch.undo()
    # First batch committed; the second rolled back with its children intact.
    remaining = pdr.debug_artifacts(db_path)
    assert len(remaining) == 1000 - BATCH_SIZE
    _, events_left = _counts(db_path)
    assert events_left == len(remaining), "a rolled-back batch must keep its events"


def test_absent_child_table_is_resolved_once_not_swallowed_per_delete(
    tmp_path, monkeypatch, pdr
):
    """The old code caught *any* exception per child table and called it "table
    missing on an older schema". Batching makes that dangerous: a real DELETE
    failure swallowed there would let run rows go while their children stayed.
    Tables are resolved from the schema up front instead."""
    db_path = tmp_path / "runtime.db"
    _seed(db_path, debug_runs=5)

    with db.connect(db_path) as conn:
        tables = pdr._existing_child_tables(conn)
    assert "run_event" in tables
    assert "validation_result" not in tables  # not in this schema

    statements = _record_statements(monkeypatch, pdr)
    pdr.prune(db_path, [run["id"] for run in pdr.debug_artifacts(db_path)])

    assert not [s for s in statements if "validation_result" in s["sql"]]
    assert _counts(db_path) == (3, 0)


def test_prune_rejects_a_non_positive_batch_size(tmp_path, pdr):
    db_path = tmp_path / "runtime.db"
    _seed(db_path, debug_runs=5)
    for batch_size in (0, -1):
        with pytest.raises(ValueError):
            pdr.prune(db_path, ["dbg-0"], batch_size=batch_size)


def test_prune_of_nothing_touches_nothing(tmp_path, pdr):
    db_path = tmp_path / "runtime.db"
    _seed(db_path, debug_runs=4)
    before = _counts(db_path)
    pdr.prune(db_path, [])
    assert _counts(db_path) == before
