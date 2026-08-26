"""Slice 13: the provenance family against its real writers.

This test exists because it was missing. The slice shipped four mirrors, five
dual-write hooks and no test that runs any of them: the shared contract enrols
the mirror *classes* — columns, key, references, round trip, `jsonb`, identity
— and says nothing about whether the authority ever calls them. A perturbation
sweep proved the gap rather than suspected it: each of the five hooks was
commented out in turn and the whole of `tests/db` stayed green **five times out
of five**. A mirror nothing writes to is indistinguishable from a mirror that
does not exist, and reconciliation is the only thing that would ever say so.

Same shape as every family since slice 5, and for the same reason: reconcile
after *every* write rather than once at the end. A final-state check cannot see
an intermediate write masked by a later whole-row upsert of the same row —
which is exactly what `provider_attempt` does when `finish_provider_attempt`
rewrites the row `start_provider_attempt` wrote.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from command_center.db.execution_store import PostgresSessionMirror, PostgresTaskMirror
from command_center.db.provenance_store import (
    PostgresProvenanceEvidenceMirror,
    PostgresProviderAttemptMirror,
    PostgresRunProvenanceMirror,
    PostgresRunProviderRouteMirror,
    provenance_evidence_divergence,
    provider_attempt_divergence,
    run_provenance_divergence,
    run_provider_route_divergence,
)
from command_center.db.run_store import PostgresRunMirror
from command_center.runtime.db import execution as exec_db
from command_center.runtime.db import provenance as prov_db

SAMPLE_OBSERVED_AT = "2026-08-14T00:00:00"


def _patch(monkeypatch, factory) -> None:
    """Point every mirror in the run's ancestry at the test's connection.

    The parents matter as much as the family under test: `run_provenance`
    references `run`, so an unmirrored run makes every provenance write fail on
    a foreign key — silently, like all mirror failures — and the test would be
    reporting the parent's absence while appearing to test this family.

    Each class is captured as a default argument before the patch lands. A bare
    `lambda: Mirror(...)` reading the module attribute resolves it *after*
    patching and returns the patched factory, recursively; this task has hit
    that trap twice.
    """
    from command_center.db import execution_store, provenance_store, run_store

    for module, name, mirror in (
        (execution_store, "PostgresTaskMirror", PostgresTaskMirror),
        (execution_store, "PostgresSessionMirror", PostgresSessionMirror),
        (run_store, "PostgresRunMirror", PostgresRunMirror),
        (provenance_store, "PostgresRunProvenanceMirror", PostgresRunProvenanceMirror),
        (
            provenance_store,
            "PostgresProvenanceEvidenceMirror",
            PostgresProvenanceEvidenceMirror,
        ),
        (provenance_store, "PostgresProviderAttemptMirror", PostgresProviderAttemptMirror),
        (
            provenance_store,
            "PostgresRunProviderRouteMirror",
            PostgresRunProviderRouteMirror,
        ),
    ):
        monkeypatch.setattr(
            module, name, lambda mirror=mirror: mirror(connection_factory=factory)
        )


def _launch(db_path: Path, **extra) -> dict:
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
        **extra,
    )


def test_the_provenance_family_reconciles_after_every_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    _patch(monkeypatch, pg_connection_factory)
    provenance = PostgresRunProvenanceMirror(connection_factory=pg_connection_factory)
    evidence = PostgresProvenanceEvidenceMirror(connection_factory=pg_connection_factory)
    attempts = PostgresProviderAttemptMirror(connection_factory=pg_connection_factory)

    routes = PostgresRunProviderRouteMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)

    def reconciled(stage: str) -> None:
        stored = prov_db.get_run_provenance(db_path, run["id"])
        assert run_provenance_divergence([stored] if stored else [], provenance) == [], stage
        assert (
            provenance_evidence_divergence(
                prov_db.list_provenance_evidence_stored(db_path), evidence
            )
            == []
        ), stage
        assert (
            provider_attempt_divergence(
                prov_db.list_provider_attempts(db_path, run["id"]), attempts
            )
            == []
        ), stage
        assert (
            run_provider_route_divergence(
                prov_db.list_provider_routes_stored(db_path), routes
            )
            == []
        ), stage

    # Stage zero, and it is the stage acceptance said was missing. `create_run`
    # writes `run_provenance` and `run_provider_route` itself, and the first
    # reconciliation used to run only after `update_run_provenance` — whose
    # whole-row upsert repaired the unmirrored row before anything looked at
    # it. A staged check is only as good as its first stage, and the first
    # write of this family was not in front of one.
    # `provider_route` must be headed by the run's own provider — the authority
    # refuses otherwise, which is how this stage found its own arguments.
    run = _launch(db_path, provider_id="claude", provider_route=["claude", "codex"])
    reconciled("run created — provenance and route written by create_run")

    prov_db.update_run_provenance(db_path, run["id"], fields={"branch": "feat/x"})
    reconciled("provenance updated")

    prov_db.set_run_provenance_once(
        db_path,
        run["id"],
        field="accepted_sha",
        value="a" * 40,
        fields={"accepted_sha": "a" * 40, "accepted_at": SAMPLE_OBSERVED_AT},
    )
    reconciled("immutable fact set once")

    prov_db.create_provenance_evidence(
        db_path,
        run_id=run["id"],
        integrity_id="evidence-1",
        adapter="github",
        status="observed",
        candidate_sha="b" * 40,
        reported_sha="b" * 40,
        native_payload_json='{"b": 1, "a": 2}',
        normalized_json='{"conclusion": "success"}',
        observed_at=SAMPLE_OBSERVED_AT,
    )
    reconciled("evidence recorded")

    prov_db.start_provider_attempt(
        db_path,
        run_id=run["id"],
        attempt_number=1,
        provider_id="claude",
        started_at=SAMPLE_OBSERVED_AT,
    )
    reconciled("attempt started")

    # The write the final-state check cannot see: this rewrites the row the
    # previous stage wrote, so a mirror that dropped the start and caught the
    # finish would reconcile clean at the end.
    prov_db.finish_provider_attempt(
        db_path,
        run_id=run["id"],
        attempt_number=1,
        outcome="succeeded",
        classification="ok",
        disposition="accepted",
        error_code=None,
        completed_at=SAMPLE_OBSERVED_AT,
    )
    reconciled("attempt finished")


def test_a_mirror_failure_cannot_break_a_provenance_write(tmp_path, monkeypatch) -> None:
    """The dual-write is best effort in exactly one direction.

    Provenance is the record of what was accepted and deployed; a PostgreSQL
    outage must not stop the authority recording it. The mirror's failure is
    swallowed, which is the whole reason reconciliation has to exist.
    """
    from command_center.db import execution_store, provenance_store, run_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

        def delete_task(self, task_id: str) -> None:
            raise RuntimeError("postgres is down")

    for module, name in (
        (execution_store, "PostgresTaskMirror"),
        (execution_store, "PostgresSessionMirror"),
        (run_store, "PostgresRunMirror"),
        (provenance_store, "PostgresRunProvenanceMirror"),
        (provenance_store, "PostgresProvenanceEvidenceMirror"),
        (provenance_store, "PostgresProviderAttemptMirror"),
        (provenance_store, "PostgresRunProviderRouteMirror"),
    ):
        monkeypatch.setattr(module, name, lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    run = _launch(db_path)

    updated = prov_db.update_run_provenance(db_path, run["id"], fields={"branch": "feat/x"})
    assert updated["branch"] == "feat/x"
    attempt = prov_db.start_provider_attempt(
        db_path,
        run_id=run["id"],
        attempt_number=1,
        provider_id="claude",
        started_at=SAMPLE_OBSERVED_AT,
    )
    assert attempt["outcome"] == "started"
    assert prov_db.get_run_provenance(db_path, run["id"])["branch"] == "feat/x"


def test_a_schema_without_the_provenance_tables_still_creates_runs(
    tmp_path, monkeypatch
) -> None:
    """The guard the mirror's read-back nearly undid.

    `create_run` writes `run_provenance` and `run_provider_route` only when
    `sqlite_master` says they exist, so that a database migrated only part-way
    — what the historical-schema tests build — can still create runs. Slice 13
    read both rows back for the mirror, and the review found the read-back
    unguarded: on such a database the `SELECT` raised inside the authoritative
    transaction and destroyed the very write the guards above it exist to
    protect.

    It is the worst place in the family for a mirror to raise. Every other
    mirror failure is swallowed by a hook — that is what the test above
    asserts — but this one happens *before* any hook, while the transaction is
    still open, so "best effort" silently became "the run is lost".

    Reproduced by dropping the two tables rather than by building an old
    schema: both live in `0001_initial.up.sql`, so no migration version omits
    them, and the condition the guards anticipate has to be constructed.
    """
    from command_center.db import execution_store, provenance_store, run_store

    class Silent:
        def upsert(self, record: dict) -> None:
            pass

    for module, name in (
        (execution_store, "PostgresTaskMirror"),
        (execution_store, "PostgresSessionMirror"),
        (run_store, "PostgresRunMirror"),
        (provenance_store, "PostgresRunProvenanceMirror"),
        (provenance_store, "PostgresRunProviderRouteMirror"),
    ):
        monkeypatch.setattr(module, name, lambda: Silent())

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE provider_attempt")
        conn.execute("DROP TABLE run_provider_route")
        conn.execute("DROP TABLE provenance_evidence")
        conn.execute("DROP TABLE run_provenance")

    run = _launch(db_path, provider_id="claude", provider_route=["claude"])

    assert exec_db.get_run(db_path, run["id"])["prompt"] == "p"


def test_the_backfill_mirrors_the_rows_it_creates(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """`backfill_run_provenance` is a write path, so it is a mirror path.

    It exists because acceptance deleted the mirroring loop from it and no
    test noticed — the half of a fix that is green by absence. The function is
    not obscure: `migrate()` calls it on every upgrade past schema 13, so a
    database that gains provenance rows this way would hand the mirror a
    permanent deficit that only reconciliation would ever report.

    Driven through the real writer rather than a hand-built row, and the
    premise is asserted before the claim: a run whose provenance row was
    deleted is genuinely absent from both sides first, so the reconciliation
    below cannot pass by comparing nothing with nothing.
    """
    _patch(monkeypatch, pg_connection_factory)
    provenance = PostgresRunProvenanceMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    exec_db.db.migrate(db_path)
    run = _launch(db_path)

    # Remove the row `create_run` wrote, on both sides, so the backfill has
    # something to do and the mirror has nothing to hide behind.
    with exec_db.db.connect(db_path) as conn:
        with exec_db.db.transaction(conn):
            conn.execute("DELETE FROM run_provenance WHERE run_id = ?", (run["id"],))
    provenance.delete_where("run_id", run["id"])
    assert prov_db.get_run_provenance(db_path, run["id"]) is None
    assert provenance.list_records() == []

    inserted = prov_db.backfill_run_provenance(db_path)

    assert inserted == 1, "the backfill had nothing to do, so this proves nothing"
    stored = prov_db.get_run_provenance(db_path, run["id"])
    assert stored is not None
    assert run_provenance_divergence([stored], provenance) == []
