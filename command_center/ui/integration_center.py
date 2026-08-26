"""Integration Center — the "Projects" surface (AICC-INT-001, increment 1).

Rendering only, per the ``command_center.ui`` boundary: the registry comes
from ``integration.registry``, health from ``integration.collectors``
(strictly read-only), open tasks from the ``tasks`` list the caller already
loaded through ``tasks_repository``, and recent runs from the unified read
model. Health collection is on demand (button), cached in
``st.session_state`` so Streamlit reruns never hammer ``git``/``gh``.
"""

from __future__ import annotations

import streamlit as st

from command_center.integration import collectors, registry

_HEALTH_CACHE_KEY = "integration_center_health"

_KIND_LABELS: dict[str, str] = {
    "application": "Application",
    "service": "Service",
    "library": "Library",
    "infrastructure": "Infrastructure",
    "other": "Other",
}

_WORKTREE_BADGES: dict[str, tuple[str, str]] = {
    "ok": ("Checkout OK", "green"),
    "unconfigured": ("Путь не настроен", "gray"),
    "invalid_path": ("Путь не существует", "red"),
    "not_git_repo": ("Не git-репозиторий", "red"),
}

_CI_BADGES: dict[str, tuple[str, str]] = {
    "success": ("CI: зелёный", "green"),
    "failure": ("CI: красный", "red"),
    "in_progress": ("CI: выполняется", "blue"),
    "cancelled": ("CI: отменён", "orange"),
    "unknown": ("CI: неизвестно", "gray"),
}


def _cached_health() -> dict[str, dict]:
    cache = st.session_state.get(_HEALTH_CACHE_KEY)
    return cache if isinstance(cache, dict) else {}


def _collect_all(entries: list[dict]) -> None:
    st.session_state[_HEALTH_CACHE_KEY] = {
        entry["id"]: collectors.collect_health(entry) for entry in entries
    }


def _open_tasks_for(entry: dict, tasks: list[dict]) -> list[dict]:
    return [
        task
        for task in tasks
        if task.get("project") == entry["project"] and task.get("status") != "Done"
    ]


def _render_entry_card(entry: dict, health: dict | None, open_task_count: int) -> None:
    with st.container(border=True):
        st.markdown(f"#### {entry['name']}")
        st.caption(
            f"`{entry['id']}` · {_KIND_LABELS.get(entry['kind'], entry['kind'])} · "
            f"namespace задач: `{entry['project']}` · repo: `{entry.get('repo_path') or '—'}`"
        )
        with st.container(horizontal=True):
            if health is None:
                st.badge("Статус не собран", color="gray")
            else:
                label, color = _WORKTREE_BADGES.get(
                    health["worktree_state"], (health["worktree_state"], "gray")
                )
                st.badge(label, color=color)
                git = health.get("git") or {}
                if git.get("available"):
                    st.badge(
                        "Изменения" if git.get("dirty") else "Чисто",
                        color="orange" if git.get("dirty") else "green",
                        icon=":material/commit:",
                    )
                    if git.get("branch"):
                        st.badge(str(git["branch"]), color="gray", icon=":material/fork_right:")
                github = health.get("github") or {}
                if github.get("available"):
                    ci_label, ci_color = _CI_BADGES.get(
                        github.get("ci_state") or "unknown", _CI_BADGES["unknown"]
                    )
                    st.badge(ci_label, color=ci_color)
                    if github.get("open_pr_count") is not None:
                        st.badge(
                            f"Open PRs: {github['open_pr_count']}",
                            color="blue",
                            icon=":material/merge:",
                        )
                elif health["worktree_state"] == "ok":
                    st.badge("GitHub: недоступен", color="gray")
            st.badge(f"Открытых задач: {open_task_count}", color="violet", icon=":material/task:")
        if health is not None:
            git = health.get("git") or {}
            if git.get("available") and git.get("last_activity"):
                st.caption(
                    f"Последняя активность: {git['last_activity']} — "
                    f"{(git.get('last_commit_subject') or '')[:80]}"
                )


def _render_drilldown(entry: dict, tasks: list[dict], runs: list[dict]) -> None:
    st.markdown(f"### {entry['name']} — детали")

    st.markdown("**Открытые задачи**")
    open_tasks = _open_tasks_for(entry, tasks)
    if not open_tasks:
        st.info("Открытых задач для этого проекта нет.")
    for task in open_tasks[:30]:
        st.caption(
            f"- {(task.get('title') or 'Без названия')[:80]} · {task.get('status')} · "
            f"приоритет {task.get('priority', 'Medium')}"
        )

    st.markdown("**Последние запуски**")
    project_runs = [run for run in runs if run.get("project") == entry["project"]][:10]
    if not project_runs:
        st.info("Запусков для этого проекта пока нет.")
    for run in project_runs:
        st.caption(
            f"- `{(run.get('id') or '')[:8]}` · {run.get('task_type') or '—'} · "
            f"{run.get('status') or '—'} · {run.get('created_at') or '—'}"
        )


def render_integration_center(tasks: list[dict], runs: list[dict]) -> None:
    """The Projects page: registry list with health badges + drill-down.

    ``tasks`` is the already-loaded task board (``tasks_repository`` shape);
    ``runs`` the unified runs list (``runs_read.list_unified_runs`` shape).
    Read-only: this page never writes the registry, tasks or runs.
    """
    st.subheader("Integration Center", anchor="integration")
    st.caption(
        "Единый пункт управления всеми проектами экосистемы: состояние "
        "checkout, CI, PR и задачи AICC по каждому репозиторию. "
        "Только чтение — сбор статуса не изменяет репозитории."
    )

    entries = registry.load_entries()

    if st.button("Собрать статус", icon=":material/refresh:", key="integration_collect"):
        with st.spinner("Собираю git/gh статус по всем проектам..."):
            _collect_all(entries)

    health_by_id = _cached_health()
    for entry in entries:
        _render_entry_card(
            entry,
            health_by_id.get(entry["id"]),
            open_task_count=len(_open_tasks_for(entry, tasks)),
        )

    st.divider()
    choice = st.selectbox(
        "Проект для детализации",
        ["—", *[entry["id"] for entry in entries]],
        key="integration_drilldown",
    )
    if choice != "—":
        entry = next(e for e in entries if e["id"] == choice)
        _render_drilldown(entry, tasks, runs)
