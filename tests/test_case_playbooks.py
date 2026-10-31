"""Tests for command_center.case_playbooks — memory-as-code case scenarios."""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import case_playbooks, case_store


@pytest.fixture()
def c_db(tmp_path: Path) -> Path:
    p = tmp_path / "cases.db"
    case_store.init_db(p)
    return p


def _to_pending_review(c_db: Path, case_id: str) -> None:
    case_store.start_investigation(c_db, case_id, actor="Analyst")
    case_store.submit_for_review(c_db, case_id, actor="Analyst")


# ---------------------------------------------------------------------------
# Catalog shape (acceptance: 3 cases migrated to memory-as-code)
# ---------------------------------------------------------------------------


def test_catalog_has_three_migrated_cases():
    assert len(case_playbooks.SCENARIO_CATALOG) == 3


def test_catalog_entries_trace_back_to_a_source_case():
    source_numbers = {s.source_case_number for s in case_playbooks.SCENARIO_CATALOG}
    assert len(source_numbers) == 3
    for scenario in case_playbooks.SCENARIO_CATALOG:
        assert scenario.source_case_number
        assert scenario.narrative
        assert scenario.conditions
        assert scenario.recommended_action in case_playbooks.RECOMMENDED_ACTIONS


# ---------------------------------------------------------------------------
# Matching new incidents against migrated scenarios
# ---------------------------------------------------------------------------


def test_match_scenario_sanctions_incident():
    incident = {"alert_type": "sanctions", "country": "IR", "amount": 50_000}
    match = case_playbooks.match_scenario(incident)
    assert match is not None
    assert match.scenario.id == "MEM-001-sanctions-immediate-escalation"
    assert "AML-00058" in match.reason


def test_match_scenario_structuring_incident():
    incident = {
        "alert_type": "structuring",
        "frequency": 7,
        "industry": "cash_intensive",
    }
    match = case_playbooks.match_scenario(incident)
    assert match is not None
    assert match.scenario.id == "MEM-002-structuring-escalation"


def test_match_scenario_pep_false_positive_incident():
    incident = {
        "alert_type": "pep_related",
        "pep_flag": True,
        "adverse_media_flag": False,
        "amount": 25_000,
    }
    match = case_playbooks.match_scenario(incident)
    assert match is not None
    assert match.scenario.id == "MEM-003-pep-clean-edd-false-positive"


def test_match_scenario_pep_with_adverse_media_does_not_match_false_positive():
    incident = {
        "alert_type": "pep_related",
        "pep_flag": True,
        "adverse_media_flag": True,
        "amount": 25_000,
    }
    assert case_playbooks.match_scenario(incident) is None


def test_match_scenario_returns_none_for_unrelated_incident():
    incident = {"alert_type": "generic", "amount": 10}
    assert case_playbooks.match_scenario(incident) is None


def test_match_scenario_prefers_more_specific_sanctions_over_generic_country():
    incident = {"alert_type": "sanctions", "country": "RU", "amount": 1}
    match = case_playbooks.match_scenario(incident)
    assert match is not None
    assert match.scenario.recommended_action == "escalate_to_sar"


# ---------------------------------------------------------------------------
# Applying a matched scenario to a real, new case (executable half)
# ---------------------------------------------------------------------------


def test_apply_scenario_escalates_sanctions_case_to_sar(c_db: Path):
    case = case_store.create_case(c_db, title="Wire to sanctioned jurisdiction", created_by="Analyst")
    _to_pending_review(c_db, case["id"])

    incident = {"alert_type": "sanctions", "country": "KP"}
    match = case_playbooks.match_scenario(incident)
    assert match is not None

    updated = case_playbooks.apply_scenario(c_db, case["id"], match, actor="MLRO")

    assert updated["state"] == "escalated_to_sar"
    assert updated["sar_ref"] == f"SAR-{match.scenario.id}-{case['case_number']}"


def test_apply_scenario_closes_pep_false_positive_case(c_db: Path):
    case = case_store.create_case(c_db, title="PEP customer review", created_by="Analyst")
    _to_pending_review(c_db, case["id"])

    incident = {
        "alert_type": "pep_related",
        "pep_flag": True,
        "adverse_media_flag": False,
        "amount": 1_000,
    }
    match = case_playbooks.match_scenario(incident)
    assert match is not None

    updated = case_playbooks.apply_scenario(c_db, case["id"], match, actor="ComplianceOfficer")

    assert updated["state"] == "closed"
    assert "AML-00051" in updated["closure_reason"]


def test_apply_scenario_respects_case_store_permission_rules(c_db: Path):
    case = case_store.create_case(c_db, title="Structuring pattern", created_by="Analyst")
    _to_pending_review(c_db, case["id"])

    incident = {"alert_type": "structuring", "frequency": 6, "industry": "cash_intensive"}
    match = case_playbooks.match_scenario(incident)
    assert match is not None

    with pytest.raises(case_store.PermissionDenied):
        case_playbooks.apply_scenario(c_db, case["id"], match, actor="Analyst")


def test_apply_scenario_rejects_unsupported_action(c_db: Path):
    case = case_store.create_case(c_db, title="Whatever", created_by="Analyst")
    _to_pending_review(c_db, case["id"])

    bogus_scenario = case_playbooks.CaseScenario(
        id="MEM-999-bogus",
        source_case_number="AML-00099",
        title="Bogus",
        narrative="n/a",
        conditions=(),
        recommended_action="delete_forever",
        recommended_priority="low",
        action_reason_template="n/a",
    )
    match = case_playbooks.ScenarioMatch(scenario=bogus_scenario, incident={}, reason="n/a")

    with pytest.raises(ValueError):
        case_playbooks.apply_scenario(c_db, case["id"], match, actor="MLRO")
