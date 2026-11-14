"""Renders the Execution Queue: Waiting / Ready lists, a prominent "Launch
Ready" control, and an optional "launch next ready task" action — the only
two places in the app that ever call `execution_queue.launch_ready`. Nothing
in this module (or the `execution_queue` service it calls) launches anything
on a plain rerun; every launch here is the direct result of a button click
in this render, this run.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import streamlit as st

from command_center import execution_queue, project_config, recommend
from command_center.ui.launch_feedback import render_skipped_launch


_HISTORY_LIMIT = 25
_HISTORY_STATES: frozenset[str] = frozenset(
    {execution_queue.STATE_LAUNCHED, execution_queue.STATE_CANCELLED}
)


def _pick_next(ready_entries: list[dict], tasks_by_id: dict[str, dict]) -> dict | None:
    """Highest-priority ready entry, oldest first among ties — the single
    entry "Launch next ready task" acts on."""
    if not ready_entries:
        return None

    def sort_key(entry: dict) -> tuple[int, str]:
        task = tasks_by_id.get(entry.get("task_id"), {})
        priority = task.get("priority", "Medium")
        return (-recommend.PRIORITY_WEIGHT.get(priority, 1), entry.get("added_at") or "")

    return sorted(ready_entries, key=sort_key)[0]


def _terminal_history_entries(entries: list[dict], *, limit: int = _HISTORY_LIMIT) -> list[dict]:
    """Newest terminal queue entries (launched/cancelled), bounded for UI."""
    terminal = [entry for entry in entries if entry.get("state") in _HISTORY_STATES]

    def sort_key(entry: dict) -> str:
        return (
            entry.get("launched_at")
            or entry.get("evaluated_at")
            or entry.get("added_at")
            or ""
        )

    return sorted(terminal, key=sort_key, reverse=True)[:limit]


def render_execution_queue_panel(
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
    root: Path,
    execution_center_api,
    project_configs: dict[str, dict],
    save_tasks_fn: Callable[[list[dict]], None],
    *,
    project: str | None = None,
    key_prefix: str = "queue",
    entries: list[dict] | None = None,
    show_heading: bool = True,
) -> None:
    if entries is None:
        entries = execution_queue.reevaluate_and_persist(root, tasks_by_id)
    if project:
        # A queue entry copies its task's raw `project` (which may be a display
        # name), while `project` here is the canonical id emitted by the Kanban
        # selector — match on canonical ids (shared helper) so the queue on the
        # Kanban page shows the same project's tasks the lane above it does.
        entries = [e for e in entries if project_config.project_matches(e.get("project"), project)]

    waiting = execution_queue.waiting_entries(entries)
    ready = execution_queue.ready_entries(entries)
    terminal_history = _terminal_history_entries(entries)
    total_terminal = sum(1 for e in entries if e.get("state") in _HISTORY_STATES)

    if show_heading:
        st.markdown("#### Очередь запуска")

    # `launch_ready` runs synchronously right before the `st.rerun()` below,
    # in the *same* script pass as the button click — so a plain `st.success`/
    # `st.warning` call here would be created and then immediately discarded
    # by that rerun before ever reaching the browser (this was the actual
    # root cause of "launch button does nothing": every entry whose
    # `launch.validate_launch` reported a warning — e.g. a dirty working tree,
    # which is the AIOS repository's normal day-to-day state — was silently
    # skipped, and the message explaining *why* never survived long enough
    # to be seen). Stashing it in `session_state` and rendering it on the
    # *next* run (right here, before anything else) is what makes it durable
    # across that rerun.
    flash_key = f"{key_prefix}_launch_flash"
    flash = st.session_state.pop(flash_key, None)
    if flash:
        if flash["launched_count"]:
            st.success(f"Запущено: {flash['launched_count']}.")
        for skipped in flash["skipped"]:
            render_skipped_launch(
                f"Не запущено — {skipped['task_id']}",
                message=skipped["message"],
                warnings=skipped["warnings"],
                validation_report=skipped["validation_report"],
                details_label=skipped["task_id"],
            )

    if not waiting and not ready:
        st.caption("Активная очередь пуста.")
        if not terminal_history:
            st.caption("Добавьте задачу из рекомендаций или карточки Kanban.")
            return

    if waiting or ready:
        button_cols = st.columns(2)
        with button_cols[0]:
            launch_all_clicked = st.button(
                "🚀 Запустить готовые",
                key=f"{key_prefix}_launch_ready",
                type="primary",
                disabled=not ready,
                width="stretch",
            )
        with button_cols[1]:
            launch_next_clicked = st.button(
                "Запустить следующую готовую задачу",
                key=f"{key_prefix}_launch_next",
                disabled=not ready,
                width="stretch",
            )

        if launch_all_clicked or launch_next_clicked:
            entry_ids = None
            if launch_next_clicked:
                next_entry = _pick_next(ready, tasks_by_id)
                entry_ids = [next_entry["id"]] if next_entry else []
            _, results = execution_queue.launch_ready(
                root, entries, tasks, tasks_by_id, project_configs, execution_center_api, entry_ids=entry_ids
            )
            save_tasks_fn(tasks)
            launched = [r for r in results if r.launched]
            skipped = [r for r in results if not r.launched]
            st.session_state[flash_key] = {
                "launched_count": len(launched),
                "skipped": [
                    {
                        "task_id": r.task_id,
                        "message": r.message,
                        "warnings": list(r.warnings),
                        "validation_report": r.validation_report,
                    }
                    for r in skipped
                ],
            }
            st.rerun()

    if ready:
        st.caption(f"Готово к запуску · {len(ready)}")
        for entry in ready:
            task = tasks_by_id.get(entry.get("task_id"), {})
            row = st.columns([4, 1])
            row[0].caption(f"🟢 {task.get('title', entry.get('task_id'))} — {task.get('project', '—')}")
            if row[1].button("Убрать", key=f"{key_prefix}_remove_{entry['id']}"):
                execution_queue.dequeue_and_persist(root, entry["id"])
                st.rerun()

    if waiting:
        st.caption(f"Ожидает зависимостей · {len(waiting)}")
        for entry in waiting:
            task = tasks_by_id.get(entry.get("task_id"), {})
            row = st.columns([4, 1])
            row[0].caption(f"🟡 {task.get('title', entry.get('task_id'))} — {entry.get('reason') or '—'}")
            if row[1].button("Убрать", key=f"{key_prefix}_remove_{entry['id']}"):
                execution_queue.dequeue_and_persist(root, entry["id"])
                st.rerun()

    if terminal_history:
        with st.expander(f"История очереди · {total_terminal}", expanded=not waiting and not ready):
            for entry in terminal_history:
                task = tasks_by_id.get(entry.get("task_id"), {})
                title = task.get("title", entry.get("task_id"))
                if entry.get("state") == execution_queue.STATE_LAUNCHED:
                    st.caption(
                        f"✅ {title} · run `{entry.get('run_id') or '—'}` · {entry.get('launched_at') or entry.get('added_at') or '—'}"
                    )
                else:
                    st.caption(
                        f"⚪ {title} · отменено · {entry.get('reason') or 'без причины'} · {entry.get('evaluated_at') or entry.get('added_at') or '—'}"
                    )
            if total_terminal > len(terminal_history):
                st.caption(f"Показаны последние {len(terminal_history)} записей.")
