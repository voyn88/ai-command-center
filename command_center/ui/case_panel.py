"""AML Case Panel — Streamlit UI for case management."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from command_center import alert_store, case_store, evidence_store
from command_center.case_store import (
    CASE_STATES,
    PRIORITIES,
    CaseNotFound,
    CaseStoreError,
    InvalidTransition,
    MissingRequiredField,
    PermissionDenied,
)

_STATE_BADGE: dict[str, str] = {
    "open": "🔵",
    "under_investigation": "🟡",
    "pending_review": "🟠",
    "closed": "✅",
    "escalated_to_sar": "🚨",
}

_PRIORITY_BADGE: dict[str, str] = {
    "low": "🟢",
    "medium": "🟡",
    "high": "🔴",
    "critical": "🚨",
}


def render(
    case_db: Path | None = None,
    alert_db: Path | None = None,
    evidence_db: Path | None = None,
) -> None:
    if case_db is None:
        case_db = case_store.resolve_db_path()
    if alert_db is None:
        alert_db = alert_store.resolve_db_path()
        alert_store.init_db(alert_db)
    if evidence_db is None:
        evidence_db = evidence_store.resolve_db_path()
        evidence_store.init_db(evidence_db)

    case_store.init_db(case_db)

    st.header("Дела AML")

    tab_list, tab_create, tab_detail, tab_my = st.tabs(
        ["Список дел", "Открыть дело", "Дело", "Мои дела"]
    )

    with tab_list:
        _render_list(case_db)

    with tab_create:
        _render_create(case_db, alert_db)

    with tab_detail:
        _render_detail(case_db, alert_db, evidence_db)

    with tab_my:
        _render_my_cases(case_db)


# ---------------------------------------------------------------------------
# Tab: List
# ---------------------------------------------------------------------------


def _render_list(case_db: Path) -> None:
    col1, col2, col3 = st.columns(3)
    with col1:
        state_filter = st.selectbox("Статус", ["— все —"] + list(CASE_STATES))
    with col2:
        priority_filter = st.selectbox("Приоритет", ["— все —"] + list(PRIORITIES))
    with col3:
        assignee_filter = st.text_input("Следователь")

    sf = state_filter if state_filter != "— все —" else None
    pf = priority_filter if priority_filter != "— все —" else None
    af = assignee_filter.strip() or None
    cases = case_store.list_cases(case_db, state=sf, priority=pf, assigned_to=af)

    st.caption(f"Найдено: {len(cases)}")
    if not cases:
        st.info("Дела не найдены.")
        return

    rows = [
        {
            "Номер": c["case_number"],
            "Тема": c["title"],
            "Статус": f"{_STATE_BADGE.get(c['state'], '')} {c['state']}",
            "Приоритет": f"{_PRIORITY_BADGE.get(c['priority'], '')} {c['priority']}",
            "Следователь": c["assigned_to"] or "—",
            "Создано": c["created_at"][:10],
        }
        for c in cases
    ]
    st.dataframe(rows, use_container_width=True)


# ---------------------------------------------------------------------------
# Tab: Create
# ---------------------------------------------------------------------------


def _render_create(case_db: Path, alert_db: Path) -> None:
    with st.form("create_case"):
        title = st.text_input("Тема дела *")
        description = st.text_area("Описание", height=80)

        col1, col2 = st.columns(2)
        with col1:
            priority = st.selectbox("Приоритет", list(PRIORITIES), index=1)
        with col2:
            assigned_to = st.text_input("Назначить следователю")

        alert_id = st.text_input("Привязать алерт (ID, опционально)")
        created_by = st.text_input("Создатель", value="ComplianceOfficer")

        submitted = st.form_submit_button("Открыть дело")
        if submitted:
            if not title.strip():
                st.error("Укажите тему дела.")
                return
            try:
                case = case_store.create_case(
                    case_db,
                    title=title.strip(),
                    description=description.strip() or None,
                    priority=priority,
                    created_by=created_by.strip() or "ComplianceOfficer",
                    assigned_to=assigned_to.strip() or None,
                )
                if alert_id.strip():
                    case_store.link_alert(
                        case_db, case["id"], alert_id.strip(),
                        actor=created_by.strip() or "ComplianceOfficer",
                    )
                    st.success(
                        f"Дело {case['case_number']} открыто и алерт {alert_id.strip()[:8]} привязан."
                    )
                else:
                    st.success(f"Дело {case['case_number']} открыто.")
            except (CaseStoreError, MissingRequiredField) as e:
                st.error(str(e))


# ---------------------------------------------------------------------------
# Tab: Case detail
# ---------------------------------------------------------------------------


def _render_detail(case_db: Path, alert_db: Path, evidence_db: Path) -> None:
    case_ref = st.text_input("Номер дела или ID (напр. AML-00001)")
    if not case_ref.strip():
        st.info("Введите номер или ID дела.")
        return

    try:
        if case_ref.strip().startswith("AML-"):
            case = case_store.get_case_by_number(case_db, case_ref.strip())
        else:
            case = case_store.get_case(case_db, case_ref.strip())
    except CaseNotFound:
        st.error(f"Дело {case_ref!r} не найдено.")
        return

    _render_case_header(case)
    _render_state_actions(case_db, case)
    _render_linked_alerts(case_db, alert_db, case)
    _render_evidence_section(evidence_db, case)
    _render_audit_log(case_db, case["id"])


def _render_case_header(case: dict) -> None:
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Номер", case["case_number"])
    col2.metric("Статус", f"{_STATE_BADGE.get(case['state'], '')} {case['state']}")
    col3.metric("Приоритет", f"{_PRIORITY_BADGE.get(case['priority'], '')} {case['priority']}")
    col4.metric("Следователь", case["assigned_to"] or "—")

    st.subheader(case["title"])
    if case.get("description"):
        st.caption(case["description"])
    if case.get("closure_reason"):
        st.info(f"Причина закрытия: {case['closure_reason']}")
    if case.get("sar_ref"):
        st.warning(f"SAR ref: {case['sar_ref']}")


def _render_state_actions(case_db: Path, case: dict) -> None:
    st.subheader("Действия")
    actor = st.text_input("Актор", value="ComplianceOfficer", key=f"actor_{case['id']}")
    state = case["state"]

    if state == "open":
        if st.button("▶ Начать расследование", key=f"start_{case['id']}"):
            _safe(lambda: case_store.start_investigation(case_db, case["id"], actor=actor))

    elif state == "under_investigation":
        col_a, col_b = st.columns(2)
        with col_a:
            new_assignee = st.text_input("Переназначить", key=f"reassign_{case['id']}")
            if st.button("Переназначить", key=f"btn_reassign_{case['id']}"):
                if new_assignee.strip():
                    _safe(lambda: case_store.assign_investigator(
                        case_db, case["id"], assigned_to=new_assignee.strip(), actor=actor))
        with col_b:
            if st.button("📋 Отправить на ревью", key=f"review_{case['id']}"):
                _safe(lambda: case_store.submit_for_review(case_db, case["id"], actor=actor))

    elif state == "pending_review":
        col_a, col_b = st.columns(2)
        with col_a:
            closure_reason = st.text_area("Причина закрытия *", key=f"close_reason_{case['id']}", height=60)
            if st.button("✅ Закрыть дело", key=f"close_{case['id']}"):
                _safe(lambda: case_store.close_case(
                    case_db, case["id"], actor=actor, closure_reason=closure_reason))
        with col_b:
            sar_ref = st.text_input("SAR ref *", key=f"sar_ref_{case['id']}")
            if st.button("🚨 Эскалировать в SAR", key=f"sar_{case['id']}"):
                _safe(lambda: case_store.escalate_to_sar(
                    case_db, case["id"], actor=actor, sar_ref=sar_ref))

    else:
        st.info(f"Дело в финальном статусе: **{state}**. Действия недоступны.")


def _render_linked_alerts(case_db: Path, alert_db: Path, case: dict) -> None:
    st.subheader("Привязанные алерты")
    links = case_store.get_case_alerts(case_db, case["id"])

    if links:
        rows = []
        for lnk in links:
            try:
                a = alert_store.get_alert(alert_db, lnk["alert_id"])
                rows.append({
                    "Alert ID": lnk["alert_id"][:8],
                    "Статус": a["state"],
                    "Источник": a.get("source", "—"),
                    "Тема": a.get("trigger_desc", "")[:60],
                    "Привязан": lnk["linked_at"][:10],
                })
            except Exception:
                rows.append({
                    "Alert ID": lnk["alert_id"][:8],
                    "Статус": "?",
                    "Источник": "—",
                    "Тема": "(недоступен)",
                    "Привязан": lnk["linked_at"][:10],
                })
        st.dataframe(rows, use_container_width=True)
    else:
        st.info("Алертов не привязано.")

    with st.expander("Привязать алерт"):
        al_id = st.text_input("Alert ID", key=f"link_alert_{case['id']}")
        al_actor = st.text_input("Актор", value="Analyst", key=f"link_actor_{case['id']}")
        if st.button("Привязать", key=f"btn_link_{case['id']}"):
            if al_id.strip():
                _safe(lambda: case_store.link_alert(
                    case_db, case["id"], al_id.strip(), actor=al_actor))


def _render_evidence_section(evidence_db: Path, case: dict) -> None:
    st.subheader("Доказательная база")
    items = evidence_store.list_evidence(
        evidence_db, entity_type="case", entity_id=case["id"]
    )
    st.metric("Документов", len(items))
    if items:
        st.dataframe(
            [{"Тип": i["evidence_type"], "Название": i["title"],
              "Добавил": i["submitted_by"], "Дата": i["submitted_at"][:10]}
             for i in items],
            use_container_width=True,
        )

    with st.expander("Добавить документ"):
        with st.form(f"add_ev_{case['id']}"):
            ev_title = st.text_input("Название *")
            ev_type = st.selectbox("Тип", sorted(evidence_store.EVIDENCE_TYPES))
            file_ref = st.text_input("Путь к файлу / ссылка")
            url = st.text_input("URL (альтернативно)")
            ev_actor = st.text_input("Добавил", value="Analyst")
            if st.form_submit_button("Добавить"):
                _safe(lambda: evidence_store.attach_evidence(
                    evidence_db,
                    entity_type="case",
                    entity_id=case["id"],
                    evidence_type=ev_type,
                    title=ev_title,
                    file_ref=file_ref or None,
                    url=url or None,
                    submitted_by=ev_actor,
                ))


def _render_audit_log(case_db: Path, case_id: str) -> None:
    with st.expander("Журнал аудита дела"):
        log = case_store.get_audit_log(case_db, case_id)
        if log:
            st.dataframe(
                [{"Время": e["occurred_at"][:19], "Актор": e["actor"],
                  "Действие": e["action"],
                  "Из": e.get("from_state") or "—",
                  "В": e.get("to_state") or "—",
                  "Детали": e.get("detail") or ""}
                 for e in log],
                use_container_width=True,
            )
        else:
            st.info("Лог пуст.")


# ---------------------------------------------------------------------------
# Tab: My cases
# ---------------------------------------------------------------------------


def _render_my_cases(case_db: Path) -> None:
    investigator = st.text_input("Имя следователя", value="")
    if not investigator.strip():
        st.info("Введите имя следователя.")
        return
    cases = case_store.list_cases(case_db, assigned_to=investigator.strip())
    if not cases:
        st.info(f"Нет дел для {investigator!r}.")
        return
    open_cases = [c for c in cases if c["state"] not in ("closed", "escalated_to_sar")]
    st.metric("Активных дел", len(open_cases))
    rows = [
        {
            "Номер": c["case_number"],
            "Тема": c["title"],
            "Статус": f"{_STATE_BADGE.get(c['state'], '')} {c['state']}",
            "Приоритет": f"{_PRIORITY_BADGE.get(c['priority'], '')} {c['priority']}",
            "Создано": c["created_at"][:10],
        }
        for c in cases
    ]
    st.dataframe(rows, use_container_width=True)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _safe(fn) -> None:
    try:
        fn()
        st.rerun()
    except (PermissionDenied, InvalidTransition, MissingRequiredField, CaseStoreError) as e:
        st.error(str(e))
