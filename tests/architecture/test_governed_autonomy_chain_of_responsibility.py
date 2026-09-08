"""VOYN-MIN-WOW-3: the chain-of-responsibility rule for the 3 critical
autonomy zones (execution, merge/publish, policy authority) named in
`docs/adr/0011-governed-autonomy-chain-of-responsibility.md` must stay true of
the real code, not just of the prose. This is the ADR's own §"Verification"
gate: if any of these assertions goes red, either the code changed underneath
the ADR or the ADR drifted from the code, and the mismatch must be resolved
before either changes further."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center.runtime import autonomy as A
from command_center.runtime import db as runtime_db
from command_center.runtime.autonomy import AutonomyPolicy, ProposalKind, RiskLevel
from command_center.runtime.autonomy_service import AutonomyEngine

REPO_ROOT = Path(__file__).resolve().parents[2]
ADR_PATH = REPO_ROOT / "docs/adr/0011-governed-autonomy-chain-of-responsibility.md"


@pytest.fixture
def engine():
    return AutonomyEngine(runtime_db.resolve_db_path())


def test_adr_names_the_3_critical_zones_and_their_governing_code():
    text = ADR_PATH.read_text(encoding="utf-8")
    for marker in (
        "3 critical zones",
        "ProposalKind.TASK_EXECUTION",
        "ProposalKind.MERGE",
        "AutonomyPolicy.intersect",
    ):
        assert marker in text, f"ADR 0011 must name {marker!r}"


def test_execution_and_merge_are_the_only_kinds_crossing_the_human_gate_line():
    # Zone 1 and zone 2 are exactly the two proposal kinds whose default risk
    # is HIGH or above; everything else stays LOW (metadata-only mutation).
    always_low = {ProposalKind.TASK_CREATION, ProposalKind.PRIORITY_CHANGE, ProposalKind.DEPENDENCY_LINK}
    ev = [A.Evidence(kind="k", source="s", summary="sum", observed_at="2026-01-01T00:00:00")]
    for kind in always_low:
        assert A.classify_risk(kind, ev) == RiskLevel.LOW, kind

    assert A.classify_risk(ProposalKind.TASK_EXECUTION, ev) == RiskLevel.HIGH
    assert A.classify_risk(ProposalKind.MERGE, ev) == RiskLevel.CRITICAL


def test_zone_2_critical_risk_is_never_auto_approvable_under_any_policy():
    # Zone 2's approver step: no policy configuration can turn a human gate
    # into an auto-approval for CRITICAL risk.
    policy = AutonomyPolicy(enabled=True, auto_approve_max_risk=RiskLevel.CRITICAL)
    assert policy.auto_approve_max_risk != RiskLevel.CRITICAL  # clamped down to HIGH on construction
    assert policy.may_auto_approve(RiskLevel.CRITICAL) is False


def test_zone_3_policy_intersection_never_grants_beyond_either_input():
    # Zone 3's rule: a runtime-supplied policy can only restrict the persisted
    # one, never widen it.
    permissive = AutonomyPolicy(
        enabled=True,
        allowed_kinds=A.ALL_KINDS,
        auto_approve_max_risk=RiskLevel.HIGH,
        allow_execution_dispatch=True,
        max_evidence_age_seconds=999_999,
    )
    closed = AutonomyPolicy()  # fully closed default

    effective = permissive.intersect(closed)
    assert effective.enabled is False
    assert effective.allowed_kinds == frozenset()
    assert effective.allow_execution_dispatch is False
    assert effective.auto_approve_max_risk == RiskLevel.NONE

    # Symmetric: intersecting the closed policy in as the "stored" side with a
    # permissive runtime override still yields the closed result.
    also_effective = closed.intersect(permissive)
    assert also_effective.enabled is False
    assert also_effective.allowed_kinds == frozenset()


def test_no_zone_step_proceeds_without_a_named_actor(engine):
    # The actor check runs before the proposal lookup in both dispatch and
    # confirm_execution, so an anonymous caller is refused before the engine
    # even reveals whether the proposal id exists.
    with pytest.raises(ValueError):
        engine.dispatch("does-not-exist", actor="")
    with pytest.raises(ValueError):
        engine.dispatch("does-not-exist", actor="   ")
    with pytest.raises(ValueError):
        engine.confirm_execution("does-not-exist", actor="", run_id="r1")
    with pytest.raises(ValueError):
        engine.approve("does-not-exist", actor="")
