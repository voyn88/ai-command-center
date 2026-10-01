import pytest

from command_center.evidence_checklist import (
    ChecklistItem,
    EvidenceInsufficient,
    checklist_from_registry,
    evaluate_checklist,
    require_sufficient_evidence,
)


def test_checklist_allows_only_when_every_item_is_satisfied():
    decision = evaluate_checklist(
        "commit",
        [
            ChecklistItem("backup_verified", True),
            ChecklistItem("rollback_plan_recorded", True),
        ],
    )

    assert decision.operation == "commit"
    assert decision.allowed is True
    assert decision.reasons == ()


def test_checklist_reports_every_unsatisfied_item_not_just_the_first():
    decision = evaluate_checklist(
        "commit",
        [
            ChecklistItem("backup_verified", False, "no backup on record"),
            ChecklistItem("rollback_plan_recorded", True),
            ChecklistItem("operator_identity_confirmed", False),
        ],
    )

    assert decision.allowed is False
    assert decision.reasons == (
        "backup_verified: no backup on record",
        "operator_identity_confirmed",
    )


def test_checklist_with_no_items_fails_closed_instead_of_trivially_passing():
    decision = evaluate_checklist("uninstall", [])

    assert decision.allowed is False
    assert decision.reasons == ("uninstall: no_checklist_defined",)


def test_require_sufficient_evidence_raises_with_all_reasons_joined():
    with pytest.raises(EvidenceInsufficient) as excinfo:
        require_sufficient_evidence(
            "commit", [ChecklistItem("backup_verified", False, "missing")]
        )

    assert excinfo.value.decision.operation == "commit"
    assert "commit" in str(excinfo.value)
    assert "backup_verified: missing" in str(excinfo.value)


def test_require_sufficient_evidence_returns_the_decision_when_allowed():
    decision = require_sufficient_evidence(
        "commit", [ChecklistItem("backup_verified", True)]
    )

    assert decision.allowed is True


REGISTRY = {
    "commit": ("operator_identity_confirmed", "backup_verified"),
}


def test_checklist_from_registry_treats_missing_evidence_as_unsatisfied():
    items = checklist_from_registry("commit", REGISTRY, {})

    assert items == (
        ChecklistItem("operator_identity_confirmed", False),
        ChecklistItem("backup_verified", False),
    )


def test_checklist_from_registry_treats_blank_string_as_unsatisfied():
    items = checklist_from_registry(
        "commit",
        REGISTRY,
        {"operator_identity_confirmed": "   ", "backup_verified": False},
    )

    assert all(not item.satisfied for item in items)


def test_checklist_from_registry_accepts_string_detail_or_truthy_value():
    items = checklist_from_registry(
        "commit",
        REGISTRY,
        {"operator_identity_confirmed": "dimastov, change #1234", "backup_verified": True},
    )

    assert items == (
        ChecklistItem(
            "operator_identity_confirmed", True, "dimastov, change #1234"
        ),
        ChecklistItem("backup_verified", True),
    )


def test_checklist_from_registry_unknown_operation_yields_no_items():
    assert checklist_from_registry("no-such-op", REGISTRY, {}) == ()
