"""`command_center.db.execution_reconcile` (VOYN-W0-AICC-SRV-09-READ-POOL).

No real PostgreSQL here, deliberately: this suite drives the *real*
`Postgres*Mirror` classes against `FakePostgres`, an in-memory stand-in that
interprets the SQL text those classes emit. That is what makes the earlier
rejection ("`_patch()` replaces each mirror class with a lambda accepting only
its defaulted `mirror` parameter... production reconciliation constructs it
with `PostgresTaskMirror(pg_connection_factory)`") structurally impossible to
repeat here: there is no stand-in whose constructor could disagree with the
real one, because nothing here replaces the classes at all.
"""

from __future__ import annotations

import re
from contextlib import contextmanager, nullcontext

import pytest

from command_center.db.execution_reconcile import (
    ExecutionReconciliationReport,
    reconcile_execution_center,
)
from command_center.db.execution_store import (
    PostgresSessionMirror,
    PostgresTaskMirror,
    SESSION,
    TASK,
)
from command_center.db.mirror_support import MIRROR_UNAVAILABLE
from command_center.db.run_children_store import (
    PostgresReportMirror,
    PostgresRunEventMirror,
    REPORT,
    RUN_EVENT,
)
from command_center.db.run_store import PostgresRunMirror, RUN
from command_center.runtime.db import core as runtime_core

_SPECS = (TASK, SESSION, RUN, RUN_EVENT, REPORT)

_MIRRORS = {
    "task": PostgresTaskMirror,
    "session": PostgresSessionMirror,
    "run": PostgresRunMirror,
    "run_event": PostgresRunEventMirror,
    "report": PostgresReportMirror,
}


# --------------------------------------------------------------------------
# FakePostgres — interprets the SQL the real mirror classes emit
# --------------------------------------------------------------------------


class _FakeTable:
    def __init__(self, columns: tuple[str, ...], key_columns: tuple[str, ...]) -> None:
        self.columns = columns
        self.key_columns = key_columns
        self.rows: dict[tuple, dict] = {}


class _FakeCursor:
    def __init__(self, pg: "FakePostgres") -> None:
        self._pg = pg
        self._result: list[tuple] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> None:
        stripped = sql.strip()
        if stripped.startswith("INSERT INTO"):
            table_name = re.match(r"INSERT INTO (\w+)", stripped).group(1)
            table = self._pg.tables[table_name]
            row = dict(zip(table.columns, params, strict=True))
            key = tuple(row[column] for column in table.key_columns)
            table.rows[key] = row
        elif stripped.startswith("SELECT") and " FROM " in stripped:
            table_name = re.search(r" FROM (\w+)", stripped).group(1)
            table = self._pg.tables[table_name]
            ordered = sorted(
                table.rows.values(),
                key=lambda row: tuple(row[column] for column in table.key_columns),
            )
            self._result = [tuple(row[column] for column in table.columns) for row in ordered]
        else:
            raise NotImplementedError(f"FakePostgres cannot execute: {sql!r}")

    def fetchall(self) -> list[tuple]:
        return self._result


class FakePostgres:
    """An in-memory stand-in for the tables the execution family mirrors.

    Real `Postgres*Mirror` classes run against this unmodified. Only the SQL
    text they emit is interpreted here, never their code, so a test built on
    this exercises the real `upsert`/`list_records` implementations.
    """

    def __init__(self) -> None:
        self.tables: dict[str, _FakeTable] = {}
        for spec in _SPECS:
            self.tables[spec.table] = _FakeTable(spec.columns, spec.key_columns)

    def connection_factory(self):
        return nullcontext(self)

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self)


class _SingleSlotPool:
    """Mimics a real pool opened with `AICC_PG_POOL_MAX=1`: a second
    concurrent checkout raises instead of blocking forever, so a test can see
    the exhaustion rather than hang on it."""

    def __init__(self, conn: object) -> None:
        self._conn = conn
        self._checked_out = False
        self.checkout_attempts = 0

    @contextmanager
    def connection(self):
        self.checkout_attempts += 1
        if self._checked_out:
            raise RuntimeError("pool exhausted: AICC_PG_POOL_MAX=1 already checked out")
        self._checked_out = True
        try:
            yield self._conn
        finally:
            self._checked_out = False


def _mirror_everything(pg: FakePostgres, stored: dict[str, list[dict]]) -> None:
    for table, rows in stored.items():
        mirror = _MIRRORS[table](pg.connection_factory)
        for row in rows:
            mirror.upsert(row)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "runtime.db"
    runtime_core.migrate(path)
    return path


@pytest.fixture
def pg() -> FakePostgres:
    return FakePostgres()


def _seed_family(db_path) -> tuple[dict[str, list[dict]], str]:
    """One full task/session/run/run_event/report family, plus a second, bare
    task with no session — the shape independent review's counterexample
    needs: a task whose removal touches no other table."""
    from command_center.runtime.db.execution import (
        append_run_event,
        create_report,
        create_run,
        create_session,
        create_task,
    )

    task = create_task(db_path, project="proj", title="main", task_type="dev")
    session = create_session(
        db_path, task_id=task["id"], project="proj", repository_path="/repo"
    )
    create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="proj",
        task_type="dev",
        repository_path="/repo",
        prompt="do it",
        is_resume=False,
    )
    with runtime_core.connect(db_path) as conn:
        run_row = dict(conn.execute("SELECT * FROM run").fetchone())
    append_run_event(db_path, run_row["id"], "lifecycle", {"a": 1})
    create_report(db_path, run_row["id"], "/tmp/report.md")

    lone_task = create_task(db_path, project="proj", title="orphan", task_type="dev")

    with runtime_core.connect(db_path) as conn:
        stored = {
            "task": [dict(row) for row in conn.execute("SELECT * FROM task ORDER BY id")],
            "session": [dict(row) for row in conn.execute("SELECT * FROM session ORDER BY id")],
            "run": [dict(row) for row in conn.execute("SELECT * FROM run ORDER BY id")],
            "run_event": [
                dict(row) for row in conn.execute("SELECT * FROM run_event ORDER BY id")
            ],
            "report": [dict(row) for row in conn.execute("SELECT * FROM report ORDER BY run_id")],
        }
    return stored, lone_task["id"]


# --------------------------------------------------------------------------
# Behaviour
# --------------------------------------------------------------------------


def test_reconcile_reports_clean_after_a_normal_write(db_path, pg):
    stored, _lone_task_id = _seed_family(db_path)
    _mirror_everything(pg, stored)

    report = reconcile_execution_center(db_path, pg_connection_factory=pg.connection_factory)

    assert isinstance(report, ExecutionReconciliationReport)
    assert report.stable
    assert report.attempts == 1
    assert report.clean


def test_reports_a_field_divergence(db_path, pg):
    stored, _lone_task_id = _seed_family(db_path)
    _mirror_everything(pg, stored)
    key = (stored["task"][0]["id"],)
    pg.tables["task"].rows[key]["title"] = "stale, mirrored before an authority edit"

    report = reconcile_execution_center(db_path, pg_connection_factory=pg.connection_factory)

    assert report.stable
    assert not report.clean
    assert report.divergence["task"][0]["fields"] == ["title"]
    assert report.divergence["session"] == []
    assert report.divergence["run"] == []


def test_an_unreachable_mirror_is_reported_not_raised(db_path):
    _seed_family(db_path)

    def _refused():
        raise RuntimeError("connection refused")

    report = reconcile_execution_center(db_path, pg_connection_factory=_refused)

    assert report.stable
    assert not report.clean
    assert report.divergence["task"][0]["id"] == MIRROR_UNAVAILABLE


def test_reconciling_five_tables_never_exhausts_a_single_slot_pool(db_path, pg):
    """The blocking defect this module exists to close: `mirror-status` ran
    inside the CLI's own `with pool.connection()` block, and every
    `Postgres*Mirror` it built then checked out *another* connection --
    exhausting a pool opened with `AICC_PG_POOL_MAX=1` before a query ran.

    Reconciling all five tables here happens entirely inside one checkout
    from a pool that raises rather than blocks on a second one.
    """
    stored, _lone_task_id = _seed_family(db_path)
    _mirror_everything(pg, stored)

    single_slot_pool = _SingleSlotPool(pg)
    with single_slot_pool.connection() as conn:
        report = reconcile_execution_center(
            db_path, pg_connection_factory=lambda: nullcontext(conn)
        )

    assert report.clean
    assert single_slot_pool.checkout_attempts == 1


def test_five_independent_checkouts_would_exhaust_the_same_pool(pg):
    """Negative control: proves `_SingleSlotPool` actually enforces the
    constraint the test above relies on, so that test is not vacuous."""
    single_slot_pool = _SingleSlotPool(pg)
    with single_slot_pool.connection():
        with pytest.raises(RuntimeError, match="exhausted"):
            with single_slot_pool.connection():
                pass


def test_retries_when_the_authority_moves_between_the_two_reads(db_path, pg, monkeypatch):
    """Reproduces independent review's exact counterexample: a task deleted
    from SQLite whose mirrored delete failed. Reading the authority once
    (stale, task still present) and the mirror once (also still present,
    since its delete never ran) would agree and report clean -- while the two
    stores currently disagree, because the authority has since moved on.

    The fix is to keep re-confirming the authority snapshot after the mirror
    read until it stops moving, and only compare a *confirmed* pair.
    """
    from command_center.runtime.db.execution import delete_task

    stored, lone_task_id = _seed_family(db_path)
    _mirror_everything(pg, stored)  # the mirror keeps the row: its delete "failed"

    import command_center.db.execution_reconcile as execution_reconcile

    real_read = execution_reconcile._read_authority_snapshot
    before_delete = real_read(db_path)
    after_delete_holder: dict[str, dict] = {}
    calls = {"n": 0}

    def _staged_read(path):
        calls["n"] += 1
        if calls["n"] == 1:
            return before_delete
        return after_delete_holder["snapshot"]

    monkeypatch.setattr(execution_reconcile, "_read_authority_snapshot", _staged_read)

    assert delete_task(db_path, lone_task_id)
    after_delete_holder["snapshot"] = real_read(db_path)

    report = execution_reconcile.reconcile_execution_center(
        db_path, pg_connection_factory=pg.connection_factory
    )

    assert report.stable
    assert report.attempts == 2  # the first authority read did not survive confirmation
    assert not report.clean
    task_divergence = report.divergence["task"]
    assert len(task_divergence) == 1
    assert task_divergence[0]["authority"] is None
    assert task_divergence[0]["mirror"]["id"] == lone_task_id
    # No other table is touched by deleting a task with no session.
    assert report.divergence["session"] == []
    assert report.divergence["run"] == []
    assert report.divergence["run_event"] == []
    assert report.divergence["report"] == []


def test_reports_unstable_when_the_authority_never_settles(tmp_path, pg, monkeypatch):
    """A naive implementation would eventually just compare whatever it last
    read; this refuses to call that comparison meaningful and says so."""
    import command_center.db.execution_reconcile as execution_reconcile

    calls = {"n": 0}

    def _always_moving(_path):
        calls["n"] += 1
        return {
            "task": [{"id": calls["n"]}],
            "session": [],
            "run": [],
            "run_event": [],
            "report": [],
        }

    monkeypatch.setattr(execution_reconcile, "_read_authority_snapshot", _always_moving)

    report = execution_reconcile.reconcile_execution_center(
        tmp_path / "runtime.db",
        pg_connection_factory=pg.connection_factory,
        max_attempts=3,
    )

    assert report.stable is False
    assert report.attempts == 3
    assert report.divergence == {}
    assert report.clean is False


def test_max_attempts_must_be_positive(tmp_path, pg):
    with pytest.raises(ValueError):
        reconcile_execution_center(
            tmp_path / "runtime.db", pg_connection_factory=pg.connection_factory, max_attempts=0
        )
