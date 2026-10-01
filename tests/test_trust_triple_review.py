"""Unit tests for the triple-council critical-decision review
(``command_center.trust.triple_review``).

Pure domain module — no db, no fixtures beyond hand-built verdicts.
"""

from __future__ import annotations

import pytest

from command_center.trust import (
    ConsensusResult,
    DuplicateRoleError,
    IncompleteReviewError,
    MissingExplanationError,
    ROLES,
    RoleVerdict,
    evaluate,
)


def _verdict(role: str, verdict: str, explanation: str = "because reasons") -> RoleVerdict:
    return RoleVerdict(role=role, verdict=verdict, explanation=explanation)


def test_roles_are_exactly_executor_audit_stress() -> None:
    assert ROLES == ("executor", "audit", "stress")


def test_verdict_requires_a_non_empty_explanation() -> None:
    with pytest.raises(MissingExplanationError):
        RoleVerdict(role="executor", verdict="approve", explanation="")
    with pytest.raises(MissingExplanationError):
        RoleVerdict(role="executor", verdict="approve", explanation="   ")


def test_verdict_rejects_unknown_role_or_choice() -> None:
    with pytest.raises(ValueError):
        RoleVerdict(role="chair", verdict="approve", explanation="x")
    with pytest.raises(ValueError):
        RoleVerdict(role="executor", verdict="maybe", explanation="x")


def test_unanimous_approval_is_approved() -> None:
    result = evaluate(
        [
            _verdict("executor", "approve", "the plan is sound and reversible"),
            _verdict("audit", "approve", "consistent with policy"),
            _verdict("stress", "approve", "no failure mode found under load"),
        ]
    )
    assert isinstance(result, ConsensusResult)
    assert result.outcome == "approved"
    assert "unanimous" in result.rationale
    assert result.explanations == {
        "executor": "the plan is sound and reversible",
        "audit": "consistent with policy",
        "stress": "no failure mode found under load",
    }


def test_single_reject_vetoes_regardless_of_the_other_two() -> None:
    result = evaluate(
        [
            _verdict("executor", "approve", "looks good to me"),
            _verdict("audit", "approve", "no policy conflict"),
            _verdict("stress", "reject", "fails catastrophically under peak load"),
        ]
    )
    assert result.outcome == "rejected"
    assert "stress" in result.rationale


def test_multiple_rejects_are_named_in_the_rationale() -> None:
    result = evaluate(
        [
            _verdict("executor", "reject", "changed my mind, this is unsafe"),
            _verdict("audit", "approve", "no policy conflict"),
            _verdict("stress", "reject", "fails catastrophically under peak load"),
        ]
    )
    assert result.outcome == "rejected"
    assert "executor" in result.rationale and "stress" in result.rationale


def test_disagreement_without_a_veto_escalates_to_a_human() -> None:
    result = evaluate(
        [
            _verdict("executor", "approve", "ready to ship"),
            _verdict("audit", "needs_changes", "missing a rollback plan"),
            _verdict("stress", "approve", "handles the load test fine"),
        ]
    )
    assert result.outcome == "escalated"
    assert "audit" in result.rationale


def test_all_needs_changes_escalates_not_rejects() -> None:
    result = evaluate(
        [
            _verdict("executor", "needs_changes", "needs a staging rollout"),
            _verdict("audit", "needs_changes", "needs a compliance sign-off"),
            _verdict("stress", "needs_changes", "needs a load test first"),
        ]
    )
    assert result.outcome == "escalated"


def test_missing_a_role_is_an_incomplete_review() -> None:
    with pytest.raises(IncompleteReviewError):
        evaluate(
            [
                _verdict("executor", "approve"),
                _verdict("audit", "approve"),
            ]
        )


def test_duplicate_role_is_rejected() -> None:
    with pytest.raises(DuplicateRoleError):
        evaluate(
            [
                _verdict("executor", "approve"),
                _verdict("executor", "approve"),
                _verdict("audit", "approve"),
                _verdict("stress", "approve"),
            ]
        )


def test_verdicts_are_reported_in_fixed_role_order_regardless_of_input_order() -> None:
    result = evaluate(
        [
            _verdict("stress", "approve"),
            _verdict("executor", "approve"),
            _verdict("audit", "approve"),
        ]
    )
    assert tuple(v.role for v in result.verdicts) == ROLES
