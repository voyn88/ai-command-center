"""Contract tests for the new-engine model skeletons (``command_center.api.models``).

The test pins the *shape*: the still-skeletal entities construct with defaults,
and the constrained fields (``Proposal.kind``, ``ModelEntry.kind``) reject
out-of-contract values — so a later increment cannot silently drift the contract
the shells were built against.

The three Wave-1 routed entities (``Proposal``, ``OwnerItem``, ``DigestItem``)
are now response models on a live surface, so their identity/routing fields
(``id``; ``Proposal.project_ref``) are required rather than defaulted — a
response can never carry an empty-string id.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from command_center.api import models


def test_skeleton_entities_construct_with_defaults() -> None:
    # Entities still in the contract-only phase construct with bare defaults.
    for name in (
        "Motion", "Vote", "Decision",
        "Incident",
    ):
        cls = getattr(models, name)
        assert cls().model_dump() is not None


def test_routed_entities_require_identity_fields() -> None:
    assert models.Proposal(id="p1", project_ref="AICC").id == "p1"
    assert models.OwnerItem(id="o1").id == "o1"
    assert models.DigestItem(id="d1").id == "d1"
    for bad in (
        lambda: models.Proposal(),  # missing id + project_ref
        lambda: models.Proposal(id="p"),  # missing project_ref
        lambda: models.OwnerItem(),  # missing id
        lambda: models.DigestItem(),  # missing id
    ):
        with pytest.raises(ValidationError):
            bad()


def test_audit_entities_require_identity_and_triage_fields() -> None:
    # AuditRun/AuditFinding are Wave-2 routed entities: a response never carries
    # an empty id, and a finding always names its run, category and owner.
    run = models.AuditRun(id="r1", project_ref="AICC")
    assert run.id == "r1" and run.status == "queued"
    finding = models.AuditFinding(
        id="f1", run_id="r1", category="lint", owner="engineering"
    )
    assert finding.status == "open" and finding.owner == "engineering"
    for bad in (
        lambda: models.AuditRun(),  # missing id + project_ref
        lambda: models.AuditRun(id="r"),  # missing project_ref
        lambda: models.AuditFinding(),  # missing id/run_id/category/owner
        lambda: models.AuditFinding(id="f", run_id="r", category="lint"),  # missing owner
        lambda: models.AuditFinding(
            id="f", run_id="r", category="bogus", owner="x"
        ),  # category not in contract
    ):
        with pytest.raises(ValidationError):
            bad()


def test_proposal_kind_is_constrained() -> None:
    for kind in ("trend", "ux", "optimization", "competitor", "feedback"):
        assert models.Proposal(id="p", project_ref="AICC", kind=kind).kind == kind
    with pytest.raises(ValidationError):
        models.Proposal(id="p", project_ref="AICC", kind="not-a-kind")


def test_model_entry_is_routed_and_kind_is_constrained() -> None:
    # ModelEntry is a Wave-3 routed entity: a response never carries an empty id.
    entry = models.ModelEntry(id="m1", kind="local")
    assert entry.id == "m1" and entry.kind == "local" and entry.status == "available"
    assert models.ModelEntry(id="m2", kind="external").kind == "external"
    for bad in (
        lambda: models.ModelEntry(),  # missing id
        lambda: models.ModelEntry(id="m", kind="cloud"),  # kind not in contract
        lambda: models.ModelEntry(id="m", status="offline"),  # status not in contract
    ):
        with pytest.raises(ValidationError):
            bad()


def test_proposal_carries_advisor_triage_fields() -> None:
    proposal = models.Proposal(
        id="p", kind="trend", title="Adopt X", body="…", expected_gain="high",
        effort="low", project_ref="AICC",
    )
    assert proposal.expected_gain == "high"
    assert proposal.effort == "low"
    assert proposal.project_ref == "AICC"
