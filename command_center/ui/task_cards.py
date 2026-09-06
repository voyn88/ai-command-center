"""Task card widgets extracted from ``app.py`` (NIGHT-W9-AICC-ARCH slice 4).

The shared Task Card component (Title/Progress/Stage/Project/Executor/
Repository/Workspace/Branch/Git/PR/Tests + action row) used by the Kanban
board and Focus Mode, plus its timeline/dependency-graph sub-renders and the
advisory "next task" callout — formerly inline in ``app.py``. Pure move, no
behavior change; ``app.py`` re-exports every name by assignment.

Boundary (see this package's ``__init__``): rendering only. Pure task-card
read-model logic stays in ``command_center.task_view`` (see
``docs/adr/0001-engineering-control-center-v2-increment-1.md``); task
persistence goes through ``ui.legacy_task_helpers`` → ``tasks_repository``
(single writer of ``data/tasks.json`` per ``docs/AUTHORITY_MAP.md``); the
``ExecutionCenterAPI`` singleton is reached through ``ui.agent_launcher``'s
injected accessor (``app.py`` owns the ``st.cache_resource`` seam — no second
Supervisor/engine here).
"""

from __future__ import annotations

import streamlit as st

from command_center import (
    agent_runner,
    artifacts,
    execution_queue,
    executors,
    launch,
    models,
    recommend,
    task_view,
    tasks_repository,
)
from command_center.runtime import api as runtime_api
from command_center.runtime import session_view
from command_center.ui import (
    agent_launcher,
    confirm_dialog,
    inspector,
    legacy_task_helpers,
    tokens,
)
from command_center.ui.agent_launcher import (
    ROOT,
    TASK_TYPE_LABELS,
    _get_execution_center_api,
)

# Canonical sources (no duplicate vocabularies here — same rule app.py follows).
PRIORITY_COLORS: dict[str, str] = tokens.PRIORITY_COLORS
LAUNCH_STATUS_COLORS: dict[str, str] = tokens.LAUNCH_STATUS_COLORS
KANBAN_COLUMNS: list[str] = models.KANBAN_STATUSES


def format_estimate(hours: float) -> str:
    return f"{int(hours)}ч" if hours == int(hours) else f"{hours:g}ч"


# Pure task-card read-model logic lives in `command_center.task_view` — see
# `docs/adr/0001-engineering-control-center-v2-increment-1.md`. These
# render_* functions only turn its plain-data output into widgets.


def _set_launch_status(task_id: str, status: str, note: str) -> None:
    tasks_repository.set_manual_launch_status(ROOT, task_id, status, note)


def _render_manual_merge_button(
    api: runtime_api.ExecutionCenterAPI,
    completion: dict | None,
    *,
    key: str,
) -> None:
    if not session_view.manual_merge_available(completion):
        return
    if st.button(
        "Сделать мердж",
        key=key,
        type="primary",
        icon=":material/merge:",
        help="Слияния выполняются строго последовательно; CI, ревью и конфликты проверяются повторно.",
    ):
        try:
            updated = api.request_manual_merge(
                completion["run_id"], confirmed=True
            )
        except Exception as exc:  # noqa: BLE001 - gate refusal belongs in the UI
            st.error(f"Мердж не выполнен: {exc}")
        else:
            if updated and updated.get("completion_state") == "COMPLETED":
                st.success("PR смёржен и подтверждён в целевой ветке.")
            else:
                st.success("PR смёржен; проверяется целевая ветка.")
            st.rerun()


def render_task_timeline(task: dict) -> None:
    events = task_view.sorted_timeline(task)
    if not events:
        st.caption("История ещё пуста.")
        return
    for event in events:
        st.caption(f"`{event.get('ts', '—')}` · **{event.get('type', '—')}** — {event.get('message', '')}")


def render_dependency_graph(task: dict, tasks_by_id: dict[str, dict]) -> None:
    dot = task_view.dependency_graph_dot(task, tasks_by_id)
    if dot is None:
        st.caption("Нет связанных задач.")
        return
    st.graphviz_chart(dot)


def render_task_card(
    task: dict,
    *,
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
    key_prefix: str,
    git_status_cache: dict[str, dict],
    completion: dict | None = None,
    live_progress: tuple[int | None, str | None] | None = None,
    show_kanban_controls: bool = False,
) -> None:
    task_id = task.get("id")
    title = task.get("title") or "Без названия"
    workspace_path = task.get("workspace_path") or task.get("repository_path")

    with st.container(border=True):
        st.markdown(f"### {title}")
        st.caption(f"{task.get('project')} · {TASK_TYPE_LABELS.get(task.get('task_type'), task.get('task_type'))}")

        live_percent, live_stage = live_progress or (None, None)
        progress = (
            int(live_percent)
            if live_percent is not None
            else int(task.get("progress") or 0)
        )
        stage = (
            live_stage
            or task.get("current_stage")
            or models.EXECUTION_STAGES[0]
        )
        st.progress(progress / 100, text=f"{stage} — {progress}%")

        # Three distinct, visually separated clusters — planning state
        # (this Kanban lane, owned by the user), current execution state
        # (`launch_status`, synced live from `runtime.db` per ADR 0003 —
        # never a manual Kanban lane), and dependency readiness (derived,
        # never stored) — deliberately never merged into one badge row, so
        # "what lane is this in," "is an agent actually running against it
        # right now," and "is it blocked on something else" each read as
        # their own answer instead of one ambiguous chip soup.
        with st.container(horizontal=True):
            priority = task.get("priority", "Medium")
            st.badge(priority, color=PRIORITY_COLORS.get(priority, "blue"))
            if task.get("owner"):
                st.badge(task["owner"], color="gray", icon=":material/person:")
            if task.get("estimate_hours"):
                st.badge(format_estimate(task["estimate_hours"]), color="gray", icon=":material/schedule:")

        with st.container(horizontal=True):
            launch_status = task.get("launch_status") or "Ready"
            running = launch_status == "Running"
            st.badge(
                f"⏺ {launch_status}" if running else launch_status,
                color=LAUNCH_STATUS_COLORS.get(launch_status, "gray"),
            )
            executor_id = task.get("executor")
            if executor_id:
                st.badge(executors.get_executor(executor_id).label, color="blue", icon=":material/smart_toy:")
            if task.get("branch"):
                st.badge(task["branch"], color="gray", icon=":material/fork_right:")
            if task.get("current_run_id"):
                st.caption(f"run `{task['current_run_id'][:8]}` · Live Execution Center")

        blocked = models.is_blocked(task, tasks_by_id)
        if blocked:
            with st.container(horizontal=True):
                st.badge("Заблокировано", color="red", icon=":material/block:")
                unmet_names = ", ".join(
                    (tasks_by_id.get(dep_id, {}).get("title") or dep_id)
                    for dep_id in models.unmet_dependencies(task, tasks_by_id)
                )
                st.caption(f"Ожидает: {unmet_names}")
        elif task.get("depends_on"):
            st.badge("Зависимости выполнены", color="green", icon=":material/check_circle:")

        git_status = task_view.cached_git_status(workspace_path, git_status_cache)
        with st.container(horizontal=True):
            if git_status.get("is_repo"):
                dirty = git_status.get("dirty")
                st.badge("Изменения" if dirty else "Чисто", color="orange" if dirty else "green", icon=":material/commit:")
            if task.get("pull_request_url"):
                st.link_button("PR", task["pull_request_url"], icon=":material/merge:")
            if task.get("latest_verdict"):
                passing = models.is_passing_verdict(task["latest_verdict"])
                st.badge(
                    models.VERDICT_LABELS.get(task["latest_verdict"], task["latest_verdict"]),
                    color="green" if passing else "red",
                )

        # Inspector select (UX-2c): one-tap to load this task into the
        # top-bar Inspector pane without opening the full dialog.
        with st.container(horizontal=True):
            if st.button("🔍 В инспектор", key=f"{key_prefix}_inspect", icon=":material/search:", help="Открыть в Инспекторе"):
                inspector.select_task(task_id)
                st.rerun()

        _render_manual_merge_button(
            _get_execution_center_api(),
            completion,
            key=f"{key_prefix}_manual_merge",
        )

        with st.expander("Действия", icon=":material/tune:"):
            st.caption(f"ID: `{task_id}` · Создано: {task.get('created_at', '—')} · Обновлено: {task.get('updated_at', '—')}")
            st.caption(
                f"Стадия workflow: "
                f"{models.WORKFLOW_STAGE_LABELS.get(task.get('workflow_stage'), task.get('workflow_stage') or '—')}"
            )
            if task.get("goal"):
                st.caption(f"Цель: {task['goal']}")
            if task.get("notes"):
                st.caption(f"Заметки: {task['notes']}")
            st.caption(f"Репозиторий: `{task.get('repository_path') or '—'}` · Workspace: `{workspace_path or '—'}`")

            if task.get("read_only"):
                # A master-projection record is a window into the canonical
                # backlog, not an operable console task: no launch, no queue,
                # no manual status — the fleet's own pipeline is its only
                # writer (VOYN-W0-AICC-WIRE-BACKLOG-API; save_tasks drops
                # such records structurally even if a path slipped through).
                st.info(
                    "Задача центрального бэклога (read-only): исполняется "
                    "флотом, управление — через конвейер, не через консоль."
                )
                return

            action_cols = st.columns(5)
            with action_cols[0]:
                if st.button("Workspace", key=f"{key_prefix}_action_workspace", icon=":material/folder_open:"):
                    if workspace_path:
                        ok_action, message_action = launch.open_folder_at(workspace_path)
                        (st.success if ok_action else st.error)(message_action)
                    else:
                        st.error("Workspace не настроен.")
            with action_cols[1]:
                git_open = st.button("Git", key=f"{key_prefix}_action_git", icon=":material/commit:")
            with action_cols[2]:
                if st.button("Промпт", key=f"{key_prefix}_action_prompt", icon=":material/content_copy:"):
                    prompt_text = task.get("prompt") or task.get("goal") or ""
                    ok_action, message_action = launch.copy_to_clipboard(prompt_text)
                    (st.success if ok_action else st.error)(message_action)
            with action_cols[3]:
                report_open = st.button("Отчёт", key=f"{key_prefix}_action_report", icon=":material/description:")
            with action_cols[4]:
                if st.button("В очередь", key=f"{key_prefix}_action_queue", icon=":material/playlist_add:"):
                    # Lost-update-safe: the whole load→enqueue→save cycle runs
                    # under `queue_lock` so a concurrent writer's queue change is
                    # never clobbered by a stale snapshot (see execution_queue).
                    execution_queue.enqueue_and_persist(ROOT, task, tasks_by_id)
                    st.success("Добавлено в очередь запуска.")

            if git_open:
                if workspace_path and git_status.get("is_repo"):
                    st.write(f"Ветка: `{git_status.get('branch')}`")
                    st.write(f"Последний коммит: `{git_status.get('last_commit_hash')}` — {git_status.get('last_commit_subject')}")
                    st.write(f"Изменено файлов: {git_status.get('modified_count', 0)}, неотслеживаемых: {git_status.get('untracked_count', 0)}")
                else:
                    st.warning("Workspace не является git-репозиторием или не настроен.")

            if report_open:
                if task.get("report_path"):
                    report_full_path = agent_runner.resolve_report_path(task)
                    if report_full_path is None:
                        st.warning("Путь к отчёту не проходит проверку безопасности — файл не открыт.")
                    elif report_full_path.exists():
                        st.code(artifacts.read_text(report_full_path), language="markdown")
                    else:
                        st.caption("Файл отчёта не найден на диске.")
                else:
                    st.caption("Отчёт ещё не создан.")

            # "Ручной статус" — honest framing (UX analysis §3.5): these are
            # planning labels, NOT process control. Grouped under a caption that
            # says so, and localized, so the row no longer reads as a media-style
            # transport that can pause a running agent (it cannot). Real
            # cancellation lives only on the run card in the Execution Center.
            st.markdown("**Ручной статус** (метка плана, не управление процессом)")
            status_cols = st.columns(3)
            with status_cols[0]:
                if st.button("Приостановить", key=f"{key_prefix}_action_pause", icon=":material/pause:"):
                    _set_launch_status(task_id, "Requires Attention", "Отмечено как приостановлено (вручную).")
                    st.rerun()
            with status_cols[1]:
                if st.button("Возобновить", key=f"{key_prefix}_action_resume", icon=":material/play_arrow:"):
                    _set_launch_status(task_id, "Ready", "Отмечено как возобновлено (вручную).")
                    st.rerun()
            with status_cols[2]:
                if st.button("К перезапуску", key=f"{key_prefix}_action_restart", icon=":material/restart_alt:"):
                    _set_launch_status(task_id, "Ready", "Отмечено для перезапуска (вручную).")
                    st.rerun()
            st.caption(
                "Это статус-метки для планирования, а не управление процессом: "
                "синхронный запуск Claude Code нельзя приостановить на лету. "
                "Реальная отмена прогона — на карточке в «Live Execution Center»."
            )

            st.divider()
            st.markdown("**Запуск**")
            agent_launcher.render_agent_launcher(
                key_prefix=f"{key_prefix}_launch",
                project=task.get("project"),
                default_prompt=task.get("prompt") or task.get("goal") or title,
                tasks=tasks,
                task_id=task_id,
                default_task_type=task.get("task_type", "implementation"),
            )

            st.divider()
            st.markdown("**История**")
            render_task_timeline(task)

            st.markdown("**Зависимости**")
            deps = task.get("depends_on") or []
            if deps:
                for dep_id in deps:
                    dep = tasks_by_id.get(dep_id)
                    label = f"{(dep.get('title') or '')[:50]} ({dep.get('status')})" if dep else f"(удалена) {dep_id}"
                    st.caption(f"- {label}")
            render_dependency_graph(task, tasks_by_id)

        if show_kanban_controls:
            current_status = task.get("status", KANBAN_COLUMNS[0])
            status_options = task_view.kanban_status_options(current_status)
            new_status = st.selectbox(
                "Статус",
                status_options,
                index=status_options.index(current_status),
                key=f"{key_prefix}_status_select",
                label_visibility="collapsed",
            )
            if new_status != current_status:
                legacy_task_helpers.update_task_status(task_id, new_status)
                st.rerun()

            delete_key_prefix = f"{key_prefix}_delete"
            st.session_state.setdefault(f"{delete_key_prefix}_confirm_open", False)
            if st.button("Удалить", key=f"{key_prefix}_delete", icon=":material/delete:", width="stretch"):
                confirm_dialog.open_confirmation(delete_key_prefix)

            confirm_dialog.render_destructive_confirmation(
                key_prefix=delete_key_prefix,
                dialog_title="Подтверждение удаления",
                warning=f"Задача «{title}» (`{task_id}`) будет удалена. Это действие нельзя отменить.",
                checkbox_label="Я подтверждаю удаление этой задачи.",
                confirm_label="Подтвердить удаление",
                on_confirm=lambda: legacy_task_helpers.delete_task(
                    task_id,
                    # Same runtime.db cascade app.py's own delete_task shim
                    # performs (audit AR-1), through the one API singleton.
                    on_deleted=lambda tid: _get_execution_center_api().delete_task(tid),
                ),
            )


def render_next_task_callout(tasks: list[dict], project: str | None = None, *, active_runs: list[dict] | None = None) -> None:
    """Non-invasive '➡ Next Task' recommendation, always with an explanation
    of *why* — see `command_center.recommend.recommend_next_task`. Never
    creates, launches, or modifies anything; purely advisory.

    ``active_runs`` (runtime.db runs filtered to active states) makes the
    "исполнитель занят" reason agree with the Launch Gate instead of the
    lagging Kanban ``launch_status``; see ``recommend._score_candidates``."""
    recommendation = recommend.recommend_next_task(tasks, project=project, active_runs=active_runs)
    if recommendation is None:
        st.info("➡ Следующая задача: нет открытых незаблокированных задач.")
        return

    task = recommendation.task
    with st.container(border=True):
        st.markdown(f"##### ➡ Следующая задача: {task.get('title') or 'Без названия'}")
        st.caption(f"{task.get('project')} · {task.get('status')} · приоритет {task.get('priority', 'Medium')}")
        st.caption("Почему: " + "; ".join(recommendation.reasons))
