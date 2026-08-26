"""Slice 11: `run` — and the write-side sibling of slice 4's trap.

The shared contract covers this table's shape: 42 columns, three flags, seven
timestamps, two `jsonb`, two foreign keys, all against the accepted schema and
the live SQLite one. What it cannot cover is the *hook*, and that is where this
table's hazard is.
"""

from __future__ import annotations

from pathlib import Path

from command_center.db.execution_store import PostgresSessionMirror, PostgresTaskMirror
from command_center.db.run_store import PostgresRunMirror, run_divergence
from command_center.runtime.db import execution as exec_db
from tests.db.mirror_probe import each_lost_write_is_noticed


def _patch(monkeypatch, factory) -> None:
    from command_center.db import execution_store, run_store

    for module, name, mirror in (
        (run_store, "PostgresRunMirror", PostgresRunMirror),
        (execution_store, "PostgresTaskMirror", PostgresTaskMirror),
        (execution_store, "PostgresSessionMirror", PostgresSessionMirror),
    ):
        monkeypatch.setattr(
            module, name, lambda mirror=mirror: mirror(connection_factory=factory)
        )


def _launch(db_path: Path) -> dict:
    task = exec_db.create_task(db_path, project="AICC", title="run me", task_type="feature")
    session = exec_db.create_session(
        db_path, task_id=task["id"], project="AICC", repository_path="/tmp/repo"
    )
    return exec_db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project="AICC",
        task_type="feature",
        repository_path="/tmp/repo",
        prompt="do the thing",
        is_resume=False,
        command=["claude", "--print"],
    )


def test_the_mirror_receives_the_stored_row_not_the_writers_record(
    tmp_path, monkeypatch
) -> None:
    """The difference, measured — not the version I first wrote.

    `create_run` builds its `INSERT` column list from `PRAGMA table_info(run)`
    intersected with its record, so the record it returns is missing whatever it
    never set. I claimed that mirroring it would send `NULL` into `NOT NULL`
    columns and lose every run silently; measuring says otherwise. The three
    columns that differ — `failure_reason`, `first_output_at`, `pre_run_head` —
    are all nullable, so mirroring the record would work *today*.

    The hook still takes the stored row, for the reason that survives the
    measurement: what the record omits depends on the caller's optional
    arguments and on that database's columns. This test therefore asserts the
    measured difference rather than a hazard, so it fails when the shapes
    converge or diverge further — either of which changes what the hook must do.
    """
    from command_center.db import execution_store, run_store

    seen: list[dict] = []

    class Recording:
        def upsert(self, record: dict) -> None:
            seen.append(dict(record))

    monkeypatch.setattr(run_store, "PostgresRunMirror", lambda: Recording())
    monkeypatch.setattr(execution_store, "PostgresTaskMirror", lambda: Recording())
    monkeypatch.setattr(execution_store, "PostgresSessionMirror", lambda: Recording())

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    returned = _launch(db_path)

    mirrored = seen[-1]
    assert set(mirrored) - set(returned) == {
        "failure_reason",
        "first_output_at",
        "pre_run_head",
        # Necessarily absent from the record: it is written at the *end* of
        # finalization, and this assertion is about the row `create_run` stores.
        "finalized_at",
    }
    assert set(returned) - set(mirrored) == set()
    assert len(returned) == 38 and len(mirrored) == 42
    assert mirrored["id"] == returned["id"]


def test_runs_reconcile_after_every_write(pg_connection_factory, tmp_path, monkeypatch) -> None:
    _patch(monkeypatch, pg_connection_factory)
    runs = PostgresRunMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)

    def reconciled(stage: str) -> None:
        assert run_divergence(exec_db.list_runs(db_path), runs) == [], stage

    run = _launch(db_path)
    reconciled("run created")

    queued = exec_db.update_run_state(
        db_path, run["id"], expected_version=run["version"], new_state="QUEUED"
    )
    reconciled("run queued")

    updated = exec_db.update_run_state(
        db_path, run["id"], expected_version=queued["version"], new_state="RUNNING"
    )
    reconciled("run running")

    exec_db.update_run_fields(
        db_path,
        run["id"],
        expected_version=updated["version"],
        fields={"pid": 4242, "started_at": exec_db.db.iso_now()},
    )
    reconciled("run started")


def test_the_flags_and_json_columns_round_trip(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """Three `INTEGER 0/1` flags and two `jsonb` columns in one row — every
    conversion class this table carries, exercised through the real writer
    rather than a fixture's idea of a run."""
    _patch(monkeypatch, pg_connection_factory)
    runs = PostgresRunMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    run = _launch(db_path)

    exec_db.update_run_fields(
        db_path,
        run["id"],
        expected_version=run["version"],
        fields={"cancel_requested": 1, "working_tree_changed": 1, "exit_code": 0},
    )

    stored = runs.list_records()[0]
    assert stored["cancel_requested"] == 1
    assert stored["working_tree_changed"] == 1
    assert stored["is_resume"] == 0
    assert run_divergence(exec_db.list_runs(db_path), runs) == []


def test_every_lost_mirror_write_is_visible_to_reconciliation(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    from command_center.db import execution_store, run_store

    runs = PostgresRunMirror(connection_factory=pg_connection_factory)
    state: dict[str, Path] = {}

    def scenario() -> None:
        for table in ("run", "session", "task"):
            with pg_connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {table}")
        db_path = tmp_path / f"runtime-{len(state)}.db"
        exec_db.db.migrate(db_path)
        state["db"] = db_path
        run = _launch(db_path)
        exec_db.update_run_state(
            db_path, run["id"], expected_version=run["version"], new_state="QUEUED"
        )

    def noticed() -> bool:
        return bool(run_divergence(exec_db.list_runs(state["db"]), runs))

    results = each_lost_write_is_noticed(
        monkeypatch,
        targets=(
            (
                run_store,
                ("PostgresRunMirror",),
                lambda: PostgresRunMirror(connection_factory=pg_connection_factory),
            ),
            # The classes come from the module-level import, captured before
            # any patch. Reading them back through `execution_store` inside the
            # lambda would resolve to the patched attribute — the lambda
            # itself — and the mirror would raise into a hook that swallows.
            # That is the third time this shape has appeared in this migration.
            (
                execution_store,
                ("PostgresTaskMirror",),
                lambda: PostgresTaskMirror(connection_factory=pg_connection_factory),
            ),
            (
                execution_store,
                ("PostgresSessionMirror",),
                lambda: PostgresSessionMirror(connection_factory=pg_connection_factory),
            ),
        ),
        scenario=scenario,
        noticed=noticed,
    )

    # Four writes: task, session, run, and the state change. Losing the task or
    # the session is visible through the run too, because the target refuses a
    # run whose parents are absent.
    assert [result.target for result in results] == [
        "PostgresTaskMirror",
        "PostgresSessionMirror",
        "PostgresRunMirror",
        "PostgresRunMirror",
    ]
    assert all(result.noticed for result in results), results


def test_a_mirror_failure_cannot_break_the_authoritative_write(tmp_path, monkeypatch) -> None:
    from command_center.db import execution_store, run_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

        def delete_task(self, task_id: str) -> None:
            raise RuntimeError("postgres is down")

    monkeypatch.setattr(run_store, "PostgresRunMirror", lambda: Exploding())
    monkeypatch.setattr(execution_store, "PostgresTaskMirror", lambda: Exploding())
    monkeypatch.setattr(execution_store, "PostgresSessionMirror", lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    run = _launch(db_path)

    assert exec_db.get_run(db_path, run["id"])["prompt"] == "do the thing"
    exec_db.update_run_state(db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
    assert exec_db.get_run(db_path, run["id"])["state"] == "QUEUED"
