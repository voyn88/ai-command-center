"""Workspace Home page and planning intelligence extracted from ``app.py``
(NIGHT-W9-AICC-ARCH slice 6).

Thin renderer over ``command_center.workspace_home``'s snapshot plus the
project planning-intelligence panel and its quick actions. No business logic
beyond ``st.*`` calls; every field shown for BANK/LEGAL is already redacted
by ``build_workspace_home_snapshot`` before it reaches here (see
WORKSPACE_HOME_ARCHITECTURE.md §5.1/§13 — this renderer is not the security
boundary and never receives the data that would need redacting). Pure move,
no behavior change; ``app.py`` re-exports the page renderers by assignment.
"""

from __future__ import annotations

import streamlit as st

from command_center import models, project_config, workspace_home
from command_center.runtime import api as runtime_api
from command_center.ui import (
    backlog_reconcile_panel,
    project_intelligence_panel,
    project_selector,
    recommendations_panel,
)
from command_center.ui.agent_launcher import ROOT
from command_center.ui.execution_center_form import EXECUTION_CENTER_STATE_LABELS
from command_center.ui.legacy_task_helpers import upsert_tasks

_WORKTREE_STATE_LABELS: dict[str, str] = {
    "unconfigured": "Путь к репозиторию не настроен",
    "invalid_path": "Путь недействителен (не существует)",
    "not_git_repo": "Путь не является git-репозиторием",
    "ok": "OK",
}


def _run_badge(run: dict) -> str:
    state = run.get("state") or run.get("status") or "—"
    label = EXECUTION_CENTER_STATE_LABELS.get(state, state) if run.get("source") == "v2" else state
    return f"[{run.get('source', '—')}] {label}"


def _quick_action_open_project(project_id: str) -> None:
    st.session_state.pending_nav = "projects"
    st.session_state.pending_project_browser = project_id


def _quick_action_new_task(project_id: str) -> None:
    st.session_state.pending_nav = "create"
    st.session_state.pending_create_project = project_id


def _quick_action_launch_run(project_id: str) -> None:
    st.session_state.pending_nav = "execution_center"
    st.session_state.pending_exec_center_project = project_id


def _quick_action_view_run(source: str, run_id: str) -> None:
    if source == "v2":
        st.session_state.pending_nav = "execution_center"
        st.session_state.pending_exec_center_run = run_id
    else:
        st.session_state.pending_nav = "runs"


def render_project_planning_intelligence(
    api: runtime_api.ExecutionCenterAPI,
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
    *,
    selector_key: str,
    recommendation_key_prefix: str,
    backlog_reconcile_key_prefix: str | None = None,
) -> str | None:
    """Render the shared founder health/recommendation surface.

    Workspace Home and Kanban deliberately delegate to the same component
    functions here so their metrics, scoring, queue state, and launch behavior
    cannot drift into separate implementations: the numbers come from
    `project_intelligence.compute_project_intelligence` and the cards from
    `recommendation_service.build_recommendation_views` on *both* pages, so
    there is exactly one implementation of each to keep correct.

    Only the Streamlit widget-key namespace differs per host page — each caller
    passes its own prefixes so the two pages' pills/buttons never collide on
    widget identity. `backlog_reconcile_key_prefix` is opt-in: backlog
    reconciliation is a Kanban-only planning tool, not part of the founder
    health/recommendation surface Workspace Home is meant to mirror.
    """
    project_filter = project_selector.render_project_selector(tasks, key=selector_key)
    project_intelligence_panel.render_project_intelligence_strip(tasks, project=project_filter)
    st.divider()

    project_configs = project_config.load_project_configs()
    with st.expander(
        "Планирование и рекомендации",
        icon=":material/auto_awesome:",
        expanded=False,
    ):
        recommendations_panel.render_recommendations_panel(
            tasks,
            tasks_by_id,
            ROOT,
            api,
            project_configs,
            upsert_tasks,
            project=project_filter,
            key_prefix=recommendation_key_prefix,
        )
        if backlog_reconcile_key_prefix is not None:
            backlog_reconcile_panel.render_backlog_reconcile_panel(
                tasks,
                ROOT,
                project=project_filter,
                key_prefix=backlog_reconcile_key_prefix,
            )
    return project_filter


def render_workspace_home_page(
    api: runtime_api.ExecutionCenterAPI,
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
) -> None:
    snapshot = workspace_home.build_workspace_home_snapshot(execution_center_api=api)

    render_project_planning_intelligence(
        api,
        tasks,
        tasks_by_id,
        selector_key="workspace_home_project_selector",
        recommendation_key_prefix="workspace_home_reco",
    )
    st.divider()

    with st.container(horizontal=True):
        st.metric("Проекты", len(snapshot["projects"]), border=True)
        st.metric("Активные прогоны", len(snapshot["active_runs"]), border=True)
        st.metric("Открытые задачи (v2)", sum(p["task_count"] for p in snapshot["projects"]), border=True)
        st.metric("Артефакты", len(snapshot["artifacts"]), border=True)
        st.metric("Отчёты", len(snapshot["reports"]), border=True)

    st.divider()
    st.markdown("#### Проекты")

    for project in snapshot["projects"]:
        project_id = project["id"]
        worktree_info = snapshot["worktrees_by_project"].get(project_id, {"state": "unconfigured", "worktrees": []})
        with st.container(border=True):
            header_cols = st.columns([3, 1, 1, 1])
            badge = " · 🔒 Чувствительный" if project["sensitive"] else ""
            header_cols[0].markdown(f"**{project['display_name']}**{badge}")
            header_cols[1].metric("Задачи (v2)", project["task_count"])
            header_cols[2].metric("Активные прогоны", project["active_run_count"])
            header_cols[3].caption(_WORKTREE_STATE_LABELS.get(worktree_info["state"], worktree_info["state"]))

            if worktree_info["state"] == "ok":
                for worktree in worktree_info["worktrees"][:5]:
                    st.caption(f"🌿 {worktree.get('branch', '—')} · `{worktree.get('head', '—')}`")
            elif worktree_info["state"] != "unconfigured":
                st.warning(_WORKTREE_STATE_LABELS.get(worktree_info["state"], worktree_info["state"]))

            action_cols = st.columns(3)
            with action_cols[0]:
                if st.button("Открыть", key=f"home_open_{project_id}", icon=":material/folder_open:", width="stretch"):
                    _quick_action_open_project(project_id)
                    st.rerun()
            with action_cols[1]:
                if st.button("Новая задача", key=f"home_new_task_{project_id}", icon=":material/add_task:", width="stretch"):
                    _quick_action_new_task(project_id)
                    st.rerun()
            with action_cols[2]:
                if st.button("Запустить прогон", key=f"home_launch_{project_id}", icon=":material/play_arrow:", width="stretch"):
                    _quick_action_launch_run(project_id)
                    st.rerun()

    st.divider()
    st.markdown("#### Активные прогоны")
    if not snapshot["active_runs"]:
        st.info("Активных прогонов нет.")
    else:
        for run in snapshot["active_runs"]:
            with st.container(border=True):
                cols = st.columns([3, 2, 2, 1])
                cols[0].write(f"**{run.get('project', '—')}** · {run.get('task_type', '—')}")
                cols[1].caption(_run_badge(run))
                cols[2].caption(f"Начат: {run.get('started_at') or '—'}")
                if cols[3].button(
                    "Открыть", key=f"home_view_active_{run.get('source')}_{run.get('run_id')}", width="stretch"
                ):
                    _quick_action_view_run(run.get("source"), run.get("run_id"))
                    st.rerun()

    st.markdown("#### Последние прогоны")
    if not snapshot["recent_runs"]:
        st.info("Прогонов пока нет.")
    else:
        for run in snapshot["recent_runs"][:10]:
            with st.container(border=True):
                cols = st.columns([3, 2, 2, 1])
                cols[0].write(f"**{run.get('project', '—')}** · {run.get('task_type', '—')}")
                cols[1].caption(_run_badge(run))
                cols[2].caption(f"Завершён: {run.get('completed_at') or '—'}")
                if cols[3].button(
                    "Открыть", key=f"home_view_recent_{run.get('source')}_{run.get('run_id')}", width="stretch"
                ):
                    _quick_action_view_run(run.get("source"), run.get("run_id"))
                    st.rerun()

    st.divider()
    left, right = st.columns(2)

    with left:
        st.markdown("#### Артефакты")
        if not snapshot["artifacts"]:
            st.info("Артефактов пока нет.")
        else:
            with st.container(border=True):
                for artifact in snapshot["artifacts"][:10]:
                    st.caption(f"{artifact.get('project', '—')} · {artifact.get('task_type') or '—'}")
            if st.button("Все артефакты", key="home_view_all_artifacts"):
                st.session_state.pending_nav = "generated"
                st.rerun()

    with right:
        st.markdown("#### Отчёты")
        if not snapshot["reports"]:
            st.info("Отчётов пока нет.")
        else:
            with st.container(border=True):
                for report in snapshot["reports"][:10]:
                    verdict = models.VERDICT_LABELS.get(report.get("verdict"), report.get("verdict") or "не определён")
                    st.caption(f"{report.get('project', '—')} · {verdict}")
            if st.button("Все отчёты", key="home_view_all_reports"):
                st.session_state.pending_nav = "reports"
                st.rerun()

    st.divider()
    st.markdown("#### Последняя активность")
    if not snapshot["recent_activity"]:
        st.info("Активности пока нет.")
    else:
        with st.container(border=True):
            for event in snapshot["recent_activity"][:15]:
                st.caption(f"{event.get('ts', '—')} — {event.get('project', '—')} — {event.get('event_type', '—')}")
