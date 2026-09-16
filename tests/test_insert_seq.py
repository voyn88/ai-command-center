"""`run.insert_seq` / `completion.insert_seq` — insertion order as a column
(VOYN-W0-AICC-INSERT-SEQ).

The question both tables are asked is "which is the newest row for this task",
and until schema 26 the only thing that could answer it when `created_at` tied
was SQLite's implicit `rowid`. `created_at` ties often, not rarely: it is
`iso_now()`, ISO text at *second* resolution, and an automatic rework relaunches
a task the moment its failure is observed — so several runs per task inside one
second is what the pipeline does on purpose, and a caller that reads the wrong
one acts on a failure that has already been superseded.

Every test here freezes `iso_now`, which is not a trick to force a rare race but
the literal condition the pipeline produces: rows the timestamp cannot separate.
Under that condition `created_at DESC` alone is a coin flip, so the assertions
below are about the tiebreak and nothing else.

Two attractive-looking alternatives are pinned as *disproofs* rather than
described in a comment, because a comment does not fail when someone reaches for
them: `run.sequence` counts within a session and a relaunched task gets a fresh
session, so its runs all hold `sequence = 1`; and the ids are `uuid4().hex`, so
sorting on them sorts on noise.
"""

from __future__ import annotations

import threading

import pytest

from command_center.runtime import db as runtime_db

FROZEN = "2026-01-01T00:00:00"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "runtime.db"
    runtime_db.migrate(path)
    return path


@pytest.fixture
def frozen_clock(monkeypatch):
    """Every row in one test gets the same `created_at`.

    Patched on the package facade because that is what the table modules call
    (`db.iso_now()`, late-bound) — the same seam the rest of the suite uses.
    """
    monkeypatch.setattr(runtime_db, "iso_now", lambda: FROZEN)


def _task_with_session(db_path, *, title="insert-seq"):
    task = runtime_db.create_task(
        db_path, project="AICC", title=title, task_type="implementation"
    )
    session = runtime_db.create_session(
        db_path, task_id=task["id"], project="AICC", repository_path="/tmp/insert-seq"
    )
    return task, session


def _run(db_path, task, session, *, run_id=None):
    return runtime_db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AICC",
        task_type="implementation",
        repository_path="/tmp/insert-seq",
        prompt="p",
        is_resume=False,
        run_id=run_id,
        enforce_workspace_lock=False,
    )


def _completion(db_path, run):
    return runtime_db.create_completion(
        db_path,
        run_id=run["id"],
        task_id=run["task_id"],
        project="AICC",
        repository_path="/tmp/insert-seq",
        completion_state="EXECUTION_FINISHED",
    )


def _migrations_through(version: int) -> list:
    return [migration for migration in runtime_db.MIGRATIONS if migration[0] <= version]


def _explain(db_path, sql: str) -> list:
    with runtime_db.connect(db_path) as conn:
        return conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()


# --------------------------------------------------------------------------
# The column exists and counts
# --------------------------------------------------------------------------


def test_runs_are_stamped_in_insertion_order(db_path, frozen_clock):
    task, session = _task_with_session(db_path)
    runs = [_run(db_path, task, session) for _ in range(5)]
    assert [run["insert_seq"] for run in runs] == [1, 2, 3, 4, 5]


def test_completions_are_stamped_in_insertion_order(db_path, frozen_clock):
    task, session = _task_with_session(db_path)
    completions = [_completion(db_path, _run(db_path, task, session)) for _ in range(5)]
    assert [row["insert_seq"] for row in completions] == [1, 2, 3, 4, 5]


def test_the_stamp_is_global_not_per_task(db_path, frozen_clock):
    """`backfill_run_provenance` orders the *whole* `run` table by
    `(created_at, insert_seq)` to pick each bounded batch, so a per-task counter
    would make the stamp incomparable across tasks and the tiebreak meaningless.
    The per-task read would be satisfied by a per-task counter; the global one
    would not, so the stamp is global."""
    first_task, first_session = _task_with_session(db_path, title="first")
    second_task, second_session = _task_with_session(db_path, title="second")
    stamps = [
        _run(db_path, first_task, first_session)["insert_seq"],
        _run(db_path, second_task, second_session)["insert_seq"],
        _run(db_path, first_task, first_session)["insert_seq"],
    ]
    assert stamps == [1, 2, 3]


def test_the_stamp_has_an_index_leading_on_it(db_path):
    """`MAX(insert_seq)` runs on every insert, inside the write lock. Without an
    index leading on the column it is a full table scan, and the cost grows with
    the run history while the lock is held — so the index is part of the write
    path rather than a read-side nicety.

    Asserted through the planner rather than by reading `sqlite_master`: an
    index that exists and is not used would satisfy the weaker check.
    """
    plan = " ".join(
        row["detail"]
        for row in _explain(db_path, "SELECT COALESCE(MAX(insert_seq), 0) + 1 FROM run")
    )
    assert "idx_run_insert_seq" in plan, plan


def test_the_per_task_read_has_its_own_index(db_path):
    """`(task_id, insert_seq)` — the task is the seek, the stamp is the order, and
    no temp B-tree in between. This is why `get_latest_run_for_task` orders by
    `insert_seq` alone: with `created_at` in front, SQLite falls back to
    `idx_run_task_id` plus a sort over every run the task ever had, and the
    index this migration adds goes unused."""
    plan = " ".join(
        row["detail"]
        for row in _explain(
            db_path,
            "SELECT * FROM run WHERE task_id = 'x' ORDER BY insert_seq DESC LIMIT 1",
        )
    )
    assert "idx_run_task_insert_seq" in plan, plan


# --------------------------------------------------------------------------
# What it is for: the newest-row reads, when `created_at` cannot decide
# --------------------------------------------------------------------------


def test_latest_run_for_task_is_the_last_inserted_when_timestamps_tie(
    db_path, frozen_clock
):
    task, session = _task_with_session(db_path)
    runs = [_run(db_path, task, session) for _ in range(4)]
    assert {run["created_at"] for run in runs} == {FROZEN}

    latest = runtime_db.get_latest_run_for_task(db_path, task["id"])
    assert latest["id"] == runs[-1]["id"]
    assert latest["insert_seq"] == max(run["insert_seq"] for run in runs)


def test_latest_completion_for_task_is_the_last_inserted_when_timestamps_tie(
    db_path, frozen_clock
):
    task, session = _task_with_session(db_path)
    runs = [_run(db_path, task, session) for _ in range(4)]
    for run in runs:
        _completion(db_path, run)

    assert runtime_db.get_completion_by_task(db_path, task["id"])["run_id"] == runs[-1]["id"]


def test_the_answer_does_not_depend_on_the_id_sort_order(db_path, frozen_clock):
    """`new_id()` is `uuid4().hex` and `create_run` accepts a caller-supplied
    id besides, so a text tiebreak would order by noise. Inserted newest-first
    by id on purpose: a `MIN(id)`/`MAX(id)` tiebreak returns the wrong row here,
    `insert_seq` does not."""
    task, session = _task_with_session(db_path)
    first = _run(db_path, task, session, run_id="zzzz-inserted-first")
    second = _run(db_path, task, session, run_id="aaaa-inserted-second")

    assert first["id"] > second["id"]
    assert runtime_db.get_latest_run_for_task(db_path, task["id"])["id"] == second["id"]


def test_run_sequence_cannot_answer_this_question(db_path, frozen_clock):
    """The disproof, pinned. `sequence` is `MAX(sequence) + 1` *within a
    session*, and the supervisor opens a fresh session for every non-resume
    launch — so a task relaunched three times holds three runs at `sequence = 1`
    and ordering by it orders by a constant. `insert_seq` separates the same
    three rows."""
    task = runtime_db.create_task(
        db_path, project="AICC", title="relaunched", task_type="implementation"
    )
    runs = []
    for _ in range(3):
        session = runtime_db.create_session(
            db_path, task_id=task["id"], project="AICC", repository_path="/tmp/insert-seq"
        )
        runs.append(_run(db_path, task, session))

    assert [run["sequence"] for run in runs] == [1, 1, 1]
    assert [run["insert_seq"] for run in runs] == [1, 2, 3]
    assert runtime_db.get_latest_run_for_task(db_path, task["id"])["id"] == runs[-1]["id"]


# --------------------------------------------------------------------------
# Concurrency: the write lock is the guarantee, not the clock
# --------------------------------------------------------------------------


def test_concurrent_creates_never_share_a_stamp(db_path, frozen_clock):
    """`MAX(insert_seq) + 1` is read inside the `BEGIN IMMEDIATE` the insert
    already holds, so two writers serialise on the write lock instead of both
    reading the same maximum — the same shape `run.sequence` and `run_event.seq`
    use. Threads with their own connections, because that is what actually
    contends for the lock; a single connection would prove nothing."""
    task, session = _task_with_session(db_path)
    stamps: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker() -> None:
        start.wait()
        try:
            run = _run(db_path, task, session)
        except BaseException as exc:  # noqa: BLE001 - the test must see every failure
            with lock:
                errors.append(exc)
            return
        with lock:
            stamps.append(run["insert_seq"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert errors == []
    assert sorted(stamps) == list(range(1, 9))


# --------------------------------------------------------------------------
# Migration 26: seeded from `rowid`, so no stored task changes its answer
# --------------------------------------------------------------------------


def test_the_migration_seeds_the_column_from_rowid(tmp_path, monkeypatch):
    """The migration is now the only place in the runtime store that *orders* by
    `rowid`. Rows written before schema 26 were ordered by it, so seeding from
    it is what makes the upgrade invisible: the same question gets the same
    answer on either side of the migration."""
    path = tmp_path / "runtime-v25.db"
    with monkeypatch.context() as historical:
        historical.setattr(runtime_db, "MIGRATIONS", _migrations_through(25))
        historical.setattr(runtime_db, "SCHEMA_VERSION", 25)
        historical.setattr(runtime_db, "iso_now", lambda: FROZEN)
        runtime_db.migrate(path)
        assert runtime_db.current_schema_version(path) == 25

        task, session = _task_with_session(path)
        legacy_runs = [_run(path, task, session) for _ in range(3)]
        for run in legacy_runs:
            _completion(path, run)

        with runtime_db.connect(path) as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(run)")}
            assert "insert_seq" not in columns
            # The pre-26 query, spelled out: `rowid` is the only thing this
            # schema has to break the `created_at` tie with, and it is the
            # answer the upgrade has to keep. (The reader itself is not called
            # here — it names `insert_seq`, and a caller only ever reaches it
            # through `migrate()`.)
            pre_upgrade_latest = conn.execute(
                "SELECT id FROM run WHERE task_id = ? "
                "ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (task["id"],),
            ).fetchone()["id"]

    runtime_db.migrate(path)
    assert runtime_db.current_schema_version(path) == runtime_db.SCHEMA_VERSION

    with runtime_db.connect(path) as conn:
        stamped = {
            row["id"]: row["insert_seq"]
            for row in conn.execute("SELECT id, insert_seq FROM run")
        }
        completion_stamped = [
            row["insert_seq"]
            for row in conn.execute("SELECT insert_seq FROM completion ORDER BY insert_seq")
        ]
    assert [stamped[run["id"]] for run in legacy_runs] == [1, 2, 3]
    assert completion_stamped == [1, 2, 3]

    latest = runtime_db.get_latest_run_for_task(path, task["id"])
    assert latest["id"] == legacy_runs[-1]["id"]
    # Same answer as before the upgrade — the point of seeding rather than
    # backfilling from anything cleverer.
    assert latest["id"] == pre_upgrade_latest
    assert runtime_db.get_completion_by_task(path, task["id"])["run_id"] == (
        legacy_runs[-1]["id"]
    )


def test_a_run_created_after_the_upgrade_continues_the_seeded_sequence(
    tmp_path, monkeypatch
):
    path = tmp_path / "runtime-v25-then-new.db"
    with monkeypatch.context() as historical:
        historical.setattr(runtime_db, "MIGRATIONS", _migrations_through(25))
        historical.setattr(runtime_db, "SCHEMA_VERSION", 25)
        historical.setattr(runtime_db, "iso_now", lambda: FROZEN)
        runtime_db.migrate(path)
        task, session = _task_with_session(path)
        for _ in range(3):
            _completion(path, _run(path, task, session))

    runtime_db.migrate(path)
    monkeypatch.setattr(runtime_db, "iso_now", lambda: FROZEN)

    fresh = _run(path, task, session)
    assert fresh["insert_seq"] == 4
    assert _completion(path, fresh)["insert_seq"] == 4
    assert runtime_db.get_latest_run_for_task(path, task["id"])["id"] == fresh["id"]


def test_the_migration_is_idempotent(db_path):
    """Re-running a fully applied migration 26 must not re-seed a column that
    already carries live values — the check-then-add shape every `ALTER TABLE`
    migration in this file uses."""
    task, session = _task_with_session(db_path)
    runs = [_run(db_path, task, session) for _ in range(3)]
    before = [run["insert_seq"] for run in runs]

    from command_center.runtime.db import schema

    with runtime_db.connect(db_path) as conn:
        schema._migration_26_add_insert_seq(conn)

    with runtime_db.connect(db_path) as conn:
        after = [
            conn.execute(
                "SELECT insert_seq FROM run WHERE id = ?", (run["id"],)
            ).fetchone()["insert_seq"]
            for run in runs
        ]
    assert after == before


# --------------------------------------------------------------------------
# The trap the column walked into
# --------------------------------------------------------------------------


def test_create_completion_still_works_against_a_pre_completion_column_schema(
    tmp_path, monkeypatch
):
    """`create_completion` used to build its `INSERT` from a static literal, so
    adding `insert_seq` to that literal would have made it unusable against
    every older schema — including the deliberately-v5 database
    `tests/test_autonomy_db.py` builds to prove the v5 -> v6 upgrade preserves
    rows. It now intersects the literal with `PRAGMA table_info`, the rule
    `create_run` has followed since the provider columns landed.
    """
    path = tmp_path / "runtime-v5.db"
    with monkeypatch.context() as historical:
        historical.setattr(runtime_db, "MIGRATIONS", _migrations_through(5))
        historical.setattr(runtime_db, "SCHEMA_VERSION", 5)
        runtime_db.migrate(path)
        assert runtime_db.current_schema_version(path) == 5

        task, session = _task_with_session(path)
        run = _run(path, task, session)
        completion = _completion(path, run)

    assert completion["run_id"] == run["id"]
    assert runtime_db.get_completion(path, run["id"])["completion_state"] == (
        "EXECUTION_FINISHED"
    )
