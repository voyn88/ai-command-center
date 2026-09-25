"""Repository-tier tests for the VOYN-MIN-WOW-1 proof package
(``command_center.runtime.db.proof_package``).

Hermetic: each test migrates a brand-new SQLite file under ``tmp_path`` and
drives the council/proposal repository functions directly to build the
fixtures the aggregator reads — no service, no HTTP, no shared state.

Fixtures use only generic project codes (``AICC``, ``OTHER``) and invented
ids.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.runtime import autonomy as A
from command_center.runtime import db


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "runtime.db"
    db.migrate(path)
    return path


def _proposal(db_path: Path, **overrides) -> dict:
    kwargs = dict(
        kind=A.ProposalKind.TASK_CREATION,
        project="AICC",
        title="Add tests",
        rationale="Coverage gap detected",
        state=A.ProposalState.DRAFT,
        risk_level=A.RiskLevel.LOW,
    )
    kwargs.update(overrides)
    return db.create_proposal(db_path, **kwargs)


# --- digital memory ---------------------------------------------------------


def test_build_digital_memory_merges_council_and_proposal_events(db_path: Path) -> None:
    motion = db.create_motion(
        db_path, title="Adopt X", proposed_by="chair", project_ref="AICC"
    )
    proposal = _proposal(db_path)
    db.append_proposal_event(
        db_path, proposal["id"], "manual_note", actor="ops", message="looked into it"
    )

    memory = db.build_digital_memory(db_path, project="AICC")

    sources = {e["source"] for e in memory}
    assert sources == {"council", "proposal"}
    assert any(e["ref_id"] == motion["id"] and e["event_type"] == "motion_opened" for e in memory)
    assert any(e["ref_id"] == proposal["id"] and e["event_type"] == "manual_note" for e in memory)
    # oldest first
    assert memory == sorted(memory, key=lambda e: e["created_at"])


def test_build_digital_memory_scopes_to_project(db_path: Path) -> None:
    db.create_motion(db_path, title="in", proposed_by="chair", project_ref="AICC")
    db.create_motion(db_path, title="out", proposed_by="chair", project_ref="OTHER")
    _proposal(db_path, project="AICC", title="in")
    _proposal(db_path, project="OTHER", title="out")

    memory = db.build_digital_memory(db_path, project="AICC")

    assert all(e["ref_title"] == "in" for e in memory if e["source"] == "council")
    assert all(e["ref_title"] == "in" for e in memory if e["source"] == "proposal")


# --- counterfactual ----------------------------------------------------------


def test_build_counterfactual_captures_withdrawn_motion(db_path: Path) -> None:
    motion = db.create_motion(
        db_path, title="Risky bet", proposed_by="chair", project_ref="AICC"
    )
    db.withdraw_motion(db_path, motion["id"], expected_version=0)

    entries = db.build_counterfactual(db_path, project="AICC")

    assert len(entries) == 1
    assert entries[0]["kind"] == "withdrawn_motion"
    assert entries[0]["ref_id"] == motion["id"]


def test_build_counterfactual_captures_rejected_decision(db_path: Path) -> None:
    motion = db.create_motion(
        db_path, title="Vendor swap", proposed_by="chair", project_ref="AICC"
    )
    db.record_decision(
        db_path,
        motion_id=motion["id"],
        expected_version=0,
        outcome="rejected",
        tally={"yes": 1, "no": 2, "abstain": 0},
        roles=[{"voter_id": "a", "voter_kind": "human", "role": "member", "choice": "no"}],
        rationale="too risky",
        quorum=1,
    )

    entries = db.build_counterfactual(db_path, project="AICC")

    assert len(entries) == 1
    assert entries[0]["kind"] == "rejected_decision"
    assert entries[0]["ref_id"] == motion["id"]
    assert entries[0]["rationale"] == "too risky"


def test_build_counterfactual_excludes_approved_decisions(db_path: Path) -> None:
    motion = db.create_motion(
        db_path, title="Good idea", proposed_by="chair", project_ref="AICC"
    )
    db.record_decision(
        db_path,
        motion_id=motion["id"],
        expected_version=0,
        outcome="approved",
        tally={"yes": 2, "no": 0, "abstain": 0},
        roles=[],
        rationale="clear win",
        quorum=1,
    )

    assert db.build_counterfactual(db_path, project="AICC") == []


# --- decision P&L --------------------------------------------------------


def test_build_decision_pnl_only_includes_decisions_with_impact(db_path: Path) -> None:
    with_impact = db.create_motion(
        db_path, title="Automate X", proposed_by="chair", project_ref="AICC"
    )
    db.record_decision(
        db_path,
        motion_id=with_impact["id"],
        expected_version=0,
        outcome="approved",
        tally={"yes": 1, "no": 0, "abstain": 0},
        roles=[],
        rationale="pays for itself",
        quorum=1,
        impact={"amount_usd": 12000, "kind": "cost_avoided"},
    )
    without_impact = db.create_motion(
        db_path, title="Rename a doc", proposed_by="chair", project_ref="AICC"
    )
    db.record_decision(
        db_path,
        motion_id=without_impact["id"],
        expected_version=0,
        outcome="approved",
        tally={"yes": 1, "no": 0, "abstain": 0},
        roles=[],
        rationale="trivial",
        quorum=1,
    )

    pnl = db.build_decision_pnl(db_path, project="AICC")

    assert len(pnl) == 1
    assert pnl[0]["ref_id"] == with_impact["id"]
    assert pnl[0]["impact"] == {"amount_usd": 12000, "kind": "cost_avoided"}


# --- audit vault -----------------------------------------------------------


def test_build_audit_vault_lists_proposal_evidence(db_path: Path) -> None:
    proposal = _proposal(db_path)
    db.append_proposal_evidence(
        db_path,
        proposal["id"],
        kind="test_run",
        source="ci",
        summary="all green",
        observed_at=db.iso_now(),
    )

    vault = db.build_audit_vault(db_path, project="AICC")

    assert len(vault) == 1
    assert vault[0]["proposal_id"] == proposal["id"]
    assert vault[0]["kind"] == "test_run"
    assert vault[0]["source"] == "ci"


def test_build_audit_vault_scopes_to_project(db_path: Path) -> None:
    in_scope = _proposal(db_path, project="AICC")
    out_of_scope = _proposal(db_path, project="OTHER")
    db.append_proposal_evidence(
        db_path, in_scope["id"], kind="k", source="s", observed_at=db.iso_now()
    )
    db.append_proposal_evidence(
        db_path, out_of_scope["id"], kind="k", source="s", observed_at=db.iso_now()
    )

    vault = db.build_audit_vault(db_path, project="AICC")

    assert len(vault) == 1
    assert vault[0]["proposal_id"] == in_scope["id"]


# --- the package -------------------------------------------------------------


def test_build_proof_package_assembles_all_four_pillars(db_path: Path) -> None:
    motion = db.create_motion(db_path, title="X", proposed_by="chair", project_ref="AICC")
    db.record_decision(
        db_path,
        motion_id=motion["id"],
        expected_version=0,
        outcome="approved",
        tally={"yes": 1, "no": 0, "abstain": 0},
        roles=[],
        rationale="ok",
        quorum=1,
        impact={"amount_usd": 500},
    )
    proposal = _proposal(db_path)
    db.append_proposal_evidence(
        db_path, proposal["id"], kind="k", source="s", observed_at=db.iso_now()
    )

    package = db.build_proof_package(db_path, project="AICC")

    assert package["project"] == "AICC"
    assert package["generated_at"]
    assert len(package["digital_memory"]) >= 1
    assert len(package["decision_pnl"]) == 1
    assert len(package["audit_vault"]) == 1
    assert isinstance(package["integrity_hash"], str) and len(package["integrity_hash"]) == 64


def test_build_proof_package_integrity_hash_changes_with_content(db_path: Path) -> None:
    db.create_motion(db_path, title="X", proposed_by="chair", project_ref="AICC")
    first = db.build_proof_package(db_path, project="AICC")

    db.create_motion(db_path, title="Y", proposed_by="chair", project_ref="AICC")
    second = db.build_proof_package(db_path, project="AICC")

    assert first["integrity_hash"] != second["integrity_hash"]


def test_build_proof_package_empty_project_is_a_valid_empty_package(db_path: Path) -> None:
    package = db.build_proof_package(db_path, project="NOBODY-HOME")

    assert package["digital_memory"] == []
    assert package["counterfactual"] == []
    assert package["decision_pnl"] == []
    assert package["audit_vault"] == []
    assert package["integrity_hash"]
