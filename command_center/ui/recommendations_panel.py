"""Renders the upgraded "Recommended Next Tasks" surface: rationale,
dependencies, impact, readiness, and a direct Launch-now or Add-to-Queue
action per recommendation — over `command_center.recommendation_service`
(itself a thin decorator around the unmodified `command_center.recommend`
scoring engine)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import streamlit as st

from command_center import execution_queue, recommendation_service, tasks_repository
from command_center.runtime import db as runtime_db
from command_center.ui.launch_feedback import render_skipped_launch

_QUEUED_STATE_LABELS: dict[str, str] = {
    execution_queue.STATE_WAITING: "В очереди · ожидает",
    execution_queue.STATE_READY: "В очереди · готово",
}


def render_recommendations_panel(
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
    root: Path,
    execution_center_api,
    project_configs: dict[str, dict],
    save_tasks_fn: Callable[[list[dict]], None],
    *,
    project: str | None = None,
    limit: int = 3,
    key_prefix: str = "reco",
) -> None:
    queue_entries = execution_queue.reevaluate_and_persist(root, tasks_by_id)
    active_runs = execution_center_api.list_runs(states=runtime_db.EXECUTION_CENTER_ACTIVE_STATES)
    views = recommendation_service.build_recommendation_views(
        tasks, tasks_by_id, project=project, queue_entries=queue_entries, limit=limit, active_runs=active_runs
    )

    st.markdown("#### Рекомендованные задачи")

    # See `queue_panel.render_execution_queue_panel`'s identical comment: a
    # message rendered here and immediately followed by `st.rerun()` (as the
    # "Запустить" button below used to do) is discarded by that rerun before
    # the browser ever shows it — this is what made a *blocked* launch (e.g.
    # the AIOS repository's normal dirty working tree) look like the button
    # did nothing at all. Stashing the outcome in `session_state` and
    # rendering it on the next run, before the (possibly now-empty, if the
    # task launched and dropped off this list) `views`, keeps it visible.
    flash_key = f"{key_prefix}_launch_flash"
    flash = st.session_state.pop(flash_key, None)
    if flash:
        if flash["launched"]:
            st.success(f"Запуск начат: `{flash['run_id']}`.")
        else:
            render_skipped_launch(
                "Не удалось запустить сразу",
                message=flash["message"],
                warnings=flash["warnings"],
                validation_report=flash["validation_report"],
            )

    if not views:
        st.info("Нет рекомендованных незаблокированных задач.")
        return

    columns = st.columns(len(views))
    for column, view in zip(columns, views, strict=True):
        task = tasks_by_id.get(view["task_id"])
        with column, st.container(border=True):
            st.markdown(f"**{view['title']}**")
            st.caption(f"{view['project']} · приоритет {view['priority']}")
            st.caption("Почему: " + "; ".join(view["reasons"]))

            if view["dependencies"]:
                dep_summary = ", ".join(
                    f"{dep['title'] or dep['id']} ({'готово' if dep['done'] else dep['status'] or '?'})"
                    for dep in view["dependencies"]
                )
                st.caption(f"Зависимости: {dep_summary}")
            else:
                st.caption("Зависимости: нет")

            st.caption(
                f"Влияние: разблокирует {view['impact']} задач(и)"
                if view["impact"]
                else "Влияние: не блокирует другие задачи"
            )
            st.caption("Готовность: " + ("готово к запуску" if view["ready"] else "заблокировано"))

            queued_label = _QUEUED_STATE_LABELS.get(view["queued_state"] or "")
            if queued_label:
                st.caption(queued_label)

            if task is not None and tasks_repository.is_master_projection_task(task):
                # A master-projection record is a read-only view of the
                # canonical backlog (`live_board.launch_gate` refuses it for
                # the same reason): no queue, no launch, the fleet's own
                # pipeline is its only writer.
                st.info(
                    "Задача центрального бэклога (read-only): исполняется "
                    "флотом, управление — через конвейер, не через консоль."
                )
                continue

            action_cols = st.columns(2)
            with action_cols[0]:
                queue_disabled = view["queued_state"] is not None
                if st.button(
                    "В очередь",
                    key=f"{key_prefix}_{view['task_id']}_enqueue",
                    disabled=queue_disabled,
                    width="stretch",
                ):
                    # Lost-update-safe: re-read and persist under `queue_lock`
                    # rather than saving back the `queue_entries` snapshot loaded
                    # earlier this render, which a concurrent writer may have
                    # since superseded (see execution_queue.enqueue_and_persist).
                    execution_queue.enqueue_and_persist(root, task, tasks_by_id)
                    st.rerun()

            with action_cols[1]:
                if st.button(
                    "Запустить",
                    key=f"{key_prefix}_{view['task_id']}_launch",
                    type="primary",
                    width="stretch",
                ):
                    working_entries = execution_queue.enqueue_and_persist(root, task, tasks_by_id)
                    new_entry = next(
                        e
                        for e in working_entries
                        if e.get("task_id") == view["task_id"] and e.get("state") in execution_queue.OPEN_STATES
                    )
                    _, results = execution_queue.launch_ready(
                        root,
                        working_entries,
                        tasks,
                        tasks_by_id,
                        project_configs,
                        execution_center_api,
                        entry_ids=[new_entry["id"]],
                    )
                    save_tasks_fn(tasks)
                    result = results[0] if results else None
                    st.session_state[flash_key] = {
                        "launched": bool(result and result.launched),
                        "run_id": result.run_id if result else None,
                        "message": (result.message if result else "запуск не выполнен"),
                        "warnings": list(result.warnings) if result else [],
                        "validation_report": result.validation_report if result else None,
                    }
                    st.rerun()
