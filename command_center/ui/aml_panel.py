"""First AML monitoring UI slice, backed by synthetic records only."""

from __future__ import annotations

from typing import Any

import streamlit as st

from command_center import aml_store

RISK_LABELS = {"critical": "Критический", "high": "Высокий", "medium": "Средний"}
STATUS_LABELS = {
    "new": "Новый",
    "review": "На проверке",
    "escalated": "Эскалирован",
    "waiting": "Ожидает данных",
    "closed": "Закрыт",
    "reported": "Сообщение утверждено",
}
RISK_COLORS = {"critical": "red", "high": "orange", "medium": "blue"}

DEMO_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "AML-2026-0418", "customer": "Orion Trade FZE", "country": "ОАЭ",
        "risk": "critical", "score": 94, "status": "new", "amount": 1_840_000,
        "currency": "USD", "opened": "Сегодня, 09:42", "owner": "Не назначен",
        "scenario": "Rapid movement of funds",
        "summary": "Средства прошли через счёт менее чем за 24 часа без очевидной деловой цели.",
        "factors": ("4 входящих платежа от новых контрагентов", "92% средств выведено в течение 6 часов",
                    "Контрагент связан с юрисдикцией повышенного риска"),
        "transactions": (
            ("29.07 · 08:17", "Входящий перевод", "+ 620 000 USD", "Helios Export Ltd"),
            ("29.07 · 09:03", "Входящий перевод", "+ 710 000 USD", "Northbridge LLC"),
            ("29.07 · 09:38", "Исходящий перевод", "− 1 690 000 USD", "Meridian Holdings"),
        ),
    },
    {
        "id": "AML-2026-0416", "customer": "M. Rahman", "country": "Великобритания",
        "risk": "high", "score": 86, "status": "review", "amount": 284_500,
        "currency": "EUR", "opened": "Сегодня, 08:15", "owner": "Анна К.",
        "scenario": "Structuring",
        "summary": "Серия переводов ниже внутреннего порога с последующей консолидацией.",
        "factors": ("11 однотипных платежей за 3 дня", "Суммы близки к порогу мониторинга"),
        "transactions": (
            ("28.07 · 15:10", "Входящий перевод", "+ 24 900 EUR", "Private sender"),
            ("29.07 · 07:54", "Исходящий перевод", "− 263 000 EUR", "Atlas Property GmbH"),
        ),
    },
    {
        "id": "AML-2026-0412", "customer": "Baltic Components OÜ", "country": "Эстония",
        "risk": "high", "score": 79, "status": "waiting", "amount": 415_000,
        "currency": "USD", "opened": "Вчера, 16:20", "owner": "Максим П.",
        "scenario": "Counterparty risk",
        "summary": "Платёж новому контрагенту не соответствует заявленному профилю деятельности.",
        "factors": ("Новый контрагент", "Назначение платежа не совпадает с профилем клиента"),
        "transactions": (("28.07 · 14:51", "Исходящий перевод", "− 415 000 USD", "Vega Consulting SA"),),
    },
    {
        "id": "AML-2026-0409", "customer": "Nova Digital Sp. z o.o.", "country": "Польша",
        "risk": "medium", "score": 64, "status": "escalated", "amount": 98_200,
        "currency": "EUR", "opened": "Вчера, 11:05", "owner": "Ирина С.",
        "scenario": "Unusual activity",
        "summary": "Оборот значительно превысил среднемесячный профиль клиента.",
        "factors": ("Оборот x4,7 к среднему", "Активность в нетипичное время"),
        "transactions": (("28.07 · 02:14", "Входящий перевод", "+ 98 200 EUR", "Nova Services BV"),),
    },
)

CUSTOMERS: tuple[dict[str, Any], ...] = (
    {"name": "Orion Trade FZE", "type": "Юридическое лицо", "country": "ОАЭ", "risk": "Высокий",
     "kyc": "Требует обновления", "pep": "Нет", "cases": 2, "review": "31.07.2026"},
    {"name": "M. Rahman", "type": "Физическое лицо", "country": "Великобритания", "risk": "Высокий",
     "kyc": "Актуален", "pep": "Потенциальное совпадение", "cases": 1, "review": "15.08.2026"},
    {"name": "Baltic Components OÜ", "type": "Юридическое лицо", "country": "Эстония", "risk": "Средний",
     "kyc": "Ожидаются документы", "pep": "Нет", "cases": 1, "review": "05.08.2026"},
    {"name": "Nova Digital Sp. z o.o.", "type": "Юридическое лицо", "country": "Польша", "risk": "Средний",
     "kyc": "Актуален", "pep": "Нет", "cases": 1, "review": "18.12.2026"},
)

RULES: tuple[dict[str, Any], ...] = (
    {"rule": "Rapid movement of funds", "alerts": 14, "true_positive": "43%", "status": "Активно"},
    {"rule": "Structuring", "alerts": 9, "true_positive": "56%", "status": "Активно"},
    {"rule": "Counterparty risk", "alerts": 6, "true_positive": "67%", "status": "Активно"},
    {"rule": "Unusual activity", "alerts": 18, "true_positive": "28%", "status": "На калибровке"},
)

VIEW_LABELS = {
    "overview": "Обзор",
    "queue": "Очередь",
    "customers": "Клиенты",
    "investigation": "Расследование",
    "reporting": "Отчётность",
}


def filter_cases(cases: tuple[dict[str, Any], ...] | list[dict[str, Any]], *, risks: list[str], statuses: list[str], query: str) -> list[dict[str, Any]]:
    """Return cases matching the operator's active queue filters."""
    needle = query.strip().casefold()
    return [
        case for case in cases
        if (not risks or case["risk"] in risks)
        and (not statuses or case["status"] in statuses)
        and (not needle or needle in case["id"].casefold() or needle in case["customer"].casefold()
             or needle in case["country"].casefold())
    ]


def _money(case: dict[str, Any]) -> str:
    return f"{case['amount']:,.0f} {case['currency']}".replace(",", " ")


def _open_investigation(case_id: str) -> None:
    """Select a case before the AML view widget is recreated on rerun."""
    st.session_state.aml_investigation_case = case_id
    st.session_state.aml_view = "investigation"


def _init_state() -> None:
    aml_store.seed_cases(DEMO_CASES)
    st.session_state.setdefault("aml_actor", "AML Analyst")
    st.session_state.setdefault("aml_role", "Analyst")


def _apply_action(case: dict[str, Any], action: str, note: str, *, confirmed: bool = False) -> None:
    try:
        aml_store.transition_case(
            case["id"],
            action,
            actor=st.session_state.aml_actor,
            role=st.session_state.aml_role,
            reason=note,
            expected_version=case["version"],
            confirmed=confirmed,
        )
    except aml_store.AmlStoreError as exc:
        st.error(str(exc))
    else:
        st.rerun()


@st.dialog("Мгновенное закрытие по playbook")
def _instant_closure_dialog(case: dict[str, Any], initial_note: str) -> None:
    st.warning(f"Закрыть в 1 клик: **{case['id']} · {case['customer']}**")
    st.caption(
        f"Playbook «{aml_store.INSTANT_PLAYBOOK_ID}»: сценарий {case['scenario']}, "
        f"риск {case['risk']}. Роль: {st.session_state.aml_role} · пользователь: {st.session_state.aml_actor}"
    )
    reason = st.text_area(
        "Обоснование закрытия",
        value=initial_note,
        placeholder="Укажите, почему алерт не требует расследования…",
        key=f"aml_instant_reason_{case['id']}",
    )
    confirmed = st.checkbox(
        "Я проверил(а) материалы и подтверждаю закрытие по playbook",
        key=f"aml_instant_confirm_{case['id']}",
    )
    if st.button(
        "Закрыть в 1 клик",
        type="primary",
        icon=":material/bolt:",
        disabled=not confirmed or not reason.strip(),
        key=f"aml_instant_submit_{case['id']}",
    ):
        try:
            report = aml_store.run_instant_closure(
                case["id"],
                actor=st.session_state.aml_actor,
                role=st.session_state.aml_role,
                reason=reason,
                expected_version=case["version"],
                confirmed=True,
            )
        except aml_store.AmlStoreError as exc:
            st.error(str(exc))
        else:
            st.session_state.aml_last_instant_report = report
            st.rerun()


@st.dialog("Подтвердите критическое действие")
def _critical_action_dialog(case: dict[str, Any], action: str, action_label: str, initial_note: str) -> None:
    st.warning(f"{action_label}: **{case['id']} · {case['customer']}**")
    st.caption(f"Роль: {st.session_state.aml_role} · пользователь: {st.session_state.aml_actor}")
    reason = st.text_area(
        "Обоснование",
        value=initial_note,
        placeholder="Укажите факты и основание решения…",
        key=f"aml_critical_reason_{action}_{case['id']}",
    )
    confirmed = st.checkbox(
        "Я проверил(а) материалы и подтверждаю действие",
        key=f"aml_critical_confirm_{action}_{case['id']}",
    )
    if st.button(
        action_label,
        type="primary",
        icon=":material/gavel:",
        disabled=not confirmed or not reason.strip(),
        key=f"aml_critical_submit_{action}_{case['id']}",
    ):
        _apply_action(case, action, reason, confirmed=True)


@st.dialog("Утверждение регуляторного сообщения")
def _approve_sar_dialog(sar: dict[str, Any]) -> None:
    st.warning(f"Утвердить **{sar['id']}** по кейсу **{sar['case_id']}**?")
    st.write(sar["rationale"])
    reason = st.text_area(
        "Основание решения MLRO",
        placeholder="Укажите результаты проверки и основание утверждения…",
        key=f"aml_sar_approval_reason_{sar['id']}",
    )
    confirmed = st.checkbox(
        "Я проверил(а) материалы и подтверждаю регуляторное сообщение",
        key=f"aml_sar_approval_confirm_{sar['id']}",
    )
    if st.button(
        "Утвердить сообщение",
        type="primary",
        icon=":material/verified:",
        disabled=not confirmed or not reason.strip(),
        key=f"aml_sar_approval_submit_{sar['id']}",
    ):
        try:
            aml_store.approve_sar(
                sar["id"],
                actor=st.session_state.aml_actor,
                role=st.session_state.aml_role,
                reason=reason,
                confirmed=True,
            )
        except aml_store.AmlStoreError as exc:
            st.error(str(exc))
        else:
            st.rerun()


def _render_metrics(cases: list[dict[str, Any]]) -> None:
    open_cases = [case for case in cases if case["status"] not in {"closed", "reported"}]
    with st.container(horizontal=True):
        st.metric("Открытые алерты", len(open_cases), "+2 сегодня", border=True, chart_data=[3, 4, 3, 5, 4, 6, len(open_cases)])
        st.metric("Критические", sum(case["risk"] == "critical" for case in cases), "требуют внимания", border=True)
        st.metric("На проверке", sum(case["status"] == "review" for case in cases), border=True)
        st.metric("Объём операций", f"${sum(case['amount'] for case in cases) / 1_000_000:.1f}M", border=True)


def _case_rows(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "Кейс": case["id"],
            "Клиент": case["customer"],
            "Страна": case["country"],
            "Риск": RISK_LABELS[case["risk"]],
            "Скор": case["score"],
            "Сумма": _money(case),
            "Статус": STATUS_LABELS[case["status"]],
            "Аналитик": case["owner"],
            "Открыт": case["opened"],
        }
        for case in cases
    ]


def _render_overview(cases: list[dict[str, Any]]) -> None:
    _render_metrics(cases)
    left, right = st.columns([3, 2])
    with left:
        with st.container(border=True):
            st.markdown("#### Приоритетная очередь")
            priority = sorted(
                (case for case in cases if case["status"] not in {"closed", "reported"}),
                key=lambda case: case["score"],
                reverse=True,
            )
            st.dataframe(_case_rows(priority[:4]), hide_index=True, key="aml_overview_queue")
    with right:
        with st.container(border=True):
            st.markdown("#### Этапы процесса")
            stage_counts = [
                {"Этап": STATUS_LABELS[status], "Кейсы": sum(case["status"] == status for case in cases)}
                for status in ("new", "review", "waiting", "escalated", "reported", "closed")
            ]
            st.bar_chart(stage_counts, x="Этап", y="Кейсы", horizontal=True)
    with st.container(border=True):
        st.markdown("#### Контроль SLA")
        sla_cols = st.columns(3)
        sla_cols[0].metric("До нарушения SLA", "1ч 18м", "AML-2026-0418")
        sla_cols[1].metric("В пределах SLA", "3 кейса")
        sla_cols[2].metric("Просрочено", "0", delta_color="inverse")


def _queue_filters(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filter_cols = st.columns([2, 2, 3])
    risks = filter_cols[0].multiselect("Уровень риска", list(RISK_LABELS), format_func=RISK_LABELS.get,
                                      key="aml_risk_filter", placeholder="Все уровни", persist_state="session")
    statuses = filter_cols[1].multiselect("Статус", list(STATUS_LABELS), format_func=STATUS_LABELS.get,
                                         key="aml_status_filter", placeholder="Все статусы", persist_state="session")
    query = filter_cols[2].text_input("Поиск", key="aml_query", placeholder="Кейс, клиент или страна",
                                      icon=":material/search:", persist_state="session")
    return filter_cases(cases, risks=risks, statuses=statuses, query=query)


def _render_queue(cases: list[dict[str, Any]]) -> None:
    st.markdown("#### Очередь алертов")
    filtered = _queue_filters(cases)
    if not filtered:
        st.warning("По выбранным фильтрам алерты не найдены.")
        return
    st.dataframe(_case_rows(filtered), hide_index=True, key="aml_queue_table")
    selected_id = st.selectbox(
        "Открыть кейс", [case["id"] for case in filtered],
        format_func=lambda case_id: next(f"{case_id} · {case['customer']}" for case in filtered if case["id"] == case_id),
        key="aml_selected_case", persist_state="session",
    )
    with st.container(horizontal=True):
        st.button(
            "Перейти к расследованию",
            type="primary",
            icon=":material/manage_search:",
            key="aml_open_investigation",
            on_click=_open_investigation,
            args=(selected_id,),
        )
        st.caption("Выбранный кейс сохранится при переходе между окнами.")


def _render_customers(cases: list[dict[str, Any]]) -> None:
    st.markdown("#### Клиенты и KYC")
    search = st.text_input("Поиск клиента", placeholder="Название, страна или тип клиента",
                           key="aml_customer_search", icon=":material/search:")
    needle = search.strip().casefold()
    customers = [
        customer for customer in CUSTOMERS
        if not needle or needle in customer["name"].casefold() or needle in customer["country"].casefold()
        or needle in customer["type"].casefold()
    ]
    st.dataframe(customers, hide_index=True, key="aml_customers_table", column_config={
        "name": "Клиент", "type": "Тип", "country": "Страна", "risk": "Риск",
        "kyc": "KYC", "pep": "PEP / санкции", "cases": "Кейсы", "review": "Следующая проверка",
    })
    if not customers:
        st.info("Клиенты не найдены.")
        return
    selected = st.selectbox("Карточка клиента", [customer["name"] for customer in customers],
                            key="aml_customer_selected")
    customer = next(item for item in customers if item["name"] == selected)
    profile, monitoring = st.columns(2)
    with profile:
        with st.container(border=True):
            st.markdown(f"##### {customer['name']}")
            st.write(f"**Тип:** {customer['type']}")
            st.write(f"**Страна:** {customer['country']}")
            st.write(f"**KYC:** {customer['kyc']}")
            st.write(f"**PEP / санкции:** {customer['pep']}")
    with monitoring:
        with st.container(border=True):
            st.markdown("##### Мониторинг")
            customer_cases = [case for case in cases if case["customer"] == customer["name"]]
            st.metric("Связанные кейсы", len(customer_cases))
            st.write(f"**Следующая проверка:** {customer['review']}")
            st.write("**Ожидаемые операции:** международная торговля / услуги")


def _render_investigation(cases: list[dict[str, Any]]) -> None:
    st.markdown("#### Рабочее место расследования")
    default_id = st.session_state.get("aml_investigation_case")
    case_ids = [case["id"] for case in cases]
    selected_id = st.selectbox(
        "Кейс",
        case_ids,
        index=case_ids.index(default_id) if default_id in case_ids else 0,
        format_func=lambda case_id: next(f"{case_id} · {case['customer']}" for case in cases if case["id"] == case_id),
        key="aml_investigation_case",
        persist_state="session",
    )
    case = next(case for case in cases if case["id"] == selected_id)
    case_sar = next((sar for sar in aml_store.list_sar() if sar["case_id"] == case["id"]), None)
    st.divider()
    header, score = st.columns([4, 1])
    with header:
        st.markdown(f"### {case['id']} · {case['customer']}")
        st.caption(f"{case['scenario']} · {case['country']} · открыт {case['opened']} · аналитик: {case['owner']}")
        st.badge(RISK_LABELS[case["risk"]], color=RISK_COLORS[case["risk"]])
        st.badge(STATUS_LABELS[case["status"]], color="blue")
    score.metric("Risk score", f"{case['score']} / 100")

    detail, actions = st.columns([3, 2])
    with detail:
        st.markdown("##### Почему создан алерт")
        st.write(case["summary"])
        for factor in case["factors"]:
            st.markdown(f"- {factor}")
        st.markdown("##### Связанные транзакции")
        st.dataframe([
            {"Время": ts, "Тип": tx_type, "Сумма": amount, "Контрагент": counterparty}
            for ts, tx_type, amount, counterparty in case["transactions"]
        ], hide_index=True, width="stretch")
    with actions:
        with st.container(border=True):
            st.markdown("##### Следующее действие")
            st.write("Проверить экономический смысл операций и источник средств.")
            note = st.text_area("Заметка аналитика", placeholder="Зафиксируйте наблюдения и недостающие документы…",
                                key=f"aml_note_{case['id']}", persist_state="session")
            if st.button("Взять в работу", type="primary", width="stretch", key=f"aml_assign_{case['id']}",
                         disabled=case["status"] != "new" or st.session_state.aml_role != "Analyst"):
                _apply_action(case, "assign", note)
            if st.button("Запросить документы", width="stretch", key=f"aml_request_{case['id']}",
                         disabled=case["status"] != "review" or st.session_state.aml_role != "Analyst"):
                _apply_action(case, "documents", note)
            if st.button("Эскалировать MLRO", width="stretch", key=f"aml_escalate_{case['id']}",
                         disabled=case["status"] not in {"review", "waiting"} or st.session_state.aml_role != "Analyst"):
                _critical_action_dialog(case, "escalate", "Подтвердить эскалацию MLRO", note)
            if st.button("Закрыть без сообщения", width="stretch", key=f"aml_close_{case['id']}",
                         disabled=case["status"] != "escalated" or case_sar is not None
                         or st.session_state.aml_role != "MLRO"):
                _critical_action_dialog(case, "close", "Подтвердить закрытие кейса", note)
            if case_sar is not None:
                st.caption(f"Связанное сообщение: {case_sar['id']} · {case_sar['status']}")
            if aml_store.instant_closure_eligible(case):
                st.divider()
                st.caption("Кейс соответствует критериям playbook «низкий/средний риск, Unusual activity».")
                if st.button(
                    "⚡ Закрыть в 1 клик",
                    width="stretch",
                    key=f"aml_instant_close_{case['id']}",
                    disabled=st.session_state.aml_role != "MLRO",
                ):
                    _instant_closure_dialog(case, note)
    st.markdown("##### Хронология кейса")
    events = aml_store.list_audit_events(case["id"])
    st.dataframe(events, hide_index=True, key=f"aml_case_timeline_{case['id']}",
                 column_config={"id": None, "occurred_at": "Время", "actor": "Инициатор", "role": "Роль",
                                "event": "Событие", "details": "Комментарий", "case_id": None})
    last_report = st.session_state.get("aml_last_instant_report")
    if last_report is not None and last_report["case_id"] == case["id"]:
        st.markdown("##### Авто-отчёт о закрытии по playbook")
        st.success(f"Отчёт {last_report['id']} сформирован автоматически на основании трассы решений выше.")
        st.markdown(aml_store.render_incident_report_markdown(last_report))


def _render_reporting(cases: list[dict[str, Any]]) -> None:
    st.markdown("#### Отчётность и контроль")
    report, controls = st.tabs(["Регуляторная отчётность", "Правила мониторинга"])
    with report:
        sar_drafts = aml_store.list_sar()
        cases_with_sar = {sar["case_id"] for sar in sar_drafts}
        escalated = [case for case in cases if case["status"] == "escalated"]
        eligible = [case for case in escalated if case["id"] not in cases_with_sar]
        st.metric("На решении MLRO", len(escalated), border=True)
        if eligible:
            st.dataframe(_case_rows(escalated), hide_index=True, key="aml_reporting_cases")
            selected = st.selectbox("Кейс для черновика", [case["id"] for case in eligible], key="aml_sar_case")
            with st.form("aml_sar_form"):
                rationale = st.text_area("Основание для сообщения", placeholder="Опишите подозрительную активность и связь операций…")
                filing_type = st.selectbox("Тип", ["SAR / STR", "Внутреннее уведомление MLRO"])
                submitted = st.form_submit_button("Сохранить черновик", icon=":material/draft:")
            if submitted:
                try:
                    aml_store.create_sar_draft(
                        selected,
                        filing_type=filing_type,
                        rationale=rationale,
                        actor=st.session_state.aml_actor,
                        role=st.session_state.aml_role,
                    )
                except aml_store.AmlStoreError as exc:
                    st.error(str(exc))
                else:
                    st.success("Черновик сохранён в постоянном AML-хранилище.")
                    st.rerun()
        elif not escalated:
            st.info("Нет кейсов, эскалированных для решения MLRO.")
        else:
            st.info("Для всех эскалированных кейсов уже созданы сообщения.")
        if sar_drafts:
            st.markdown("##### Черновики")
            st.dataframe(sar_drafts, hide_index=True, key="aml_sar_drafts")
            if st.session_state.aml_role == "MLRO":
                pending = [sar for sar in sar_drafts if sar["status"] == "draft"]
                if pending:
                    selected_sar_id = st.selectbox(
                        "Черновик для решения",
                        [sar["id"] for sar in pending],
                        key="aml_sar_approval_selected",
                    )
                    selected_sar = next(sar for sar in pending if sar["id"] == selected_sar_id)
                    if st.button(
                        "Проверить и утвердить",
                        icon=":material/fact_check:",
                        key="aml_sar_approval_open",
                    ):
                        _approve_sar_dialog(selected_sar)
        st.markdown("##### Журнал аудита")
        st.dataframe(aml_store.list_audit_events(), hide_index=True, key="aml_audit_log")
    with controls:
        st.dataframe(RULES, hide_index=True, key="aml_rules", column_config={
            "rule": "Правило", "alerts": "Алерты за 30 дней",
            "true_positive": "Подтверждено", "status": "Статус",
        })
        st.info("Изменение порогов и правил появится после ввода ролей, согласования и версионирования.")


def render() -> None:
    """Render the AML workspace backed by the persistent AML store."""
    _init_state()
    st.subheader("AML Monitoring")
    st.caption("Мониторинг транзакций, расследования и регуляторная отчётность · SQLite AML store")
    st.info(
        "Рабочий прототип с синтетическими данными и локальным хранилищем. "
        "Роль и пользователь ниже — только симуляция, не authentication/RBAC. "
        "Внешние банковские системы не подключены.",
        icon=":material/shield:",
    )
    identity = st.columns([2, 2, 3])
    identity[0].selectbox(
        "Роль (симуляция)",
        aml_store.ROLES,
        key="aml_role",
        persist_state="session",
        help="Права действий проверяются повторно в AML repository.",
    )
    identity[1].text_input("Пользователь (демо)", key="aml_actor", persist_state="session")
    identity[2].caption(
        "Analyst расследует и эскалирует · MLRO принимает финальные решения. "
        "Критические действия требуют отдельного подтверждения."
    )
    view = st.segmented_control(
        "Раздел AML",
        list(VIEW_LABELS),
        default="overview",
        required=True,
        format_func=VIEW_LABELS.get,
        key="aml_view",
        width="stretch",
        persist_state="session",
    )
    cases = aml_store.list_cases()
    if view == "overview":
        _render_overview(cases)
    elif view == "queue":
        _render_queue(cases)
    elif view == "customers":
        _render_customers(cases)
    elif view == "investigation":
        _render_investigation(cases)
    else:
        _render_reporting(cases)
