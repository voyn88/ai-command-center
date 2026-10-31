from __future__ import annotations

import sqlite3
from pathlib import Path

from streamlit.testing.v1 import AppTest

from command_center import aml_store
from command_center.ui import aml_panel

APP_PATH = str(Path(__file__).resolve().parent.parent / "app.py")


def _at_on_aml_page(**session_state) -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.session_state["nav_page"] = "aml"
    for key, value in session_state.items():
        at.session_state[key] = value
    return at.run()


def test_aml_page_renders_overview_and_all_workspace_views():
    at = _at_on_aml_page()
    assert not at.exception
    assert at.subheader[0].value == "AML Monitoring"
    assert any(metric.label == "Открытые алерты" for metric in at.metric)
    assert at.segmented_control[0].options == ["Обзор", "Очередь", "Клиенты", "Расследование", "Отчётность"]


def test_aml_queue_renders_filters_and_case_selection():
    at = _at_on_aml_page(aml_view="queue")
    assert not at.exception
    assert any(select.key == "aml_selected_case" for select in at.selectbox)
    open_button = next(button for button in at.button if button.label == "Перейти к расследованию")

    at = open_button.click().run()
    assert not at.exception
    assert at.session_state["aml_view"] == "investigation"
    assert any("Рабочее место расследования" in markdown.value for markdown in at.markdown)


def test_aml_investigation_actions_drive_case_lifecycle():
    at = _at_on_aml_page(aml_view="investigation")
    assign = next(button for button in at.button if button.label == "Взять в работу")
    assert not assign.disabled

    at = assign.click().run()
    assert not at.exception
    assign = next(button for button in at.button if button.label == "Взять в работу")
    assert assign.disabled
    case = next(case for case in aml_store.list_cases() if case["id"] == "AML-2026-0418")
    assert case["status"] == "review"
    assert case["owner"] == "AML Analyst"
    assert aml_store.list_audit_events(case["id"])[0]["event"] == "Кейс взят в работу"


def test_aml_page_marks_persistent_local_prototype():
    at = _at_on_aml_page()
    assert any("синтетическими данными" in info.value for info in at.info)
    assert any("не authentication/RBAC" in info.value for info in at.info)


def test_aml_page_renders_while_another_writer_holds_the_store():
    aml_store.seed_cases(aml_panel.DEMO_CASES)
    path = aml_store.resolve_db_path()
    writer = sqlite3.connect(path, timeout=1)
    try:
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE aml_case SET score = 95 WHERE id = 'AML-2026-0418'")

        at = _at_on_aml_page()
    finally:
        writer.rollback()
        writer.close()

    assert not at.exception
    assert any(metric.label == "Открытые алерты" for metric in at.metric)


def test_mlro_role_enables_close_and_disables_analyst_actions():
    aml_store.seed_cases(aml_panel.DEMO_CASES)
    case = next(case for case in aml_store.list_cases() if case["id"] == "AML-2026-0418")
    review = aml_store.transition_case(
        case["id"], "assign", actor="AML Analyst", role="Analyst", reason="Проверка",
        expected_version=case["version"],
    )
    aml_store.transition_case(
        case["id"], "escalate", actor="AML Analyst", role="Analyst", reason="Требуется решение",
        expected_version=review["version"], confirmed=True,
    )
    at = _at_on_aml_page(aml_view="investigation", aml_role="MLRO", aml_actor="Maria MLRO")

    close = next(button for button in at.button if button.label == "Закрыть без сообщения")
    assign = next(button for button in at.button if button.label == "Взять в работу")
    assert not close.disabled
    assert assign.disabled


def test_filter_cases_matches_risk_status_and_search():
    result = aml_panel.filter_cases(aml_panel.DEMO_CASES, risks=["high"], statuses=["waiting"], query="Baltic")
    assert [case["id"] for case in result] == ["AML-2026-0412"]


def test_aml_search_empty_state_is_safe():
    at = _at_on_aml_page(aml_view="queue", aml_query="не-существующий-клиент")
    assert not at.exception
    assert any("алерты не найдены" in warning.value for warning in at.warning)


def test_customer_and_reporting_windows_render():
    customers = _at_on_aml_page(aml_view="customers")
    assert not customers.exception
    assert any("Клиенты и KYC" in markdown.value for markdown in customers.markdown)

    reporting = _at_on_aml_page(aml_view="reporting")
    assert not reporting.exception
    assert len(reporting.tabs) == 2
    assert any("Журнал аудита" in markdown.value for markdown in reporting.markdown)


def test_mlro_reporting_exposes_sar_approval_flow():
    aml_store.seed_cases(aml_panel.DEMO_CASES)
    case = next(case for case in aml_store.list_cases() if case["id"] == "AML-2026-0418")
    review = aml_store.transition_case(
        case["id"], "assign", actor="AML Analyst", role="Analyst", reason="Проверка",
        expected_version=case["version"],
    )
    aml_store.transition_case(
        case["id"], "escalate", actor="AML Analyst", role="Analyst", reason="Требуется решение",
        expected_version=review["version"], confirmed=True,
    )
    aml_store.create_sar_draft(
        "AML-2026-0418",
        filing_type="SAR / STR",
        rationale="Транзит средств требует сообщения",
        actor="AML Analyst",
        role="Analyst",
    )
    at = _at_on_aml_page(aml_view="reporting", aml_role="MLRO", aml_actor="Maria MLRO")

    assert not at.exception
    assert any(button.label == "Проверить и утвердить" for button in at.button)


_INSTANT_ELIGIBLE_CASE = {
    "id": "AML-2026-2001", "customer": "Delta Retail LLC", "country": "Латвия",
    "risk": "medium", "score": 58, "status": "new", "amount": 42_500,
    "currency": "EUR", "opened": "Сегодня, 10:00", "owner": "Не назначен",
    "scenario": "Unusual activity",
    "summary": "Оборот немного превысил среднемесячный профиль клиента.",
    "factors": ("Оборот x1,4 к среднему",),
    "transactions": (("29.07 · 10:00", "Входящий перевод", "+ 42 500 EUR", "Regular counterparty"),),
}


def test_instant_closure_button_visible_for_eligible_case_and_gated_by_role():
    aml_store.seed_cases((_INSTANT_ELIGIBLE_CASE,))
    at = _at_on_aml_page(
        aml_view="investigation", aml_investigation_case="AML-2026-2001",
        aml_role="MLRO", aml_actor="Maria MLRO",
    )
    instant_close = next(button for button in at.button if button.label == "⚡ Закрыть в 1 клик")
    assert not instant_close.disabled

    at = _at_on_aml_page(
        aml_view="investigation", aml_investigation_case="AML-2026-2001",
        aml_role="Analyst", aml_actor="AML Analyst",
    )
    instant_close = next(button for button in at.button if button.label == "⚡ Закрыть в 1 клик")
    assert instant_close.disabled


def test_instant_closure_button_hidden_for_ineligible_case():
    at = _at_on_aml_page(aml_view="investigation", aml_investigation_case="AML-2026-0418", aml_role="MLRO")
    assert not any(button.label == "⚡ Закрыть в 1 клик" for button in at.button)


def test_instant_closure_report_renders_after_playbook_runs():
    aml_store.seed_cases((_INSTANT_ELIGIBLE_CASE,))
    report = aml_store.run_instant_closure(
        "AML-2026-2001", actor="Maria MLRO", role="MLRO",
        reason="Соответствует критериям playbook, ложное срабатывание.",
        expected_version=0, confirmed=True,
    )
    at = _at_on_aml_page(
        aml_view="investigation", aml_investigation_case="AML-2026-2001",
        aml_role="MLRO", aml_actor="Maria MLRO", aml_last_instant_report=report,
    )
    assert not at.exception
    assert any("Авто-отчёт о закрытии по playbook" in markdown.value for markdown in at.markdown)
    assert any(report["id"] in success.value for success in at.success)
    assert any("Трасса решений" in markdown.value for markdown in at.markdown)
