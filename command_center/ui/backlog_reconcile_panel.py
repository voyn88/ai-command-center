"""Backlog reconciliation panel — the UI surface for
`command_center.backlog_reconcile`.

The deterministic matcher finds open tasks that are almost certainly already
done (their own run finished, or they duplicate a Done / another open task).
This panel renders each finding with its reason and a one-click resolution —
send it to Review, or delete it — so the operator never launches the same work
twice. Nothing changes until a row's button is pressed: the matcher only ever
*proposes*, exactly as asked ("если задача уже реализована, просто предложить
её удалить"), and the operator confirms per row.
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

from command_center import backlog_reconcile, tasks_repository
from command_center.ui import confirm_dialog

_KIND_LABEL = {
    backlog_reconcile.KIND_SELF_COMPLETED: "прогон уже завершён",
    backlog_reconcile.KIND_DUPLICATE_OF_DONE: "дубль завершённой",
    backlog_reconcile.KIND_DUPLICATE_OPEN: "дубль открытой",
}


def _delete_finding(root: Path, finding) -> None:
    tasks_repository.delete_task(root, finding.task_id)
    # Cascade the runtime.db footprint too so a reconciled delete leaves no
    # orphan session/run/report rows (audit AR-1). Ideally routed through the
    # ExecutionCenterAPI facade; done inline here as this panel holds no api
    # handle (tracked under AR-7).
    from command_center.runtime import db as runtime_db

    runtime_db.delete_task(runtime_db.resolve_db_path(root), finding.task_id)
    st.toast(f"Удалено: {finding.title}")


def _scoped(tasks: list[dict], project: str | None) -> list[dict]:
    """Reconciliation runs project-by-project when a project is selected, so a
    task is never proposed as a duplicate of unrelated work in another project.
    The 'Все' / None selection reconciles across everything."""
    if not project or project in ("Все", "All"):
        return tasks
    return [t for t in tasks if t.get("project") == project]


def render_backlog_reconcile_panel(
    tasks: list[dict],
    root: Path,
    *,
    project: str | None = None,
    key_prefix: str = "backlog_reconcile",
) -> None:
    """Render the reconciliation findings for `tasks` (scoped to `project`).

    Persistence goes straight through `tasks_repository` — the same functions
    the Kanban card controls use — so a resolved card is gone (or moved) on the
    immediate rerun. Returns nothing; it writes to disk and reruns on action.
    """
    findings = backlog_reconcile.find_reconcilable(_scoped(tasks, project))

    header = f"🧹 Сверка бэклога · кандидатов: {len(findings)}"
    with st.expander(header, expanded=bool(findings)):
        st.caption(
            "Открытые задачи, которые, вероятно, уже сделаны или дублируются — "
            "чтобы не запускать одну и ту же работу дважды. Ничего не удаляется "
            "и не переносится без вашего подтверждения по каждой строке."
        )
        if not findings:
            st.success("Чисто — кандидатов на удаление нет.")
            return

        for finding in findings:
            info, done_col, del_col = st.columns([0.60, 0.20, 0.20])
            with info:
                st.markdown(f"**{finding.title}**")
                st.caption(
                    f"{_KIND_LABEL.get(finding.kind, finding.kind)} · "
                    f"{int(finding.confidence * 100)}% · {finding.reason}"
                )
            # The suggested action is the primary button; the other stays
            # available so the operator keeps full control per row.
            done_clicked = done_col.button(
                "На проверку",
                key=f"{key_prefix}_done_{finding.task_id}",
                use_container_width=True,
                type="primary" if not finding.is_delete else "secondary",
            )
            delete_clicked = del_col.button(
                "Удалить",
                key=f"{key_prefix}_delete_{finding.task_id}",
                use_container_width=True,
                type="primary" if finding.is_delete else "secondary",
            )
            if done_clicked:
                # Reversible (a Review-lane task can be moved back), so this
                # stays a single immediate click — no gate.
                tasks_repository.update_task_status(
                    root, finding.task_id, "Review"
                )
                st.toast(f"На проверку: {finding.title}")
                st.rerun()

            delete_key_prefix = f"{key_prefix}_delete_{finding.task_id}"
            if delete_clicked:
                confirm_dialog.open_confirmation(delete_key_prefix)

            confirm_dialog.render_destructive_confirmation(
                key_prefix=delete_key_prefix,
                dialog_title="Подтверждение удаления",
                warning=f"Задача «{finding.title}» (`{finding.task_id}`) будет удалена. Это действие нельзя отменить.",
                checkbox_label="Я подтверждаю удаление этой задачи.",
                confirm_label="Подтвердить удаление",
                on_confirm=lambda finding=finding: _delete_finding(root, finding),
            )
