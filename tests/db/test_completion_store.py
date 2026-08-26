"""Slice 14: the completion family against its real writers.

Written for the same reason as the provenance suite one slice earlier: the
slice shipped three mirrors and four dual-write hooks with no test that runs
any of them. The shared contract enrols the mirror *classes* and is silent on
whether the authority ever calls them, so a perturbation sweep — each hook
commented out in turn — left the whole of `tests/db` green.

Reconciled after every write, not once at the end. `completion` is a mutable
current-state row updated by compare-and-set, so a later whole-row upsert
repairs a dropped earlier one and a final-state check would report clean.
"""

from __future__ import annotations

from pathlib import Path

from command_center.db.completion_store import (
    PostgresCompletionEventMirror,
    PostgresCompletionMirror,
    PostgresCompletionValidationMirror,
    completion_divergence,
    completion_event_divergence,
    completion_validation_divergence,
)
from command_center.db.execution_store import PostgresSessionMirror, PostgresTaskMirror
from command_center.db.run_store import PostgresRunMirror
from command_center.runtime.db import completion as completion_db
from command_center.runtime.db import execution as exec_db

SAMPLE_AT = "2026-08-14T00:00:00"


def _patch(monkeypatch, factory) -> None:
    """Every mirror in the ancestry, each class captured before it is patched.

    `completion` references `run`, which references `session` and `task`: an
    unmirrored parent makes every write here fail on a foreign key, silently,
    and the suite would be measuring the parent's absence.
    """
    from command_center.db import completion_store, execution_store, run_store

    for module, name, mirror in (
        (execution_store, "PostgresTaskMirror", PostgresTaskMirror),
        (execution_store, "PostgresSessionMirror", PostgresSessionMirror),
        (run_store, "PostgresRunMirror", PostgresRunMirror),
        (completion_store, "PostgresCompletionMirror", PostgresCompletionMirror),
        (completion_store, "PostgresCompletionEventMirror", PostgresCompletionEventMirror),
        (
            completion_store,
            "PostgresCompletionValidationMirror",
            PostgresCompletionValidationMirror,
        ),
    ):
        monkeypatch.setattr(
            module, name, lambda mirror=mirror: mirror(connection_factory=factory)
        )


def _launch(db_path: Path) -> dict:
    task = exec_db.create_task(db_path, project="AICC", title="t", task_type="feature")
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
        prompt="p",
        is_resume=False,
        command=["claude"],
    )


def test_the_completion_family_reconciles_after_every_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    _patch(monkeypatch, pg_connection_factory)
    completions = PostgresCompletionMirror(connection_factory=pg_connection_factory)
    events = PostgresCompletionEventMirror(connection_factory=pg_connection_factory)
    validations = PostgresCompletionValidationMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    run = _launch(db_path)

    def reconciled(stage: str) -> None:
        stored = completion_db.get_completion(db_path, run["id"])
        assert completion_divergence([stored] if stored else [], completions) == [], stage
        assert (
            completion_event_divergence(
                completion_db.list_completion_events_stored(db_path), events
            )
            == []
        ), stage
        assert (
            completion_validation_divergence(
                completion_db.list_validation_results(db_path, run["id"]), validations
            )
            == []
        ), stage

    created = completion_db.create_completion(
        db_path,
        run_id=run["id"],
        task_id=run["task_id"],
        project="AICC",
        repository_path="/tmp/repo",
        completion_state="EXECUTION_FINISHED",
        branch="feat/x",
    )
    reconciled("completion created")

    completion_db.append_completion_event(
        db_path, run["id"], "validation_started", message="running"
    )
    reconciled("first event")

    completion_db.record_validation_result(
        db_path,
        run["id"],
        attempt=1,
        command="pytest -q",
        exit_code=0,
        started_at=SAMPLE_AT,
        finished_at=SAMPLE_AT,
        stdout_summary="ok",
        stderr_summary=None,
    )
    reconciled("validation recorded")

    # The whole-row update that would repair a dropped create: a mirror that
    # lost the row above and caught this one reconciles clean at the end, which
    # is why the check runs per write.
    completion_db.update_completion(
        db_path,
        run["id"],
        expected_version=created["version"],
        fields={"completion_state": "VALIDATING_RESULT"},
    )
    reconciled("completion updated")

    completion_db.append_completion_event(db_path, run["id"], "validated", message="done")
    reconciled("second event")


def test_a_mirror_failure_cannot_break_a_completion_write(tmp_path, monkeypatch) -> None:
    """Completion is what the pipeline restarts against; PostgreSQL being down
    must not stop the authority recording it. The failure is swallowed, which
    is why reconciliation is the only thing that can report it."""
    from command_center.db import completion_store, execution_store, run_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

        def delete_task(self, task_id: str) -> None:
            raise RuntimeError("postgres is down")

    for module, name in (
        (execution_store, "PostgresTaskMirror"),
        (execution_store, "PostgresSessionMirror"),
        (run_store, "PostgresRunMirror"),
        (completion_store, "PostgresCompletionMirror"),
        (completion_store, "PostgresCompletionEventMirror"),
        (completion_store, "PostgresCompletionValidationMirror"),
    ):
        monkeypatch.setattr(module, name, lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    run = _launch(db_path)

    created = completion_db.create_completion(
        db_path,
        run_id=run["id"],
        task_id=run["task_id"],
        project="AICC",
        repository_path="/tmp/repo",
        completion_state="EXECUTION_FINISHED",
    )
    assert created["completion_state"] == "EXECUTION_FINISHED"
    assert completion_db.append_completion_event(db_path, run["id"], "validation_started") == 1
    assert completion_db.get_completion(db_path, run["id"])["run_id"] == run["id"]
