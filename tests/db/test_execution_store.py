"""Slice 10: `task` and `session` — and the first mirrored cascade.

The shared contract already covers this family's shape (columns, key,
references, round trip, FK refusal, timestamps). What is specific here is the
delete: `delete_task` removes one row and lets `ON DELETE CASCADE` take
everything hanging off it, and the mirror must behave the same way for the same
reason rather than by coincidence.
"""

from __future__ import annotations

from pathlib import Path

from command_center.db.execution_store import (
    PostgresSessionMirror,
    PostgresTaskMirror,
    session_divergence,
    task_divergence,
)
from command_center.runtime.db import execution as exec_db
from tests.db.mirror_probe import each_lost_write_is_noticed


def _patch(monkeypatch, factory) -> None:
    from command_center.db import execution_store

    # Real classes captured before the patch; reading them back through the
    # module inside the lambda would resolve to the lambda itself.
    for name, mirror in (
        ("PostgresTaskMirror", PostgresTaskMirror),
        ("PostgresSessionMirror", PostgresSessionMirror),
    ):
        monkeypatch.setattr(
            execution_store, name, lambda mirror=mirror: mirror(connection_factory=factory)
        )


def _open_task(db_path: Path) -> tuple[dict, dict]:
    task = exec_db.create_task(db_path, project="AICC", title="mirror me", task_type="feature")
    session = exec_db.create_session(
        db_path, task_id=task["id"], project="AICC", repository_path="/tmp/repo"
    )
    return task, session


def test_the_family_reconciles_after_every_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    _patch(monkeypatch, pg_connection_factory)
    tasks = PostgresTaskMirror(connection_factory=pg_connection_factory)
    sessions = PostgresSessionMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)

    def reconciled(stage: str) -> None:
        assert task_divergence(exec_db.list_tasks(db_path), tasks) == [], stage
        assert session_divergence(exec_db.list_sessions(db_path), sessions) == [], stage

    task = exec_db.create_task(db_path, project="AICC", title="mirror me", task_type="feature")
    reconciled("task created")
    exec_db.create_session(
        db_path, task_id=task["id"], project="AICC", repository_path="/tmp/repo"
    )
    reconciled("session created")


def test_deleting_a_task_cascades_in_the_mirror_too(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """The claim that looks obvious and is not.

    `delete_task` removes one row and relies on `ON DELETE CASCADE` — the
    authority sets `PRAGMA foreign_keys=ON` per connection so that it works.
    The mirror deletes the same single row and relies on the target's declared
    cascade. That symmetry holds only while *both* sides declare it, and the
    mirror can neither see nor enforce the other side's, so it is asserted
    against a real database rather than assumed: the session must be gone from
    the mirror without anything having deleted it directly.
    """
    _patch(monkeypatch, pg_connection_factory)
    tasks = PostgresTaskMirror(connection_factory=pg_connection_factory)
    sessions = PostgresSessionMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    task, session = _open_task(db_path)

    assert [row["id"] for row in sessions.list_records()] == [session["id"]]  # the premise

    assert exec_db.delete_task(db_path, task["id"]) is True

    assert tasks.list_records() == []
    assert sessions.list_records() == [], "the target's cascade did not fire"
    # And the authority agrees, which is what makes the two symmetric rather
    # than merely both empty.
    assert exec_db.get_task(db_path, task["id"]) is None
    assert exec_db.list_sessions(db_path) == []


def test_a_delete_that_removed_nothing_is_not_mirrored(tmp_path, monkeypatch) -> None:
    """`delete_task` returns False for an unknown id, and a mirror call there
    would be a no-op in effect but a lie in the log — and on a busy system, a
    stream of them."""
    from command_center.db import execution_store

    deletions: list[str] = []

    class Recording:
        def upsert(self, record: dict) -> None:
            pass

        def delete_task(self, task_id: str) -> None:
            deletions.append(task_id)

    monkeypatch.setattr(execution_store, "PostgresTaskMirror", lambda: Recording())
    monkeypatch.setattr(execution_store, "PostgresSessionMirror", lambda: Recording())

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)

    assert exec_db.delete_task(db_path, "never-existed") is False
    assert deletions == []

    task = exec_db.create_task(db_path, project="AICC", title="t", task_type="feature")
    assert exec_db.delete_task(db_path, task["id"]) is True
    assert deletions == [task["id"]]


def test_every_lost_mirror_write_is_visible_to_reconciliation(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    from command_center.db import execution_store

    tasks = PostgresTaskMirror(connection_factory=pg_connection_factory)
    sessions = PostgresSessionMirror(connection_factory=pg_connection_factory)
    state: dict[str, Path] = {}

    def scenario() -> None:
        for table in ("session", "task"):
            with pg_connection_factory() as conn:
                with conn.cursor() as cur:
                    cur.execute(f"DELETE FROM {table}")
        db_path = tmp_path / f"runtime-{len(state)}.db"
        exec_db.db.migrate(db_path)
        state["db"] = db_path
        _open_task(db_path)

    def noticed() -> bool:
        db_path = state["db"]
        return bool(
            task_divergence(exec_db.list_tasks(db_path), tasks)
            or session_divergence(exec_db.list_sessions(db_path), sessions)
        )

    results = each_lost_write_is_noticed(
        monkeypatch,
        targets=(
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

    assert [result.target for result in results] == [
        "PostgresTaskMirror",
        "PostgresSessionMirror",
    ]
    assert all(result.noticed for result in results), results


def test_a_mirror_failure_cannot_break_the_authoritative_write(tmp_path, monkeypatch) -> None:
    from command_center.db import execution_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

        def delete_task(self, task_id: str) -> None:
            raise RuntimeError("postgres is down")

    monkeypatch.setattr(execution_store, "PostgresTaskMirror", lambda: Exploding())
    monkeypatch.setattr(execution_store, "PostgresSessionMirror", lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    task, _session = _open_task(db_path)

    assert exec_db.get_task(db_path, task["id"])["title"] == "mirror me"
    assert len(exec_db.list_sessions(db_path)) == 1
    assert exec_db.delete_task(db_path, task["id"]) is True
    assert exec_db.get_task(db_path, task["id"]) is None
