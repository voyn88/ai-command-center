"""Slice 15: the proposal family against its real writers.

Third and last suite written to close the same gap slices 13 and 14 had: three
mirrors and eleven dual-write call sites, and no test that ran a single one of
them. The shared contract enrols the mirror *classes*; whether the authority
calls them is a different question, and only this kind of test asks it.

The family is the widest in the schema and the only one with a hook that writes
a parent and its children in one transaction (`create_proposal_atomic`,
`apply_assessment_atomic`, `transition_proposal_atomic`). Order is not
incidental there: the target refuses a child whose proposal is not mirrored
yet, so a parent-last hook loses every child silently.

Reconciled after every write, for the reason established in slice 5: `proposal`
is a mutable row rewritten whole on each transition, so a dropped earlier write
is repaired by a later one and a final-state check reports clean.
"""

from __future__ import annotations

from pathlib import Path

from command_center.db.proposal_store import (
    PostgresProposalEventMirror,
    PostgresProposalEvidenceMirror,
    PostgresProposalMirror,
    proposal_divergence,
    proposal_event_divergence,
    proposal_evidence_divergence,
)
from command_center.runtime.db import proposal as proposal_db

SAMPLE_AT = "2026-08-14T00:00:00"


def _patch(monkeypatch, factory) -> None:
    """Each class captured before the patch lands, never looked up after it.

    A bare `lambda: proposal_store.PostgresProposalMirror(...)` resolves the
    name *after* `setattr`, so it returns the patched factory and recurses.
    This task has walked into that twice; the default argument is the fix.
    """
    from command_center.db import proposal_store

    for name, mirror in (
        ("PostgresProposalMirror", PostgresProposalMirror),
        ("PostgresProposalEventMirror", PostgresProposalEventMirror),
        ("PostgresProposalEvidenceMirror", PostgresProposalEvidenceMirror),
    ):
        monkeypatch.setattr(
            proposal_store, name, lambda mirror=mirror: mirror(connection_factory=factory)
        )


def _create(db_path: Path, **overrides) -> dict:
    fields = {
        "kind": "code_change",
        "project": "AICC",
        "title": "t",
        "rationale": "because the mirror must be exercised",
        "state": "DRAFT",
        "risk_level": "low",
    }
    fields.update(overrides)
    return proposal_db.create_proposal(db_path, **fields)


def test_the_proposal_family_reconciles_after_every_write(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """Two proposals, not one. A hook that only mirrors the proposal it was
    just called against would still pass a single-proposal check — the second
    proposal's rows are the only thing that can catch that, which is why every
    stage reconciles both proposals' children against the complete table
    rather than one proposal's slice of it."""
    _patch(monkeypatch, pg_connection_factory)
    proposals = PostgresProposalMirror(connection_factory=pg_connection_factory)
    events = PostgresProposalEventMirror(connection_factory=pg_connection_factory)
    evidence = PostgresProposalEvidenceMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    proposal_db.db.migrate(db_path)
    other = _create(db_path, title="other")
    proposal_db.append_proposal_evidence(
        db_path,
        other["id"],
        kind="observation",
        source="ci",
        summary="the other proposal's own evidence",
        observed_at=SAMPLE_AT,
    )
    proposal_db.append_proposal_event(db_path, other["id"], "noted", message="the other proposal's own event")

    def reconciled(stage: str, proposal_id: str) -> None:
        assert proposal_divergence(proposal_db.list_proposals(db_path), proposals) == [], stage
        stored_evidence = [
            *proposal_db.list_proposal_evidence_stored(db_path, proposal_id),
            *proposal_db.list_proposal_evidence_stored(db_path, other["id"]),
        ]
        assert proposal_evidence_divergence(stored_evidence, evidence) == [], stage
        stored_events = [
            *proposal_db.list_proposal_events_stored(db_path, proposal_id),
            *proposal_db.list_proposal_events_stored(db_path, other["id"]),
        ]
        assert proposal_event_divergence(stored_events, events) == [], stage

    created = _create(db_path)
    reconciled("proposal created", created["id"])

    proposal_db.append_proposal_evidence(
        db_path,
        created["id"],
        kind="observation",
        source="ci",
        summary="one failing job",
        observed_at=SAMPLE_AT,
        data={"b": 1, "a": 2},
    )
    reconciled("evidence appended", created["id"])

    proposal_db.append_proposal_event(
        db_path, created["id"], "noted", message="an ordinary journal line"
    )
    reconciled("event appended", created["id"])

    # The whole-row rewrite that would repair anything dropped above.
    proposal_db.update_proposal(
        db_path,
        created["id"],
        expected_version=proposal_db.get_proposal(db_path, created["id"])["version"],
        fields={"title": "t2"},
    )
    reconciled("proposal updated", created["id"])

    # The two atomic lifecycle paths. They were the four call sites the first
    # version of this test left unmeasured — a sweep found them by dropping
    # each hook in turn, not by reading the module.
    proposal_db.transition_proposal_atomic(
        db_path,
        created["id"],
        expected_version=proposal_db.get_proposal(db_path, created["id"])["version"],
        new_state="PROPOSED",
        event={"event_type": "proposed", "to_state": "PROPOSED", "message": "raised"},
    )
    reconciled("transitioned atomically", created["id"])

    proposal_db.apply_assessment_atomic(
        db_path,
        created["id"],
        expected_version=proposal_db.get_proposal(db_path, created["id"])["version"],
        verdict_fields={"eligibility_json": '{"eligible": true}'},
        assessed_event={"message": "assessed", "to_state": "PROPOSED"},
        transitions=[
            {
                "new_state": "ELIGIBLE",
                "event": {"event_type": "eligible", "to_state": "ELIGIBLE"},
            }
        ],
    )
    reconciled("assessment applied atomically", created["id"])


def test_the_atomic_path_mirrors_the_parent_before_its_children(
    pg_connection_factory, tmp_path, monkeypatch
) -> None:
    """One transaction, several rows, and an order the target enforces.

    `create_proposal_atomic` writes the proposal, its evidence and its opening
    event together. The mirror has to follow the same order — PostgreSQL
    refuses a `proposal_evidence` row whose `proposal_id` is absent — and a
    hook that mirrored children first would lose them all without raising,
    because the dual-write swallows. Reconciling the children is what makes
    that visible: they can only be there if the parent went first.
    """
    _patch(monkeypatch, pg_connection_factory)
    proposals = PostgresProposalMirror(connection_factory=pg_connection_factory)
    events = PostgresProposalEventMirror(connection_factory=pg_connection_factory)
    evidence = PostgresProposalEvidenceMirror(connection_factory=pg_connection_factory)

    db_path = tmp_path / "runtime.db"
    proposal_db.db.migrate(db_path)

    stored = proposal_db.create_proposal_atomic(
        db_path,
        kind="code_change",
        project="AICC",
        title="atomic",
        rationale="parent and children in one transaction",
        state="DRAFT",
        risk_level="low",
        evidence=[
            {
                "kind": "observation",
                "source": "ci",
                "summary": "first",
                "observed_at": SAMPLE_AT,
            }
        ],
        created_event={"event_type": "created", "message": "opened"},
    )

    assert proposal_divergence([stored], proposals) == []
    assert (
        proposal_evidence_divergence(
            proposal_db.list_proposal_evidence_stored(db_path, stored["id"]), evidence
        )
        == []
    )
    assert (
        proposal_event_divergence(
            proposal_db.list_proposal_events_stored(db_path, stored["id"]), events
        )
        == []
    )
    # Not vacuous: both children exist on the authority side, so an empty
    # mirror would have been reported above rather than silently agreeing.
    assert len(proposal_db.list_proposal_evidence_stored(db_path, stored["id"])) == 1
    assert len(proposal_db.list_proposal_events_stored(db_path, stored["id"])) == 1


def test_a_mirror_failure_cannot_break_a_proposal_write(tmp_path, monkeypatch) -> None:
    from command_center.db import proposal_store

    class Exploding:
        def upsert(self, record: dict) -> None:
            raise RuntimeError("postgres is down")

    for name in (
        "PostgresProposalMirror",
        "PostgresProposalEventMirror",
        "PostgresProposalEvidenceMirror",
    ):
        monkeypatch.setattr(proposal_store, name, lambda: Exploding())

    db_path = tmp_path / "runtime.db"
    proposal_db.db.migrate(db_path)

    created = _create(db_path)
    assert created["state"] == "DRAFT"
    assert (
        proposal_db.append_proposal_evidence(
            db_path,
            created["id"],
            kind="observation",
            source="ci",
            summary="s",
            observed_at=SAMPLE_AT,
        )
        == 1
    )
    assert proposal_db.get_proposal(db_path, created["id"])["title"] == "t"
