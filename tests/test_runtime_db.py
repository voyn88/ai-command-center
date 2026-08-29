import itertools
import multiprocessing
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from command_center.runtime import db


def _fresh_db(tmp_path):
    path = tmp_path / "runtime.db"
    db.migrate(path)
    return path


# Top-level (picklable) worker functions for the real multi-*process* tests
# below — a separate OS process, not just a separate thread in this same
# interpreter, each opening its own sqlite3 connection to the same file.


def _mp_migrate_worker(path_str: str) -> None:
    from command_center.runtime import db as _db

    _db.migrate(Path(path_str))


def _mp_barrier_migrate_worker(path_str: str, barrier, result_queue) -> None:
    """Like `_mp_migrate_worker`, but synchronizes every worker on a shared
    `multiprocessing.Barrier` before calling `migrate()`, so all processes
    call `sqlite3.connect()`/`PRAGMA journal_mode=WAL` against the
    not-yet-existent db file at, as close as an OS scheduler allows,
    literally the same instant — a genuine race, not merely "started close
    together". Reports outcome via `result_queue` rather than relying only
    on exitcode, so the test can see *what* failed, not just *that* it did."""
    from command_center.runtime import db as _db

    barrier.wait()
    try:
        _db.migrate(Path(path_str))
        result_queue.put(("ok", None))
    except BaseException as exc:  # noqa: BLE001 - must surface every failure mode to the test, not just crash silently
        code = getattr(exc, "sqlite_errorcode", None)
        name = getattr(exc, "sqlite_errorname", None)
        result_queue.put(("FAIL", f"{exc!r} code={code} name={name}"))


def _mp_event_writer_worker(path_str: str, run_id: str, process_idx: int, n_events: int) -> None:
    from command_center.runtime import db as _db

    path = Path(path_str)
    for i in range(n_events):
        _db.append_run_event(path, run_id, "lifecycle", {"process_idx": process_idx, "i": i})


def _v24_db_with_run(tmp_path, monkeypatch, *, state: str, finalized: bool):
    """Build the exact pre-claim cutover shape with one controlled run."""
    path = tmp_path / f"runtime-v24-{state.lower()}-{int(finalized)}.db"
    current_migrations = list(db.MIGRATIONS)
    with monkeypatch.context() as pre_claim:
        pre_claim.setattr(db, "MIGRATIONS", current_migrations[:-1])
        pre_claim.setattr(db, "SCHEMA_VERSION", 24)
        db.migrate(path)
        task = db.create_task(
            path,
            project="AIOS",
            title=f"v24 {state}",
            task_type="implementation",
        )
        session = db.create_session(
            path,
            task_id=task["id"],
            project="AIOS",
            repository_path="/tmp/v24-cutover",
        )
        run = db.create_run(
            path,
            session_id=session["id"],
            task_id=task["id"],
            project="AIOS",
            repository_path="/tmp/v24-cutover",
            task_type="implementation",
            prompt="v24 cutover",
            is_resume=False,
        )
        with db.connect(path) as conn:
            with db.transaction(conn):
                conn.execute(
                    "UPDATE run SET state = ?, finalized_at = ? WHERE id = ?",
                    (state, db.iso_now() if finalized else None, run["id"]),
                )
    return path, run["id"]


# --------------------------------------------------------------------------
# Migrations / schema versioning
# --------------------------------------------------------------------------


def test_migrate_creates_schema_version_row(tmp_path):
    path = _fresh_db(tmp_path)
    assert db.current_schema_version(path) == db.SCHEMA_VERSION


def test_migrate_is_idempotent(tmp_path):
    path = tmp_path / "runtime.db"
    db.migrate(path)
    db.migrate(path)
    db.migrate(path)
    assert db.current_schema_version(path) == db.SCHEMA_VERSION


@pytest.mark.parametrize(
    "state",
    sorted(db.EXECUTION_CENTER_ACTIVE_STATES),
)
@pytest.mark.parametrize("finalized", [False, True])
def test_v25_migration_refuses_every_active_v24_run(
    tmp_path, monkeypatch, state, finalized
):
    path, _run_id = _v24_db_with_run(
        tmp_path, monkeypatch, state=state, finalized=finalized
    )

    with pytest.raises(RuntimeError, match="offline zero-active"):
        db.migrate(path)

    assert db.current_schema_version(path) == 24
    with db.connect(path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'run_finalization_claim'"
        ).fetchone()
    assert table is None


@pytest.mark.parametrize("finalized", [False, True])
def test_v25_migration_refuses_unknown_or_future_state(
    tmp_path, monkeypatch, finalized
):
    path, _run_id = _v24_db_with_run(
        tmp_path,
        monkeypatch,
        state="FUTURE_ACTIVE_STATE",
        finalized=finalized,
    )

    with pytest.raises(RuntimeError, match="offline zero-active"):
        db.migrate(path)

    assert db.current_schema_version(path) == 24


def test_v25_migration_rolls_back_table_when_ledger_stamp_fails(
    tmp_path, monkeypatch
):
    path = tmp_path / "runtime-v24-ledger-failure.db"
    current_migrations = list(db.MIGRATIONS)
    with monkeypatch.context() as pre_claim:
        pre_claim.setattr(db, "MIGRATIONS", current_migrations[:-1])
        pre_claim.setattr(db, "SCHEMA_VERSION", 24)
        db.migrate(path)

    with monkeypatch.context() as failed_stamp:
        failed_stamp.setattr(
            db,
            "iso_now",
            lambda: (_ for _ in ()).throw(RuntimeError("stamp failed")),
        )
        with pytest.raises(RuntimeError, match="stamp failed"):
            db.migrate(path)

    assert db.current_schema_version(path) == 24
    with db.connect(path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'run_finalization_claim'"
        ).fetchone()
    assert table is None


def test_v25_migration_rejects_unversioned_preexisting_claim_table(
    tmp_path, monkeypatch
):
    path = tmp_path / "runtime-v24-drifted-claim.db"
    current_migrations = list(db.MIGRATIONS)
    with monkeypatch.context() as pre_claim:
        pre_claim.setattr(db, "MIGRATIONS", current_migrations[:-1])
        pre_claim.setattr(db, "SCHEMA_VERSION", 24)
        db.migrate(path)
    with db.connect(path) as conn:
        conn.execute("CREATE TABLE run_finalization_claim (run_id TEXT PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="unversioned finalization claim"):
        db.migrate(path)

    assert db.current_schema_version(path) == 24


@pytest.mark.parametrize("state", sorted(db.TERMINAL_STATES))
def test_v25_migration_refuses_every_unfinalized_terminal_v24_run(
    tmp_path, monkeypatch, state
):
    path, _run_id = _v24_db_with_run(
        tmp_path, monkeypatch, state=state, finalized=False
    )

    with pytest.raises(RuntimeError, match="zero-unfinalized drain"):
        db.migrate(path)

    assert db.current_schema_version(path) == 24
    with db.connect(path) as conn:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'run_finalization_claim'"
        ).fetchone()
    assert table is None


@pytest.mark.parametrize("state", sorted(db.TERMINAL_STATES))
def test_v25_migration_accepts_only_finalized_terminal_v24_runs(
    tmp_path, monkeypatch, state
):
    path, run_id = _v24_db_with_run(
        tmp_path, monkeypatch, state=state, finalized=True
    )
    before = db.get_run(path, run_id)["finalized_at"]

    db.migrate(path)

    assert db.current_schema_version(path) == 25
    assert db.get_run(path, run_id)["finalized_at"] == before
    with db.connect(path) as conn:
        claim_count = conn.execute(
            "SELECT COUNT(*) AS c FROM run_finalization_claim"
        ).fetchone()["c"]
        v25_count = conn.execute(
            "SELECT COUNT(*) AS c FROM schema_version WHERE version = 25"
        ).fetchone()["c"]
    assert claim_count == 0
    assert v25_count == 1


# Parametrized over the *actual* recorded migration versions below the current
# head — not a contiguous ``range`` — because the migration sequence may carry
# reserved-version gaps: a version can be pre-assigned to a sibling engine that
# lands on its own branch (e.g. council took v22 while models/market hold v20/v21
# on theirs), so those numbers are not reachable heads on this branch. A historical
# database only ever recorded a real migration version, so those are exactly the
# ones that must upgrade cleanly; the gap fills in on merge without changing this
# test.
@pytest.mark.parametrize(
    "historical_version",
    [version for version, _ in db.MIGRATIONS if version < db.SCHEMA_VERSION],
)
def test_upgrade_from_every_supported_historical_schema(
    tmp_path, monkeypatch, historical_version
):
    path = tmp_path / f"runtime-v{historical_version}.db"
    current_migrations = list(db.MIGRATIONS)
    current_version = db.SCHEMA_VERSION
    historical_migrations = [
        migration for migration in current_migrations if migration[0] <= historical_version
    ]
    # Version numbers may be *reserved* with a gap (a sibling wave pre-assigns a
    # number that lands only on merge — e.g. v20 is reserved while v21 ships), so
    # the recorded schema version at a historical point is the highest migration
    # actually present up to that point, not the parametrised number itself.
    expected_recorded = max((m[0] for m in historical_migrations), default=0)
    with monkeypatch.context() as historical:
        historical.setattr(db, "MIGRATIONS", historical_migrations)
        historical.setattr(db, "SCHEMA_VERSION", historical_version)
        db.migrate(path)
        assert db.current_schema_version(path) == expected_recorded

    # Pinned to the module constant, not a literal: hard-coding the number here
    # made this test fail on every schema addition for a reason unrelated to
    # what it verifies (that a historical database upgrades cleanly).
    assert db.SCHEMA_VERSION == current_version
    db.migrate(path)
    db.migrate(path)
    assert db.current_schema_version(path) == db.SCHEMA_VERSION
    with db.connect(path) as conn:
        run_columns = {row["name"] for row in conn.execute("PRAGMA table_info(run)")}
    assert {"provider_id", "provider_metadata_json"} <= run_columns


def test_v5_historical_runs_migrate_to_claude_provider_default(tmp_path, monkeypatch):
    path = tmp_path / "runtime-v5-with-run.db"
    current_migrations = list(db.MIGRATIONS)
    with monkeypatch.context() as historical:
        historical.setattr(db, "MIGRATIONS", current_migrations[:5])
        historical.setattr(db, "SCHEMA_VERSION", 5)
        db.migrate(path)
        task = db.create_task(path, project="AIOS", title="historical", task_type="review")
        session = db.create_session(
            path, task_id=task["id"], project="AIOS", repository_path="/tmp/historical"
        )
        now = db.iso_now()
        with db.connect(path) as conn:
            with db.transaction(conn):
                conn.execute(
                    """INSERT INTO run (
                           id, session_id, task_id, sequence, is_resume, state,
                           project, task_type, repository_path, prompt,
                           cancel_requested, version, created_at, updated_at
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        "historical-run",
                        session["id"],
                        task["id"],
                        1,
                        0,
                        "COMPLETED",
                        "AIOS",
                        "review",
                        "/tmp/historical",
                        "historical",
                        0,
                        0,
                        now,
                        now,
                    ),
                )

    # v25 is intentionally a controlled, non-rolling cutover: first bring the
    # old file to v24 while intake is stopped, verify/remediate the historical
    # terminal row, and only then enable claim fencing.
    with monkeypatch.context() as pre_claim:
        pre_claim.setattr(db, "MIGRATIONS", current_migrations[:-1])
        pre_claim.setattr(db, "SCHEMA_VERSION", 24)
        db.migrate(path)
    with db.connect(path) as conn:
        with db.transaction(conn):
            conn.execute(
                "UPDATE run SET finalized_at = ? WHERE id = ?",
                (db.iso_now(), "historical-run"),
            )
    db.migrate(path)
    historical_run = db.get_run(path, "historical-run")
    assert historical_run["provider_id"] == "claude_code"
    assert historical_run["provider_metadata_json"] is None
    with db.connect(path) as conn:
        rows = conn.execute("SELECT COUNT(*) AS c FROM schema_version").fetchone()
        # Only one row per migration actually applied, not one per migrate() call.
        assert rows["c"] == len(db.MIGRATIONS)


def test_migrate_creates_all_expected_tables(tmp_path):
    path = _fresh_db(tmp_path)
    with db.connect(path) as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    for expected in (
        "task",
        "session",
        "run",
        "run_event",
        "report",
        "run_finalization_claim",
        "schema_version",
    ):
        assert expected in tables


def test_v25_claim_table_shape_index_constraints_and_cascade(tmp_path):
    path = _fresh_db(tmp_path)
    with db.connect(path) as conn:
        columns = {
            row["name"]: row
            for row in conn.execute(
                "PRAGMA table_info(run_finalization_claim)"
            ).fetchall()
        }
        indexes = {
            row["name"]: row
            for row in conn.execute(
                "PRAGMA index_list(run_finalization_claim)"
            ).fetchall()
        }
        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(run_finalization_claim)"
        ).fetchall()
    assert set(columns) == {
        "run_id",
        "owner_token",
        "owner_pid",
        "owner_identity",
        "claimed_at",
        "completed_at",
    }
    assert columns["run_id"]["pk"] == 1
    assert columns["run_id"]["notnull"] == 1
    assert columns["owner_token"]["notnull"] == 1
    assert columns["owner_pid"]["notnull"] == 1
    assert columns["owner_identity"]["notnull"] == 1
    assert columns["claimed_at"]["notnull"] == 1
    assert indexes["idx_run_finalization_claim_open"]["partial"] == 1
    assert any(
        row["table"] == "run"
        and row["from"] == "run_id"
        and row["to"] == "id"
        and row["on_delete"] == "CASCADE"
        for row in foreign_keys
    )

    task = db.create_task(
        path, project="AIOS", title="cascade", task_type="implementation"
    )
    session = db.create_session(
        path,
        task_id=task["id"],
        project="AIOS",
        repository_path="/tmp/cascade",
    )
    run = db.create_run(
        path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        repository_path="/tmp/cascade",
        task_type="implementation",
        prompt="cascade",
        is_resume=False,
        finalization_owner_token="token",
        finalization_owner_pid=1,
        finalization_owner_identity="identity",
    )
    with db.connect(path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction(conn):
                conn.execute(
                    "INSERT INTO run_finalization_claim "
                    "(run_id, owner_token, owner_pid, owner_identity, claimed_at) "
                    "VALUES (NULL, 'token', 1, 'identity', '2026-01-01T00:00:00')"
                )
        with pytest.raises(sqlite3.IntegrityError):
            with db.transaction(conn):
                conn.execute(
                    "UPDATE run_finalization_claim SET completed_at = '0000' "
                    "WHERE run_id = ?",
                    (run["id"],),
                )
        with db.transaction(conn):
            conn.execute("DELETE FROM run WHERE id = ?", (run["id"],))
        remaining = conn.execute(
            "SELECT COUNT(*) AS c FROM run_finalization_claim"
        ).fetchone()["c"]
    assert remaining == 0


@pytest.mark.parametrize(
    ("owner_token", "owner_identity"),
    [("", "identity"), ("token", "")],
)
def test_create_run_rejects_blank_finalization_owner_fields(
    tmp_path, owner_token, owner_identity
):
    path = _fresh_db(tmp_path)
    task = db.create_task(
        path, project="AIOS", title="invalid owner", task_type="implementation"
    )
    session = db.create_session(
        path,
        task_id=task["id"],
        project="AIOS",
        repository_path="/tmp/invalid-owner",
    )

    with pytest.raises(ValueError, match="must be non-empty"):
        db.create_run(
            path,
            session_id=session["id"],
            task_id=task["id"],
            project="AIOS",
            repository_path="/tmp/invalid-owner",
            task_type="implementation",
            prompt="invalid owner",
            is_resume=False,
            finalization_owner_token=owner_token,
            finalization_owner_pid=1,
            finalization_owner_identity=owner_identity,
        )


def test_migrate_on_existing_populated_db_does_not_lose_data(tmp_path):
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    db.migrate(path)  # re-run against a populated db
    assert db.get_task(path, task["id"]) is not None


# --------------------------------------------------------------------------
# WAL / busy_timeout / foreign_keys configuration
# --------------------------------------------------------------------------


def test_connection_uses_wal_journal_mode(tmp_path):
    path = _fresh_db(tmp_path)
    with db.connect(path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"


def test_connection_sets_busy_timeout(tmp_path):
    path = _fresh_db(tmp_path)
    with db.connect(path) as conn:
        timeout_ms = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert timeout_ms == 30000


def test_connection_enables_foreign_keys(tmp_path):
    path = _fresh_db(tmp_path)
    with db.connect(path) as conn:
        enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        assert enabled == 1


def test_foreign_keys_cascade_delete_task_removes_descendants(tmp_path):
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = db.create_run(
        path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="implementation",
        repository_path="/tmp/x",
        prompt="p",
        is_resume=False,
    )
    db.append_run_event(path, run["id"], "lifecycle", {"lifecycle": "x"})

    with db.connect(path) as conn:
        with db.transaction(conn):
            conn.execute("DELETE FROM task WHERE id = ?", (task["id"],))

    assert db.get_session(path, session["id"]) is None
    assert db.get_run(path, run["id"]) is None
    assert db.list_run_events(path, run["id"]) == []


def test_foreign_keys_reject_orphan_session(tmp_path):
    path = _fresh_db(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        db.create_session(path, task_id="no-such-task", project="AIOS", repository_path="/tmp/x")


# --------------------------------------------------------------------------
# Concurrent writers (WAL + busy_timeout should serialize, not fail)
# --------------------------------------------------------------------------


def test_concurrent_writers_append_events_without_error_or_loss(tmp_path):
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = db.create_run(
        path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="implementation",
        repository_path="/tmp/x",
        prompt="p",
        is_resume=False,
    )

    errors: list[Exception] = []
    n_threads = 8
    n_events_per_thread = 15

    def writer(idx: int) -> None:
        try:
            for i in range(n_events_per_thread):
                db.append_run_event(path, run["id"], "lifecycle", {"thread": idx, "i": i})
        except Exception as exc:  # pragma: no cover - failure path under test
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"concurrent writers raised: {errors}"
    events = db.list_run_events(path, run["id"], limit=10_000)
    assert len(events) == n_threads * n_events_per_thread
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs), "sequence numbers must be unique (no lost/duplicated seq under concurrency)"


# F8: a genuine multi-*process* writer test (separate OS processes, each with
# its own sqlite3 connection/interpreter), not only multi-threaded contention
# within one process. This is not a hypothetical scenario for this codebase —
# independent review reproduced two separate CLI invocations racing against
# the same db file (see F1/F5).


def test_true_multi_process_writers_append_events_without_error_or_loss(tmp_path):
    path = tmp_path / "runtime.db"
    db.migrate(path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = db.create_run(
        path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p", is_resume=False,
    )

    n_procs = 4
    n_per_proc = 10
    ctx = multiprocessing.get_context("spawn")
    procs = [
        ctx.Process(target=_mp_event_writer_worker, args=(str(path), run["id"], i, n_per_proc))
        for i in range(n_procs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)

    for i, p in enumerate(procs):
        assert p.exitcode == 0, f"writer process {i} exited with code {p.exitcode}"

    events = db.list_run_events(path, run["id"], limit=10_000)
    assert len(events) == n_procs * n_per_proc
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs), "sequence numbers must be unique across separate OS processes, not just threads"


def test_concurrent_first_time_migration_from_separate_processes_is_safe(tmp_path):
    """F5: two processes racing to apply migration N for the first time
    against a brand-new db file must not corrupt `schema_version` — the
    loser's INSERT hits the `PRIMARY KEY` on `version` and is swallowed as
    'already recorded', not raised."""
    path = tmp_path / "runtime.db"
    n_procs = 4
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_mp_migrate_worker, args=(str(path),)) for _ in range(n_procs)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)

    for i, p in enumerate(procs):
        assert p.exitcode == 0, f"migrate() worker {i} exited with code {p.exitcode}"

    assert db.current_schema_version(path) == db.SCHEMA_VERSION
    with db.connect(path) as conn:
        rows = conn.execute("SELECT version, COUNT(*) AS c FROM schema_version GROUP BY version").fetchall()
        assert len(rows) == len(db.MIGRATIONS)
        for row in rows:
            assert row["c"] == 1, "schema_version.version must stay unique even under concurrent first-time migration"


# --------------------------------------------------------------------------
# Barrier-synchronized concurrent first-time migration: a genuine race
# (every worker released at the same instant, against a database path that
# does not yet exist), repeated enough times to reliably catch an
# intermittent regression rather than getting lucky once. This is the
# regression coverage for the "database is locked" fix in `connect()` /
# `_migration_2_add_failure_reason` — before that fix, this scenario failed
# roughly 1 run in 5 with an uncaught `sqlite3.OperationalError: database is
# locked` raised from `PRAGMA journal_mode=WAL`; after fixing that, it
# uncovered a second, previously-masked race in migration 2's check-then-add
# `ALTER TABLE` (both fixed in `command_center/runtime/db.py`).
# --------------------------------------------------------------------------

_STRESS_REPETITIONS = 12
_STRESS_PROCS_PER_REPETITION = 6


def test_concurrent_first_time_migration_is_safe_under_a_genuine_barrier_synchronized_race(tmp_path):
    ctx = multiprocessing.get_context("spawn")

    for attempt in range(_STRESS_REPETITIONS):
        path = tmp_path / f"race_{attempt}" / "runtime.db"
        barrier = ctx.Barrier(_STRESS_PROCS_PER_REPETITION)
        result_queue = ctx.Queue()
        procs = [
            ctx.Process(target=_mp_barrier_migrate_worker, args=(str(path), barrier, result_queue))
            for _ in range(_STRESS_PROCS_PER_REPETITION)
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=60)

        results = [result_queue.get_nowait() for _ in range(_STRESS_PROCS_PER_REPETITION)]
        failures = [r for r in results if r[0] != "ok"]
        assert not failures, (
            f"attempt {attempt}: {len(failures)}/{_STRESS_PROCS_PER_REPETITION} workers failed "
            f"(no worker may ever see an uncaught database-locked/busy exception): {failures}"
        )
        for i, p in enumerate(procs):
            assert p.exitcode == 0, f"attempt {attempt}: worker {i} exited with code {p.exitcode}"

        # Every returned database must be fully, correctly migrated — no
        # partial migration state, no duplicate schema objects.
        assert db.current_schema_version(path) == db.SCHEMA_VERSION, f"attempt {attempt}: schema not fully applied"
        with db.connect(path) as conn:
            version_rows = conn.execute(
                "SELECT version, COUNT(*) AS c FROM schema_version GROUP BY version"
            ).fetchall()
            assert len(version_rows) == len(db.MIGRATIONS), f"attempt {attempt}: wrong number of migrations recorded"
            for row in version_rows:
                assert row["c"] == 1, f"attempt {attempt}: duplicate schema_version row for version {row['version']}"

            tables = {
                r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            for expected in ("task", "session", "run", "run_event", "report", "schema_version"):
                assert expected in tables, f"attempt {attempt}: missing table {expected!r}"

            run_columns = {r["name"] for r in conn.execute("PRAGMA table_info(run)").fetchall()}
            assert "failure_reason" in run_columns, (
                f"attempt {attempt}: migration 2's failure_reason column is missing "
                "(the exact partial-migration-state failure mode this test guards against)"
            )

            # The returned connection/database must be genuinely usable, not
            # just structurally present — a real write-then-read round trip.
            task = db.create_task(path, project="AIOS", title="race check", task_type="implementation")
            assert db.get_task(path, task["id"])["title"] == "race check"


# --------------------------------------------------------------------------
# Negative test: an unrelated OperationalError must never be retried or
# hidden by the busy/locked retry wrapper — only a genuine
# SQLITE_BUSY/SQLITE_LOCKED condition may be retried.
# --------------------------------------------------------------------------


def test_retry_on_busy_does_not_retry_or_swallow_unrelated_operational_errors():
    call_count = 0

    def always_raises_unrelated_error():
        nonlocal call_count
        call_count += 1
        raise sqlite3.OperationalError("no such table: definitely_not_a_lock_issue")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        db._retry_on_busy(always_raises_unrelated_error, deadline_seconds=5.0)

    assert call_count == 1, "an unrelated OperationalError must propagate on the first attempt, never retried"


def test_retry_on_busy_does_not_wrap_unrelated_operational_errors_in_the_busy_timeout_error():
    def raises_unrelated_error():
        raise sqlite3.OperationalError("unable to open database file")

    try:
        db._retry_on_busy(raises_unrelated_error, deadline_seconds=5.0)
        pytest.fail("expected sqlite3.OperationalError to propagate")
    except db.DatabaseBusyTimeoutError:
        pytest.fail("an unrelated OperationalError must never be reclassified as a busy/locked timeout")
    except sqlite3.OperationalError:
        pass  # expected: the original, unmodified exception type propagates


def test_is_busy_or_locked_false_for_synthetic_unrelated_operational_errors():
    """A hand-constructed `OperationalError` (not produced by the sqlite3 C
    extension) has no `sqlite_errorcode`/`sqlite_errorname`, exercising the
    message-substring fallback path — it must still correctly classify a
    non-lock error as non-retryable."""
    assert db._is_busy_or_locked(sqlite3.OperationalError("no such column: bogus")) is False
    assert db._is_busy_or_locked(sqlite3.OperationalError("syntax error")) is False


def test_is_busy_or_locked_true_for_a_genuine_sqlite_busy_error(tmp_path):
    """Forces a *real* SQLITE_BUSY from the sqlite3 C extension (not a
    hand-constructed message string) by holding a write lock open on one
    connection while a second, short-timeout connection tries to write —
    confirms `_is_busy_or_locked` correctly recognizes the real thing, not
    just a string that happens to look like it."""
    path = tmp_path / "busy.db"
    db.migrate(path)

    holder = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    contender = sqlite3.connect(str(path), timeout=0.2, isolation_level=None)
    try:
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("CREATE TABLE IF NOT EXISTS _busy_probe (x INTEGER)")
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            contender.execute("BEGIN IMMEDIATE")
            contender.execute("INSERT INTO task (id, project, title, task_type, created_at, updated_at) VALUES ('x','x','x','x','x','x')")
        assert db._is_busy_or_locked(excinfo.value) is True
    finally:
        holder.execute("ROLLBACK")
        holder.close()
        try:
            contender.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        contender.close()


def test_retry_on_busy_succeeds_once_the_lock_clears():
    """A busy condition that clears before the deadline must succeed
    transparently — the caller gets a normal return value, not an
    exception, and the retry loop stops as soon as `fn()` stops raising."""
    attempts = []

    def fails_twice_then_succeeds():
        attempts.append(1)
        if len(attempts) < 3:
            raise sqlite3.OperationalError("database is locked")
        return "ok"

    result = db._retry_on_busy(fails_twice_then_succeeds, deadline_seconds=5.0)
    assert result == "ok"
    assert len(attempts) == 3


def test_retry_on_busy_raises_domain_error_with_original_cause_when_deadline_exhausted():
    def always_busy():
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(db.DatabaseBusyTimeoutError) as excinfo:
        db._retry_on_busy(always_busy, deadline_seconds=0.05)

    assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
    assert "database is locked" in str(excinfo.value.__cause__)


# --------------------------------------------------------------------------
# Compare-and-set: lost-update prevention
# --------------------------------------------------------------------------


def _make_run(path):
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    return db.create_run(
        path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="implementation",
        repository_path="/tmp/x",
        prompt="p",
        is_resume=False,
    )


def test_update_run_state_succeeds_with_correct_version(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    updated = db.update_run_state(path, run["id"], expected_version=run["version"], new_state="QUEUED")
    assert updated["state"] == "QUEUED"
    assert updated["version"] == run["version"] + 1


def test_update_run_state_raises_lost_update_on_stale_version(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    db.update_run_state(path, run["id"], expected_version=run["version"], new_state="QUEUED")
    with pytest.raises(db.LostUpdateError):
        # `run["version"]` is now stale (already consumed above).
        db.update_run_state(path, run["id"], expected_version=run["version"], new_state="RUNNING")


def test_concurrent_cas_updates_exactly_one_writer_wins(tmp_path):
    """Two threads race to CAS-update the same run's non-state fields using the
    same (correct-at-read-time) expected_version; exactly one may succeed and
    the other must observe a lost update, purely from the version mismatch
    (not from any state-transition legality question, which
    `update_run_fields` does not evaluate at all)."""
    path = _fresh_db(tmp_path)
    run = _make_run(path)

    results: list[str] = []
    lock = threading.Lock()

    def try_update(pid_value: int) -> None:
        try:
            db.update_run_fields(path, run["id"], expected_version=run["version"], fields={"pid": pid_value})
            with lock:
                results.append(f"ok:{pid_value}")
        except db.LostUpdateError:
            with lock:
                results.append("lost")

    t1 = threading.Thread(target=try_update, args=(111,))
    t2 = threading.Thread(target=try_update, args=(222,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sorted(results) in (["lost", "ok:111"], ["lost", "ok:222"])
    final = db.get_run(path, run["id"])
    assert final["pid"] in (111, 222)
    assert final["version"] == run["version"] + 1


# --------------------------------------------------------------------------
# Terminal-state protection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("origin_state", ["PREPARED", "QUEUED"])
@pytest.mark.parametrize("crash_classification", ["INTERRUPTED", "UNKNOWN"])
def test_prepared_and_queued_can_transition_to_crash_recovery_states(tmp_path, origin_state, crash_classification):
    """`Supervisor.reconcile()` needs PREPARED/QUEUED -> INTERRUPTED/UNKNOWN
    to classify a row abandoned mid-launch by a crashed Supervisor (see
    `ALLOWED_TRANSITIONS`'s docstring) — this is a permission check, not a
    reconciliation test (that's `tests/test_runtime_reconciliation.py`)."""
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    if origin_state == "QUEUED":
        run = db.update_run_state(path, run["id"], expected_version=run["version"], new_state="QUEUED")
    run = db.update_run_state(
        path, run["id"], expected_version=run["version"], new_state=crash_classification
    )
    assert run["state"] == crash_classification


@pytest.mark.parametrize("terminal_state", sorted(db.TERMINAL_STATES))
def test_terminal_states_never_transition_anywhere(tmp_path, terminal_state):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    run = db.update_run_state(path, run["id"], expected_version=run["version"], new_state="QUEUED")
    run = db.update_run_state(path, run["id"], expected_version=run["version"], new_state="RUNNING")
    run = db.update_run_state(path, run["id"], expected_version=run["version"], new_state=terminal_state)

    for target in db.RUN_STATES:
        if target == terminal_state:
            continue
        with pytest.raises(db.InvalidTransitionError):
            db.update_run_state(path, run["id"], expected_version=run["version"], new_state=target)


def test_completed_run_cannot_silently_return_to_running(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    run = db.update_run_state(path, run["id"], expected_version=run["version"], new_state="QUEUED")
    run = db.update_run_state(path, run["id"], expected_version=run["version"], new_state="RUNNING")
    run = db.update_run_state(path, run["id"], expected_version=run["version"], new_state="COMPLETED")
    with pytest.raises(db.InvalidTransitionError):
        db.update_run_state(path, run["id"], expected_version=run["version"], new_state="RUNNING")
    assert db.get_run(path, run["id"])["state"] == "COMPLETED"


def test_invalid_transition_does_not_bump_version(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    run = db.update_run_state(path, run["id"], expected_version=run["version"], new_state="QUEUED")
    run = db.update_run_state(path, run["id"], expected_version=run["version"], new_state="RUNNING")
    run = db.update_run_state(path, run["id"], expected_version=run["version"], new_state="COMPLETED")
    version_before = run["version"]
    with pytest.raises(db.InvalidTransitionError):
        db.update_run_state(path, run["id"], expected_version=run["version"], new_state="FAILED")
    assert db.get_run(path, run["id"])["version"] == version_before


# --------------------------------------------------------------------------
# F6: unknown update fields are rejected before any SQL is built
# --------------------------------------------------------------------------


def test_update_run_state_rejects_unknown_field(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    with pytest.raises(db.UnknownRunFieldError):
        db.update_run_state(
            path, run["id"], expected_version=run["version"], new_state="QUEUED",
            fields={"prompt": "attempted overwrite of a write-once column"},
        )
    # Must not have partially applied — the row is untouched.
    assert db.get_run(path, run["id"])["version"] == run["version"]
    assert db.get_run(path, run["id"])["state"] == "PREPARED"


def test_update_run_fields_rejects_unknown_field(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    with pytest.raises(db.UnknownRunFieldError):
        db.update_run_fields(
            path, run["id"], expected_version=run["version"],
            fields={"session_id": "attempted overwrite of a write-once column"},
        )
    assert db.get_run(path, run["id"])["version"] == run["version"]


def test_update_run_fields_accepts_every_documented_updatable_field(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    # `cancel_requested` is NOT NULL (default 0); every other updatable field
    # is nullable. This exercises the full allowlist with schema-valid values.
    fields = {key: (0 if key == "cancel_requested" else None) for key in db._UPDATABLE_RUN_FIELDS}
    updated = db.update_run_fields(path, run["id"], expected_version=run["version"], fields=fields)
    assert updated["version"] == run["version"] + 1


# --------------------------------------------------------------------------
# Task 1:N Session, Session 1:N Run, Run 0..1 Report, Run 1:N RunEvent
# --------------------------------------------------------------------------


def test_auto_generated_session_id_is_a_canonical_dashed_uuid(tmp_path):
    """`session.id` is passed straight to `claude --session-id`/`--resume`,
    which rejects anything that isn't a valid UUID string — verified against
    the real `claude` CLI during Sprint 1 end-to-end validation, which caught
    an earlier version of this code generating a bare 32-char hex digest
    (`uuid4().hex`, no dashes) instead."""
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    parsed = uuid.UUID(session["id"])
    assert str(parsed) == session["id"]
    assert "-" in session["id"]


def test_task_can_have_multiple_sessions(tmp_path):
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    s1 = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    s2 = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    sessions = db.list_sessions(path, task_id=task["id"])
    assert {s["id"] for s in sessions} == {s1["id"], s2["id"]}


def test_session_can_have_multiple_runs_with_incrementing_sequence(tmp_path):
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    r1 = db.create_run(
        path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p1", is_resume=False,
    )
    r2 = db.create_run(
        path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p2", is_resume=True,
    )
    assert r1["sequence"] == 1
    assert r2["sequence"] == 2
    runs = db.list_runs(path, session_id=session["id"])
    assert {r["id"] for r in runs} == {r1["id"], r2["id"]}


# --------------------------------------------------------------------------
# Workspace locking — `create_run(enforce_workspace_lock=True)`
# --------------------------------------------------------------------------


def test_create_run_without_workspace_lock_allows_multiple_active_runs_same_path(tmp_path):
    """Default (`enforce_workspace_lock=False`) behavior is unchanged — this
    is what every other test in this file (and every direct db-layer test in
    general) relies on when it creates several concurrently-"active" `run`
    rows against the same throwaway `repository_path` with no real process
    behind any of them."""
    path = _fresh_db(tmp_path)
    r1 = _make_run(path)
    r2 = _make_run(path)
    assert r1["state"] == "PREPARED"
    assert r2["state"] == "PREPARED"


def test_create_run_with_workspace_lock_raises_when_another_active_run_holds_workspace(tmp_path):
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    first = db.create_run(
        path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p", is_resume=False, enforce_workspace_lock=True,
    )
    assert first["state"] == "PREPARED"

    with pytest.raises(db.WorkspaceLockedError) as excinfo:
        db.create_run(
            path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
            repository_path="/tmp/x", prompt="p2", is_resume=False, enforce_workspace_lock=True,
        )
    assert excinfo.value.conflicting_run["id"] == first["id"]

    # The rejected attempt must never have been inserted.
    runs = db.list_runs(path, session_id=session["id"])
    assert [r["id"] for r in runs] == [first["id"]]


def test_create_run_task_lock_rejects_second_active_run_same_task_other_workspace(tmp_path):
    # M1: the workspace lock only catches a double-launch resolving to the SAME
    # path. When the same task resolves to a DIFFERENT workspace on the second
    # in-flight launch, the task-id exclusivity check is what stops two agents
    # running for one task.
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    s1 = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/a")
    s2 = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/b")
    first = db.create_run(
        path, session_id=s1["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/a", prompt="p", is_resume=False, enforce_workspace_lock=True,
    )
    assert first["state"] == "PREPARED"

    # Same task, DIFFERENT workspace: passes the workspace lock, hits the task lock.
    with pytest.raises(db.TaskAlreadyActiveError) as excinfo:
        db.create_run(
            path, session_id=s2["id"], task_id=task["id"], project="AIOS", task_type="implementation",
            repository_path="/tmp/b", prompt="p2", is_resume=False, enforce_workspace_lock=True,
        )
    assert excinfo.value.conflicting_run["id"] == first["id"]
    # The rejected attempt was never inserted.
    assert [r["id"] for r in db.list_runs(path, session_id=s2["id"])] == []


def test_create_run_task_lock_allows_relaunch_after_prior_run_terminal(tmp_path):
    # A legitimate re-launch after the prior run finished must still succeed.
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    s1 = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/a")
    s2 = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/b")
    first = db.create_run(
        path, session_id=s1["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/a", prompt="p", is_resume=False, enforce_workspace_lock=True,
        finalization_owner_token="owner", finalization_owner_pid=123,
        finalization_owner_identity="start|command",
    )
    first = db.update_run_state(path, first["id"], expected_version=first["version"], new_state="QUEUED")
    first = db.update_run_state(path, first["id"], expected_version=first["version"], new_state="RUNNING")
    first = db.update_run_state(path, first["id"], expected_version=first["version"], new_state="COMPLETED")

    with pytest.raises(db.TaskAlreadyActiveError):
        db.create_run(
            path, session_id=s2["id"], task_id=task["id"], project="AIOS",
            task_type="implementation", repository_path="/tmp/b", prompt="blocked",
            is_resume=False, enforce_workspace_lock=True,
        )
    assert db.mark_run_finalized(path, first["id"], owner_token="owner") is not None

    second = db.create_run(
        path, session_id=s2["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/b", prompt="p2", is_resume=False, enforce_workspace_lock=True,
    )
    assert second["state"] == "PREPARED"


@pytest.mark.parametrize("active_state", sorted(db.EXECUTION_CENTER_ACTIVE_STATES))
def test_create_run_with_workspace_lock_conflicts_on_every_active_state(tmp_path, active_state):
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    first = db.create_run(
        path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p", is_resume=False,
    )
    path_to_state = {"PREPARED": [], "QUEUED": ["QUEUED"], "RUNNING": ["QUEUED", "RUNNING"]}
    for target in path_to_state[active_state]:
        first = db.update_run_state(path, first["id"], expected_version=first["version"], new_state=target)
    assert first["state"] == active_state

    with pytest.raises(db.WorkspaceLockedError):
        db.create_run(
            path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
            repository_path="/tmp/x", prompt="p2", is_resume=False, enforce_workspace_lock=True,
        )


def test_create_run_with_workspace_lock_waits_for_terminal_finalization(tmp_path):
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    first = db.create_run(
        path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p", is_resume=False,
        finalization_owner_token="owner", finalization_owner_pid=123,
        finalization_owner_identity="start|command",
    )
    first = db.update_run_state(path, first["id"], expected_version=first["version"], new_state="QUEUED")
    first = db.update_run_state(path, first["id"], expected_version=first["version"], new_state="RUNNING")
    first = db.update_run_state(
        path, first["id"], expected_version=first["version"], new_state="COMPLETED"
    )

    with pytest.raises(db.WorkspaceLockedError):
        db.create_run(
            path, session_id=session["id"], task_id=task["id"], project="AIOS",
            task_type="implementation", repository_path="/tmp/x", prompt="blocked",
            is_resume=True, enforce_workspace_lock=True,
        )
    assert db.mark_run_finalized(path, first["id"], owner_token="owner") is not None

    second = db.create_run(
        path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p2", is_resume=True, enforce_workspace_lock=True,
    )
    assert second["state"] == "PREPARED"


def test_create_run_with_workspace_lock_does_not_conflict_across_different_paths(tmp_path):
    # The workspace lock is scoped to repository_path: two DIFFERENT tasks, each
    # in its own workspace, launch concurrently without conflict. (The same task
    # in two workspaces is separately rejected by the task lock — see
    # test_create_run_task_lock_rejects_second_active_run_same_task_other_workspace.)
    path = _fresh_db(tmp_path)
    task_a = db.create_task(path, project="AIOS", title="a", task_type="implementation")
    task_b = db.create_task(path, project="AIOS", title="b", task_type="implementation")
    session_a = db.create_session(path, task_id=task_a["id"], project="AIOS", repository_path="/tmp/a")
    session_b = db.create_session(path, task_id=task_b["id"], project="AIOS", repository_path="/tmp/b")
    run_a = db.create_run(
        path, session_id=session_a["id"], task_id=task_a["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/a", prompt="p", is_resume=False, enforce_workspace_lock=True,
    )
    run_b = db.create_run(
        path, session_id=session_b["id"], task_id=task_b["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/b", prompt="p", is_resume=False, enforce_workspace_lock=True,
    )
    assert run_a["state"] == "PREPARED"
    assert run_b["state"] == "PREPARED"


def test_create_run_workspace_lock_rejected_insert_does_not_advance_sequence(tmp_path):
    """A rejected `create_run` must not have consumed a `sequence` number —
    proof the `INSERT` genuinely never ran (the conflict check and the
    insert share one transaction; see `WorkspaceLockedError`'s docstring)."""
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    first = db.create_run(
        path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p", is_resume=False,
        finalization_owner_token="owner", finalization_owner_pid=123,
        finalization_owner_identity="start|command",
    )
    with pytest.raises(db.WorkspaceLockedError):
        db.create_run(
            path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
            repository_path="/tmp/x", prompt="p2", is_resume=False, enforce_workspace_lock=True,
        )
    db.update_run_state(path, first["id"], expected_version=first["version"], new_state="CANCELLED")
    assert db.mark_run_finalized(path, first["id"], owner_token="owner") is not None
    third = db.create_run(
        path, session_id=session["id"], task_id=task["id"], project="AIOS", task_type="implementation",
        repository_path="/tmp/x", prompt="p3", is_resume=False, enforce_workspace_lock=True,
    )
    assert third["sequence"] == 2, "the rejected attempt must not have consumed sequence 2"


def test_create_run_workspace_lock_is_race_free_under_concurrent_callers(tmp_path):
    """The check-then-insert must not lose a race between two genuinely
    concurrent callers targeting the same workspace: `BEGIN IMMEDIATE`
    serializes them, so exactly one must succeed and every other must see
    the winner's row and raise `WorkspaceLockedError` — never two active
    rows for the same `repository_path` at once."""
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")

    n_threads = 8
    successes: list[dict] = []
    conflicts: list[db.WorkspaceLockedError] = []
    lock = threading.Lock()

    def attempt(idx: int) -> None:
        try:
            run = db.create_run(
                path, session_id=session["id"], task_id=task["id"], project="AIOS",
                task_type="implementation", repository_path="/tmp/x", prompt=f"p{idx}",
                is_resume=False, enforce_workspace_lock=True,
            )
            with lock:
                successes.append(run)
        except db.WorkspaceLockedError as exc:
            with lock:
                conflicts.append(exc)

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert len(successes) == 1, f"exactly one concurrent caller must win the workspace lock, got {successes}"
    assert len(conflicts) == n_threads - 1
    winner_id = successes[0]["id"]
    assert all(exc.conflicting_run["id"] == winner_id for exc in conflicts)

    active = db.list_runs(path, states=db.EXECUTION_CENTER_ACTIVE_STATES)
    assert len(active) == 1, "no more than one active run for this workspace must ever have been persisted"


def test_report_is_at_most_one_per_run(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    db.create_report(path, run["id"], "reports/AIOS/x.md")
    with pytest.raises(sqlite3.IntegrityError):
        db.create_report(path, run["id"], "reports/AIOS/y.md")


def test_run_events_are_append_only_and_ordered(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    for i in range(5):
        db.append_run_event(path, run["id"], "lifecycle", {"i": i})
    events = db.list_run_events(path, run["id"])
    assert [e["payload"]["i"] for e in events] == [0, 1, 2, 3, 4]
    assert [e["seq"] for e in events] == [1, 2, 3, 4, 5]


def test_list_run_events_after_seq_returns_only_newer_events(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    for i in range(5):
        db.append_run_event(path, run["id"], "lifecycle", {"i": i})
    events = db.list_run_events(path, run["id"], after_seq=2)
    assert [e["payload"]["i"] for e in events] == [2, 3, 4]


# --------------------------------------------------------------------------
# list_runs — states (plural) / limit extension
# --------------------------------------------------------------------------


def test_list_runs_states_filters_to_matching_set(tmp_path):
    path = _fresh_db(tmp_path)
    prepared = _make_run(path)
    running = _make_run(path)
    running = db.update_run_state(path, running["id"], expected_version=running["version"], new_state="QUEUED")
    running = db.update_run_state(path, running["id"], expected_version=running["version"], new_state="RUNNING")
    completed = _make_run(path)
    completed = db.update_run_state(path, completed["id"], expected_version=completed["version"], new_state="QUEUED")
    completed = db.update_run_state(path, completed["id"], expected_version=completed["version"], new_state="RUNNING")
    completed = db.update_run_state(path, completed["id"], expected_version=completed["version"], new_state="COMPLETED")

    active = db.list_runs(path, states=db.EXECUTION_CENTER_ACTIVE_STATES)
    assert {r["id"] for r in active} == {prepared["id"], running["id"]}

    terminal = db.list_runs(path, states=db.TERMINAL_STATES)
    assert {r["id"] for r in terminal} == {completed["id"]}


def test_list_runs_states_empty_iterable_matches_nothing(tmp_path):
    path = _fresh_db(tmp_path)
    _make_run(path)
    assert db.list_runs(path, states=[]) == []


def test_list_runs_limit_bounds_result_set_and_preserves_order(tmp_path, monkeypatch):
    path = _fresh_db(tmp_path)

    # `iso_now()` is second-precision (see `models.iso_now`'s docstring), so creating
    # several runs in quick succession can produce identical `created_at` values.
    # Monkeypatch it to strictly-increasing timestamps so ordering is deterministic
    # without a real multi-second sleep per row. `_make_run` calls `iso_now()` three
    # times per run (task/session/run), so the ticker must not run dry.
    counter = itertools.count()

    def fake_iso_now() -> str:
        minute, second = divmod(next(counter), 60)
        return f"2026-01-01T00:{minute:02d}:{second:02d}"

    monkeypatch.setattr(db, "iso_now", fake_iso_now)

    runs = [_make_run(path) for _ in range(5)]

    limited = db.list_runs(path, limit=2)
    assert len(limited) == 2
    assert [r["id"] for r in limited] == [runs[4]["id"], runs[3]["id"]]

    unbounded = db.list_runs(path)
    assert [r["id"] for r in unbounded] == [r["id"] for r in reversed(runs)]


@pytest.mark.parametrize("negative_limit", [-1, -2, -100])
def test_list_runs_negative_limit_raises_value_error_before_sql(tmp_path, negative_limit):
    path = _fresh_db(tmp_path)
    _make_run(path)
    with pytest.raises(ValueError):
        db.list_runs(path, limit=negative_limit)


def test_list_runs_limit_none_is_unbounded(tmp_path):
    path = _fresh_db(tmp_path)
    runs = [_make_run(path) for _ in range(3)]
    assert len(db.list_runs(path, limit=None)) == len(runs)


def test_list_runs_limit_zero_returns_empty_list(tmp_path):
    path = _fresh_db(tmp_path)
    _make_run(path)
    assert db.list_runs(path, limit=0) == []


def test_list_runs_state_and_states_together_raises_value_error(tmp_path):
    path = _fresh_db(tmp_path)
    with pytest.raises(ValueError):
        db.list_runs(path, state="RUNNING", states=["RUNNING", "QUEUED"])


def test_list_runs_states_combined_with_session_id_filter(tmp_path):
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session_a = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    session_b = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run_a = db.create_run(
        path, session_id=session_a["id"], task_id=task["id"], project="AIOS",
        task_type="implementation", repository_path="/tmp/x", prompt="p", is_resume=False,
    )
    db.create_run(
        path, session_id=session_b["id"], task_id=task["id"], project="AIOS",
        task_type="implementation", repository_path="/tmp/x", prompt="p", is_resume=False,
    )

    scoped = db.list_runs(path, session_id=session_a["id"], states=db.EXECUTION_CENTER_ACTIVE_STATES)
    assert {r["id"] for r in scoped} == {run_a["id"]}


# --------------------------------------------------------------------------
# Migration 3 — Live Execution Center v2 fields (expected_branch,
# launch_source, prompt_version, commit_hash, pull_request_url)
# --------------------------------------------------------------------------


def test_migration_3_columns_exist_and_are_idempotent(tmp_path):
    path = tmp_path / "runtime.db"
    db.migrate(path)
    db.migrate(path)  # re-running must not raise (idempotent ALTER TABLE ADD COLUMN)
    with db.connect(path) as conn:
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(run)").fetchall()}
    for column in ("expected_branch", "launch_source", "prompt_version", "commit_hash", "pull_request_url"):
        assert column in columns


def test_create_run_persists_migration_3_fields(tmp_path):
    path = _fresh_db(tmp_path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    run = db.create_run(
        path,
        session_id=session["id"],
        task_id=task["id"],
        project="AIOS",
        task_type="implementation",
        repository_path="/tmp/x",
        prompt="p",
        is_resume=False,
        expected_branch="feature/p1-7-deployment",
        launch_source="kanban_task",
        prompt_version=3,
    )
    assert run["expected_branch"] == "feature/p1-7-deployment"
    assert run["launch_source"] == "kanban_task"
    assert run["prompt_version"] == 3
    assert run["commit_hash"] is None
    assert run["pull_request_url"] is None

    reloaded = db.get_run(path, run["id"])
    assert reloaded["expected_branch"] == "feature/p1-7-deployment"
    assert reloaded["launch_source"] == "kanban_task"
    assert reloaded["prompt_version"] == 3


def test_create_run_defaults_migration_3_fields_to_none(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    assert run["expected_branch"] is None
    assert run["launch_source"] is None
    assert run["prompt_version"] is None
    assert run["commit_hash"] is None
    assert run["pull_request_url"] is None


def test_set_run_result_fields_updates_commit_hash_and_pr_url(tmp_path):
    path = _fresh_db(tmp_path)
    run = _make_run(path)
    updated = db.set_run_result_fields(
        path, run["id"], expected_version=run["version"],
        commit_hash="abc1234", pull_request_url="https://example.invalid/pr/1",
    )
    assert updated["commit_hash"] == "abc1234"
    assert updated["pull_request_url"] == "https://example.invalid/pr/1"
    assert updated["version"] == run["version"] + 1


def test_legacy_pre_migration_3_row_loads_with_none_defaults(tmp_path):
    """A `run` row inserted before migration 3 ran (simulated by inserting
    directly with only the migration-1/2 columns) must still load cleanly —
    additive schema evolution, never a destructive migration."""
    path = tmp_path / "runtime.db"
    db.migrate(path)
    task = db.create_task(path, project="AIOS", title="t", task_type="implementation")
    session = db.create_session(path, task_id=task["id"], project="AIOS", repository_path="/tmp/x")
    now = "2026-01-01T00:00:00"
    with db.connect(path) as conn:
        with db.transaction(conn):
            conn.execute(
                """INSERT INTO run (
                    id, session_id, task_id, sequence, is_resume, state, project, task_type,
                    repository_path, prompt, timeout_seconds, cancel_requested, version,
                    created_at, updated_at
                ) VALUES (
                    'legacy-run-1', :session_id, :task_id, 1, 0, 'COMPLETED', 'AIOS', 'implementation',
                    '/tmp/x', 'p', NULL, 0, 0, :now, :now
                )""",
                {"session_id": session["id"], "task_id": task["id"], "now": now},
            )
    run = db.get_run(path, "legacy-run-1")
    assert run is not None
    assert run["expected_branch"] is None
    assert run["launch_source"] is None
    assert run["prompt_version"] is None
    assert run["commit_hash"] is None
    assert run["pull_request_url"] is None
