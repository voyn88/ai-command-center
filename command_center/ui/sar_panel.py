"""AML SAR Panel — Streamlit UI for Suspicious Activity Report filing."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from command_center import sar_store
from command_center.ui import confirm_dialog
from command_center.sar_store import (
    SAR_STATES,
    SAR_TYPES,
    SUBJECT_ROLES,
    SUBJECT_TYPES,
    ID_TYPES,
    InvalidTransition,
    MissingRequiredField,
    NarrativeLocked,
    PermissionDenied,
    SarNotFound,
    SarStoreError,
)

_STATE_BADGE: dict[str, str] = {
    "draft": "📝",
    "under_review": "🔍",
    "approved": "✅",
    "submitted": "📤",
    "acknowledged": "🏛️",
    "rejected": "❌",
}

_TYPE_LABEL: dict[str, str] = {
    "str": "STR (ст. 7 115-ФЗ)",
    "ctr": "CTR (ст. 6 115-ФЗ)",
    "pep": "PEP-связанный",
    "sanctions": "Санкционный",
    "other": "Прочее",
}


def render(
    sar_db: Path | None = None,
    case_db: Path | None = None,
) -> None:
    if sar_db is None:
        sar_db = sar_store.resolve_db_path()
    sar_store.init_db(sar_db)

    st.header("SAR — Отчёты о подозрительных операциях")

    overdue = sar_store.list_overdue(sar_db)
    if overdue:
        st.error(f"⚠️ Просрочено SAR: {len(overdue)} — требуют немедленной подачи!")

    tab_list, tab_create, tab_detail, tab_submitted = st.tabs(
        ["Список SAR", "Создать SAR", "Детали SAR", "Поданные / Подтверждения"]
    )

    with tab_list:
        _render_list(sar_db)

    with tab_create:
        _render_create(sar_db)

    with tab_detail:
        _render_detail(sar_db)

    with tab_submitted:
        _render_submitted(sar_db)


# ---------------------------------------------------------------------------
# Tab: List
# ---------------------------------------------------------------------------


def _render_list(sar_db: Path) -> None:
    col1, col2 = st.columns(2)
    with col1:
        state_filter = st.selectbox("Статус", ["— все —"] + list(SAR_STATES), key="list_state")
    with col2:
        type_filter = st.selectbox("Тип", ["— все —"] + list(SAR_TYPES), key="list_type")

    sf = state_filter if state_filter != "— все —" else None
    tf = type_filter if type_filter != "— все —" else None
    sars = sar_store.list_sars(sar_db, state=sf, sar_type=tf)

    st.caption(f"Найдено: {len(sars)}")
    if not sars:
        st.info("SAR не найдены.")
        return

    rows = []
    for s in sars:
        overdue_flag = "🔴" if sar_store.is_overdue(s) else ""
        rows.append({
            "Номер": s["sar_number"],
            "Тип": _TYPE_LABEL.get(s["sar_type"], s["sar_type"]),
            "Статус": f"{_STATE_BADGE.get(s['state'], '')} {s['state']}",
            "Дедлайн": s["filing_deadline"][:10] + overdue_flag,
            "Создан": s["created_at"][:10],
            "Кем": s["created_by"],
        })
    st.dataframe(rows, use_container_width=True)


# ---------------------------------------------------------------------------
# Tab: Create
# ---------------------------------------------------------------------------


def _render_create(sar_db: Path) -> None:
    with st.form("create_sar"):
        sar_type = st.selectbox(
            "Тип SAR *",
            options=list(SAR_TYPES),
            format_func=lambda t: _TYPE_LABEL.get(t, t),
        )
        case_id = st.text_input("ID дела (опционально)")
        narrative = st.text_area(
            "Нарратив (описание подозрительной деятельности)",
            height=150,
            help="Изложите факты: кто, что, когда, сумма, почему подозрительно.",
        )
        created_by = st.text_input("Создатель", value="ComplianceOfficer")

        submitted = st.form_submit_button("Создать SAR")
        if submitted:
            try:
                sar = sar_store.create_sar(
                    sar_db,
                    sar_type=sar_type,
                    narrative=narrative.strip() or None,
                    case_id=case_id.strip() or None,
                    created_by=created_by.strip() or "ComplianceOfficer",
                )
                st.success(
                    f"SAR {sar['sar_number']} создан. "
                    f"Дедлайн подачи: {sar['filing_deadline'][:10]}"
                )
            except (SarStoreError, MissingRequiredField) as e:
                st.error(str(e))


# ---------------------------------------------------------------------------
# Tab: Detail
# ---------------------------------------------------------------------------


def _render_detail(sar_db: Path) -> None:
    sar_ref = st.text_input("Номер SAR или ID (напр. SAR-2026-00001)", key="detail_ref")
    if not sar_ref.strip():
        st.info("Введите номер SAR.")
        return

    try:
        if sar_ref.strip().startswith("SAR-"):
            sar = sar_store.get_sar_by_number(sar_db, sar_ref.strip())
        else:
            sar = sar_store.get_sar(sar_db, sar_ref.strip())
    except SarNotFound:
        st.error(f"SAR {sar_ref!r} не найден.")
        return

    _render_sar_header(sar)
    _render_narrative_editor(sar_db, sar)
    _render_state_actions(sar_db, sar)
    _render_subjects(sar_db, sar)
    _render_sar_audit_log(sar_db, sar["id"])


def _render_sar_header(sar: dict) -> None:
    overdue = sar_store.is_overdue(sar)
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Номер", sar["sar_number"])
    col2.metric("Тип", _TYPE_LABEL.get(sar["sar_type"], sar["sar_type"]))
    col3.metric("Статус", f"{_STATE_BADGE.get(sar['state'], '')} {sar['state']}")
    col4.metric(
        "Дедлайн",
        sar["filing_deadline"][:10],
        delta="ПРОСРОЧЕН" if overdue else None,
        delta_color="inverse",
    )
    if sar.get("case_id"):
        st.caption(f"Дело: {sar['case_id']}")
    if sar.get("submission_ref"):
        st.success(f"Подан: ref {sar['submission_ref']} | {sar.get('submitted_at', '')[:10]}")
    if sar.get("acknowledgement_ref"):
        st.success(f"Подтверждён регулятором: ref {sar['acknowledgement_ref']}")
    if sar.get("rejection_reason"):
        st.error(f"Отклонён: {sar['rejection_reason']}")


def _render_narrative_editor(sar_db: Path, sar: dict) -> None:
    st.subheader("Нарратив")
    editable = sar["state"] in ("draft", "under_review")
    if editable:
        with st.form(f"narrative_{sar['id']}"):
            text = st.text_area(
                "Описание подозрительной деятельности *",
                value=sar.get("narrative") or "",
                height=200,
            )
            actor = st.text_input("Редактор", value="Analyst")
            if st.form_submit_button("Сохранить нарратив"):
                _safe(lambda: sar_store.update_narrative(
                    sar_db, sar["id"], narrative=text, actor=actor))
    else:
        st.text_area(
            "Нарратив (только чтение)",
            value=sar.get("narrative") or "(не указан)",
            height=200,
            disabled=True,
        )


def _render_state_actions(sar_db: Path, sar: dict) -> None:
    st.subheader("Действия")
    actor = st.text_input("Актор", value="ComplianceOfficer", key=f"act_{sar['id']}")
    state = sar["state"]

    if state == "draft":
        if st.button("📋 Отправить на ревью", key=f"rev_{sar['id']}"):
            _safe(lambda: sar_store.submit_for_review(sar_db, sar["id"], actor=actor))

    elif state == "under_review":
        col_a, col_b, col_c = st.columns(3)
        with col_a:
            if st.button("✅ Утвердить", key=f"apr_{sar['id']}"):
                _safe(lambda: sar_store.approve_sar(sar_db, sar["id"], actor=actor))
        with col_b:
            reject_reason = st.text_input("Причина отказа", key=f"rr_{sar['id']}")
            if st.button("❌ Отклонить", key=f"rej_{sar['id']}"):
                _safe(lambda: sar_store.reject_sar(
                    sar_db, sar["id"], actor=actor, rejection_reason=reject_reason))
        with col_c:
            if st.button("↩ Вернуть в драфт", key=f"rdraft_{sar['id']}"):
                _safe(lambda: sar_store.reopen_to_draft(sar_db, sar["id"], actor=actor))

    elif state == "approved":
        col_a, col_b = st.columns(2)
        with col_a:
            sub_ref = st.text_input("Ref подачи в Росфинмониторинг *", key=f"sr_{sar['id']}")
            if st.button("📤 Подать в регулятор", key=f"sub_{sar['id']}"):
                _safe(lambda: sar_store.submit_to_regulator(
                    sar_db, sar["id"], actor=actor, submission_ref=sub_ref))
        with col_b:
            if st.button("↩ Вернуть в драфт", key=f"rdraft2_{sar['id']}"):
                _safe(lambda: sar_store.reopen_to_draft(sar_db, sar["id"], actor=actor))

    elif state == "submitted":
        col_a, col_b = st.columns(2)
        with col_a:
            ack_ref = st.text_input("Ref подтверждения *", key=f"ar_{sar['id']}")
            if st.button("🏛️ Зафиксировать подтверждение", key=f"ack_{sar['id']}"):
                _safe(lambda: sar_store.acknowledge_receipt(
                    sar_db, sar["id"], actor=actor, acknowledgement_ref=ack_ref))
        with col_b:
            rej_reason = st.text_input("Причина отклонения", key=f"rjr_{sar['id']}")
            if st.button("❌ Регулятор отклонил", key=f"rjb_{sar['id']}"):
                _safe(lambda: sar_store.reject_sar(
                    sar_db, sar["id"], actor=actor, rejection_reason=rej_reason))

    elif state == "rejected":
        if st.button("↩ Вернуть в драфт для доработки", key=f"ropen_{sar['id']}"):
            _safe(lambda: sar_store.reopen_to_draft(sar_db, sar["id"], actor=actor))

    else:
        st.info(f"SAR в финальном статусе: **{state}**.")


def _render_subjects(sar_db: Path, sar: dict) -> None:
    st.subheader("Субъекты SAR")
    subjects = sar_store.get_subjects(sar_db, sar["id"])
    if subjects:
        st.dataframe(
            [{"Роль": s["subject_role"], "Тип": s["subject_type"],
              "Имя/Наим.": s["name"], "ИД": f"{s.get('id_type','')}: {s.get('id_number','') or '—'}",
              "Счёт": s.get("account_number") or "—"}
             for s in subjects],
            use_container_width=True,
        )
    else:
        st.info("Субъекты не добавлены.")

    locked = sar["state"] in ("submitted", "acknowledged")
    if not locked:
        with st.expander("Добавить субъекта"):
            with st.form(f"add_subj_{sar['id']}"):
                subj_name = st.text_input("Наименование / ФИО *")
                col1, col2 = st.columns(2)
                with col1:
                    subj_type = st.selectbox("Тип субъекта", list(SUBJECT_TYPES))
                with col2:
                    subj_role = st.selectbox("Роль", list(SUBJECT_ROLES))
                col3, col4 = st.columns(2)
                with col3:
                    id_type = st.selectbox("Тип документа", ["—"] + list(ID_TYPES))
                with col4:
                    id_number = st.text_input("Номер документа")
                nationality = st.text_input("Гражданство (ISO2)")
                address = st.text_input("Адрес")
                account_number = st.text_input("Номер счёта")
                subj_actor = st.text_input("Добавил", value="Analyst")
                if st.form_submit_button("Добавить субъекта"):
                    _safe(lambda: sar_store.add_subject(
                        sar_db, sar["id"],
                        subject_type=subj_type,
                        subject_role=subj_role,
                        name=subj_name,
                        id_type=id_type if id_type != "—" else None,
                        id_number=id_number or None,
                        nationality=nationality or None,
                        address=address or None,
                        account_number=account_number or None,
                        added_by=subj_actor,
                    ))


def _render_sar_audit_log(sar_db: Path, sar_id: str) -> None:
    with st.expander("Журнал аудита SAR"):
        log = sar_store.get_audit_log(sar_db, sar_id)
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
# Tab: Submitted / Acknowledged
# ---------------------------------------------------------------------------


def _render_submitted(sar_db: Path) -> None:
    col1, col2 = st.columns(2)
    with col1:
        submitted = sar_store.list_sars(sar_db, state="submitted")
        st.metric("Ожидают подтверждения", len(submitted))
        if submitted:
            st.dataframe(
                [{"Номер": s["sar_number"], "Тип": s["sar_type"],
                  "Ref подачи": s.get("submission_ref", "—"),
                  "Дата подачи": (s.get("submitted_at") or "")[:10]}
                 for s in submitted],
                use_container_width=True,
            )
    with col2:
        acknowledged = sar_store.list_sars(sar_db, state="acknowledged")
        st.metric("Подтверждены регулятором", len(acknowledged))
        if acknowledged:
            st.dataframe(
                [{"Номер": s["sar_number"], "Тип": s["sar_type"],
                  "Ref подтв.": s.get("acknowledgement_ref", "—"),
                  "Подтверждён": (s.get("acknowledged_at") or "")[:10]}
                 for s in acknowledged],
                use_container_width=True,
            )

    overdue = sar_store.list_overdue(sar_db)
    if overdue:
        st.subheader("Просроченные SAR")
        st.dataframe(
            [{"Номер": s["sar_number"], "Тип": s["sar_type"],
              "Статус": s["state"],
              "Дедлайн": s["filing_deadline"][:10]}
             for s in overdue],
            use_container_width=True,
        )


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _safe(fn) -> None:
    try:
        fn()
        st.rerun()
    except (PermissionDenied, InvalidTransition, MissingRequiredField,
            NarrativeLocked, SarStoreError) as e:
        st.error(str(e))
