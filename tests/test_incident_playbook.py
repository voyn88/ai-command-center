"""Tests for the one-click instant-closure playbook in `command_center.aml_store`.

Covers the acceptance bar for VOYN-MIN-WOW-2: one incident type (an untriaged,
low/medium-risk "Unusual activity" alert) closes in a single call, with an
auto-generated report and a full decision trace pulled from the append-only
audit log.
"""

from __future__ import annotations

import pytest

from command_center import aml_store

ELIGIBLE_CASE = {
    "id": "AML-2026-1001",
    "customer": "Delta Retail LLC",
    "country": "Латвия",
    "risk": "medium",
    "score": 58,
    "status": "new",
    "amount": 42_500,
    "currency": "EUR",
    "opened": "Сегодня, 10:00",
    "owner": "Не назначен",
    "scenario": "Unusual activity",
    "summary": "Оборот немного превысил среднемесячный профиль клиента.",
    "factors": ("Оборот x1,4 к среднему",),
    "transactions": (("29.07 · 10:00", "Входящий перевод", "+ 42 500 EUR", "Regular counterparty"),),
}

INELIGIBLE_SCENARIO_CASE = {**ELIGIBLE_CASE, "id": "AML-2026-1002", "scenario": "Structuring"}
INELIGIBLE_RISK_CASE = {**ELIGIBLE_CASE, "id": "AML-2026-1003", "risk": "high"}
INELIGIBLE_STATUS_CASE = {**ELIGIBLE_CASE, "id": "AML-2026-1004", "status": "review"}


def _db(tmp_path, *cases):
    path = tmp_path / "aml.db"
    aml_store.seed_cases(cases or (ELIGIBLE_CASE,), path)
    return path


def _case(path, case_id="AML-2026-1001"):
    return next(case for case in aml_store.list_cases(path) if case["id"] == case_id)


def test_instant_closure_eligible_matches_only_the_one_wired_incident_type():
    assert aml_store.instant_closure_eligible(ELIGIBLE_CASE)
    assert not aml_store.instant_closure_eligible(INELIGIBLE_SCENARIO_CASE)
    assert not aml_store.instant_closure_eligible(INELIGIBLE_RISK_CASE)
    assert not aml_store.instant_closure_eligible(INELIGIBLE_STATUS_CASE)


def test_run_instant_closure_closes_case_and_records_decision_trace(tmp_path):
    path = _db(tmp_path)
    case = _case(path)

    report = aml_store.run_instant_closure(
        case["id"], actor="Maria K.", role="MLRO", reason="Соответствует критериям playbook, ложное срабатывание.",
        expected_version=case["version"], confirmed=True, db_path=path,
    )

    closed = _case(path)
    assert closed["status"] == "closed"
    assert closed["owner"] == "Maria K."
    assert closed["version"] == case["version"] + 1

    assert report["case_id"] == case["id"]
    assert report["playbook"] == aml_store.INSTANT_PLAYBOOK_ID
    assert report["outcome"] == aml_store.INSTANT_PLAYBOOK_OUTCOME
    assert report["actor"] == "Maria K."
    assert report["role"] == "MLRO"
    assert report["case_snapshot"]["scenario"] == "Unusual activity"

    trace_events = [event["event"] for event in report["decision_trace"]]
    assert trace_events == [
        "Создан алерт",
        "Playbook запущен",
        "Автоматическая проверка пройдена",
        "Кейс закрыт по playbook",
    ]
    assert all(event["case_id"] == case["id"] for event in report["decision_trace"])

    persisted_events = [event["event"] for event in aml_store.list_audit_events(case["id"], path)]
    assert persisted_events.count("Playbook запущен") == 1
    assert persisted_events.count("Кейс закрыт по playbook") == 1


def test_run_instant_closure_persists_and_renders_report(tmp_path):
    path = _db(tmp_path)
    case = _case(path)

    report = aml_store.run_instant_closure(
        case["id"], actor="Maria K.", role="MLRO", reason="Проверено, риск не подтверждён.",
        expected_version=case["version"], confirmed=True, db_path=path,
    )

    assert aml_store.get_incident_report(case["id"], path) == report
    assert aml_store.list_incident_reports(path) == [report]

    markdown = aml_store.render_incident_report_markdown(report)
    assert case["id"] in markdown
    assert "## Трасса решений" in markdown
    assert "Кейс закрыт по playbook" in markdown
    assert "Проверено, риск не подтверждён." in markdown


def test_run_instant_closure_rejects_ineligible_scenario(tmp_path):
    path = _db(tmp_path, INELIGIBLE_SCENARIO_CASE)
    case = _case(path, INELIGIBLE_SCENARIO_CASE["id"])

    with pytest.raises(aml_store.InvalidTransition):
        aml_store.run_instant_closure(
            case["id"], actor="Maria K.", role="MLRO", reason="Проверено.",
            expected_version=case["version"], confirmed=True, db_path=path,
        )


def test_run_instant_closure_rejects_ineligible_risk(tmp_path):
    path = _db(tmp_path, INELIGIBLE_RISK_CASE)
    case = _case(path, INELIGIBLE_RISK_CASE["id"])

    with pytest.raises(aml_store.InvalidTransition):
        aml_store.run_instant_closure(
            case["id"], actor="Maria K.", role="MLRO", reason="Проверено.",
            expected_version=case["version"], confirmed=True, db_path=path,
        )


def test_run_instant_closure_rejects_ineligible_status(tmp_path):
    path = _db(tmp_path, INELIGIBLE_STATUS_CASE)
    case = _case(path, INELIGIBLE_STATUS_CASE["id"])

    with pytest.raises(aml_store.InvalidTransition):
        aml_store.run_instant_closure(
            case["id"], actor="Maria K.", role="MLRO", reason="Проверено.",
            expected_version=case["version"], confirmed=True, db_path=path,
        )


def test_run_instant_closure_requires_mlro_role(tmp_path):
    path = _db(tmp_path)
    case = _case(path)

    with pytest.raises(aml_store.PermissionDenied):
        aml_store.run_instant_closure(
            case["id"], actor="Anna", role="Analyst", reason="Проверено.",
            expected_version=case["version"], confirmed=True, db_path=path,
        )


def test_run_instant_closure_requires_confirmation_and_reason(tmp_path):
    path = _db(tmp_path)
    case = _case(path)

    with pytest.raises(aml_store.ConfirmationRequired):
        aml_store.run_instant_closure(
            case["id"], actor="Maria K.", role="MLRO", reason="Проверено.",
            expected_version=case["version"], confirmed=False, db_path=path,
        )

    with pytest.raises(aml_store.ConfirmationRequired):
        aml_store.run_instant_closure(
            case["id"], actor="Maria K.", role="MLRO", reason="   ",
            expected_version=case["version"], confirmed=True, db_path=path,
        )


def test_run_instant_closure_rejects_stale_version(tmp_path):
    path = _db(tmp_path)
    case = _case(path)

    with pytest.raises(aml_store.LostUpdate):
        aml_store.run_instant_closure(
            case["id"], actor="Maria K.", role="MLRO", reason="Проверено.",
            expected_version=case["version"] + 1, confirmed=True, db_path=path,
        )


def test_run_instant_closure_rejects_case_with_active_sar(tmp_path):
    # A case already escalated with a SAR is not "new" and thus not eligible,
    # but this also proves the belt-and-braces SAR guard holds if a future
    # eligibility rule were ever loosened to include escalated cases.
    path = _db(tmp_path)
    case = _case(path)
    reviewed = aml_store.transition_case(
        case["id"], "assign", actor="Anna", role="Analyst", reason="Проверка",
        expected_version=case["version"], db_path=path,
    )
    escalated = aml_store.transition_case(
        case["id"], "escalate", actor="Anna", role="Analyst", reason="Требуется решение",
        expected_version=reviewed["version"], confirmed=True, db_path=path,
    )
    aml_store.create_sar_draft(
        case["id"], filing_type="SAR / STR", rationale="Подозрительная активность",
        actor="Anna", role="Analyst", db_path=path,
    )

    with pytest.raises(aml_store.InvalidTransition):
        aml_store.run_instant_closure(
            case["id"], actor="Maria K.", role="MLRO", reason="Проверено.",
            expected_version=escalated["version"], confirmed=True, db_path=path,
        )
