"""Agent Tuning Policy Panel — Streamlit UI-конструктор для policy-driven
управления весом метрик агентов, fallback-цепочками и SLA (VOYN-MIN-AGT-TUNING).

Every action here goes straight through `command_center.agent_policy` — a
policy created in this panel is a row in `agent_tuning_policies.db` and is
picked up by the scheduler on its very next tick (see
`agent_policy.tuned_registry` / `agent_policy.sla_seconds_for`, wired into
`runtime.api.RuntimeAPI.plan_schedule` and `task_pipeline.adapt_ready_entries`
respectively). No code change or redeploy is required to add, edit, disable
or delete a policy — that is the acceptance criterion this panel exists to
satisfy.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from command_center import agent_policy, executors, models
from command_center.agent_policy import AgentPolicyError, PolicyNotFound
from command_center.runtime import scheduler


def render(policy_db: Path | None = None) -> None:
    if policy_db is None:
        policy_db = agent_policy.resolve_db_path()
    agent_policy.init_db(policy_db)

    st.header("Политики агентов (вес, fallback, SLA)")
    st.caption(
        "Новая политика вступает в силу на следующем такте планировщика — "
        "без деплоя кода."
    )

    tab_list, tab_create, tab_test = st.tabs(
        ["Список политик", "Добавить политику", "Проверить применение"]
    )

    with tab_list:
        _render_list(policy_db)
    with tab_create:
        _render_create(policy_db)
    with tab_test:
        _render_test(policy_db)


# ---------------------------------------------------------------------------
# Tab: policy list
# ---------------------------------------------------------------------------


def _known_agent_ids() -> list[str]:
    return sorted(executors.EXECUTORS.keys())


def _render_list(policy_db: Path) -> None:
    enabled_only = st.checkbox("Только активные", value=False)
    policies = agent_policy.list_policies(policy_db, enabled_only=enabled_only)

    if not policies:
        st.info("Политики ещё не заданы — вкладка «Добавить политику».")
        return

    st.caption(f"Найдено: {len(policies)}")
    for p in policies:
        status = "✅ Активна" if p["enabled"] else "⏸ Отключена"
        label = f"{status} — {p['name']} (task_type={p['task_type']}, priority={p['priority']})"
        with st.expander(label, expanded=False):
            if p.get("description"):
                st.caption(p["description"])

            col_a, col_b, col_c = st.columns(3)
            col_a.metric("SLA (сек)", p["sla_seconds"] if p["sla_seconds"] is not None else "—")
            col_b.metric(
                "Fallback-цепочка",
                " → ".join(p["fallback_agents"]) if p.get("fallback_agents") else "—",
            )
            col_c.metric(
                "Явные веса",
                ", ".join(f"{k}:{v}" for k, v in (p.get("agent_weights") or {}).items()) or "—",
            )

            col_toggle, col_delete = st.columns(2)
            with col_toggle:
                toggle_label = "Отключить" if p["enabled"] else "Включить"
                if st.button(toggle_label, key=f"toggle_{p['id']}"):
                    try:
                        agent_policy.toggle_policy(policy_db, p["id"], enabled=not p["enabled"])
                        st.rerun()
                    except PolicyNotFound as e:
                        st.error(str(e))
            with col_delete:
                if st.button("Удалить", key=f"delete_{p['id']}"):
                    try:
                        agent_policy.delete_policy(policy_db, p["id"])
                        st.rerun()
                    except PolicyNotFound as e:
                        st.error(str(e))


# ---------------------------------------------------------------------------
# Tab: create policy
# ---------------------------------------------------------------------------


def _render_create(policy_db: Path) -> None:
    agent_ids = _known_agent_ids()

    with st.form("create_agent_policy"):
        name = st.text_input("Название *")
        description = st.text_area("Описание", height=60)

        col1, col2 = st.columns(2)
        with col1:
            task_type = st.text_input(
                "Task type (пусто/`*` = любой)",
                value=agent_policy.ANY,
                help="Например: implementation, verification_review. '*' — любой тип задачи.",
            )
        with col2:
            priority = st.selectbox(
                "Приоритет", [agent_policy.ANY, *models.TASK_PRIORITIES], index=0
            )

        st.markdown("**Fallback-цепочка** (порядок = приоритет выбора; первый — основной агент)")
        fallback_agents = st.multiselect("Агенты по порядку", agent_ids, default=[])

        st.markdown("**Явные веса агентов** (переопределяют вес из fallback-цепочки)")
        weight_rows = st.data_editor(
            [{"agent_id": "", "weight": 0} for _ in range(1)],
            num_rows="dynamic",
            key="agent_weight_editor",
            column_config={
                "agent_id": st.column_config.SelectboxColumn(
                    "agent_id", options=[""] + agent_ids
                ),
                "weight": st.column_config.NumberColumn("weight", step=1),
            },
        )

        sla_str = st.text_input("SLA, секунд (пусто = без SLA)")

        submitted = st.form_submit_button("Создать политику")
        if submitted:
            if not name.strip():
                st.error("Укажите название.")
                return
            agent_weights = {
                row["agent_id"]: int(row["weight"])
                for row in (weight_rows or [])
                if row.get("agent_id")
            }
            sla_seconds = float(sla_str) if sla_str.strip() else None
            try:
                policy = agent_policy.create_policy(
                    policy_db,
                    name=name.strip(),
                    description=description.strip() or None,
                    task_type=task_type.strip() or agent_policy.ANY,
                    priority=priority,
                    agent_weights=agent_weights or None,
                    fallback_agents=list(fallback_agents) or None,
                    sla_seconds=sla_seconds,
                )
                st.success(
                    f"Политика создана: {policy['id'][:8]} — {policy['name']}. "
                    "Изменения активны немедленно, без деплоя."
                )
            except AgentPolicyError as e:
                st.error(str(e))


# ---------------------------------------------------------------------------
# Tab: test/preview effective policy
# ---------------------------------------------------------------------------


def _render_test(policy_db: Path) -> None:
    st.subheader("Проверить эффективную политику для типа задачи/приоритета")

    col1, col2 = st.columns(2)
    with col1:
        task_type = st.text_input("Task type для проверки", value="implementation")
    with col2:
        priority = st.selectbox("Приоритет для проверки", list(models.TASK_PRIORITIES))

    if st.button("Проверить"):
        policy = agent_policy.resolve_effective_policy(
            policy_db, task_type=task_type, priority=priority
        )
        if policy is None:
            st.info("Ни одна активная политика не совпадает — используется поведение по умолчанию.")
            return
        st.success(f"Применяется политика: {policy['name']} (id={policy['id'][:8]})")

        weights = agent_policy.effective_weights(policy_db, task_type=task_type, priority=priority)
        sla = agent_policy.sla_seconds_for(policy_db, task_type=task_type, priority=priority)

        st.write("**Итоговые веса агентов:**", weights or "— (без изменений)")
        st.write("**SLA:**", f"{sla} сек" if sla is not None else "— (не задан)")

        base_registry = scheduler.default_registry()
        tuned = agent_policy.apply_agent_weights(base_registry, weights)
        st.write("**Порядок агентов после применения (по убыванию веса):**")
        st.table(
            [
                {"agent_id": spec.agent_id, "weight": spec.weight, "available": spec.available}
                for spec in tuned.all()
            ]
        )
