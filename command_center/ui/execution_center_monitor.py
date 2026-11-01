"""Live Execution Center monitor extracted from ``app.py``
(NIGHT-W9-AICC-ARCH slice 5).

The v2 Session Supervisor UI: session builders, run cards, attention triage,
launch board, project tree, capacity panel, autopilot tick and the polling
fragments composing ``render_live_execution_center``. Pure move, no behavior
change; ``app.py`` re-exports the externally-used names by assignment.

Thin consumer of the frozen Sprint 1 runtime (``command_center.runtime``):
every launch/status/event/cancel operation goes through
``runtime_api.ExecutionCenterAPI``, never touching ``Supervisor`` internals,
raw SQL, or OS signals directly — and the one ``st.cache_resource`` API
singleton stays in ``app.py``, reached through ``ui.agent_launcher``'s
injected accessor (no second Supervisor/engine here). Session-state keys and
fragment cadences are byte-identical to their former inline definitions, so
widget identity, caching identity and rerun behavior do not change.
"""

from __future__ import annotations

import html
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from command_center import (
    agent_runner,
    artifacts,
    execution_queue,
    hero_playbooks,
    launch,
    launch_service,
    models,
    project_config,
    task_pipeline,
    workspace_provisioning,
)
from command_center.runtime import api as runtime_api
from command_center.runtime import context_service as runtime_context_service
from command_center.runtime import db as runtime_db
from command_center.runtime import identity as runtime_identity
from command_center.runtime import log_tail, project_overview, scheduler, session_view
from command_center.runtime import supervisor as runtime_supervisor
from command_center.ui import (
    autopilot_panel,
    board_style,
    execution_center_form,
    execution_metrics,
    inspector,
    legacy_task_helpers,
    live_board,
    queue_panel,
    waves_panel,
)
from command_center.ui.agent_launcher import (
    ROOT,
    TASK_TYPE_LABELS,
    TASK_TYPES,
    _get_execution_center_api,
)
from command_center.ui.task_cards import (
    KANBAN_COLUMNS,
    LAUNCH_STATUS_COLORS,
    PRIORITY_COLORS,
    _render_manual_merge_button,
    render_task_timeline,
)

# Same values app.py binds (canonical sources: models / artifacts /
# legacy_task_helpers — no duplicate vocabularies or second task engine).
REPORTS_DIR = ROOT / "reports"
MANUAL_KANBAN_STATUSES: list[str] = [status for status in KANBAN_COLUMNS if status != "Done"]
PRIORITIES: list[str] = models.TASK_PRIORITIES
read_text = artifacts.read_text
format_mtime = artifacts.format_mtime
create_task = legacy_task_helpers.create_task
upsert_tasks = legacy_task_helpers.upsert_tasks


def _execution_center_status_badge_color(status: str) -> str:
    return {
        session_view.STATUS_LAUNCHING: "blue",
        session_view.STATUS_STARTING: "blue",
        session_view.STATUS_RUNNING: "blue",
        session_view.STATUS_STALE: "orange",
        session_view.STATUS_WAITING: "orange",
        session_view.STATUS_REQUIRES_ATTENTION: "orange",
        session_view.STATUS_BLOCKED: "red",
        session_view.STATUS_INCOMPLETE: "orange",
        session_view.STATUS_COMPLETED: "green",
        session_view.STATUS_FAILED: "red",
        session_view.STATUS_CANCELLED: "gray",
    }.get(status, "gray")


def _execution_center_display_status(session: dict) -> str:
    """`session["status"]` as computed by `session_view.derive_status`, with
    one additional UI-level guard on top: a `Completed` run whose task has not
    reached `progress == 100` is shown as `Requires Attention` — a process that
    exited but whose work never merged is not "done" (Required fix 7).

    The exception is a **read-only** task (review/audit/gate): it has no merge
    lifecycle, so a clean `COMPLETED` *is* its terminal success and its Kanban
    `progress` legitimately never reaches 100. Downgrading it to Requires
    Attention was the bug behind "a successful analysis shows as needing
    attention" — read-only completed runs stay `Completed`."""
    return session_view.operator_display_status(
        session["status"],
        progress=session.get("progress"),
        task_type=session.get("task_type"),
    )


def _execution_center_record_heartbeat(run_id: str, pid: int | None, now: datetime) -> None:
    """Cheap, read-only liveness probe — never a signal to the process,
    never a write to `runtime.db`. This *is* the mission's "Heartbeat", and
    it is exactly what it sounds like: the last time the UI itself confirmed
    (via `identity.capture_identity`, the same primitive `Supervisor.
    reconcile()` already uses) that this PID still exists — not a signal the
    agent emits. Kept only in `st.session_state`, never persisted, so it
    never adds a row to `runtime.db` on every refresh tick."""
    if not pid:
        return
    if runtime_identity.query_identity(pid).status is runtime_identity.ProcessQueryStatus.LIVE:
        st.session_state.setdefault("exec_center_heartbeats", {})[run_id] = now


def _execution_center_heartbeat_probe_at(run_id: str) -> datetime | None:
    return st.session_state.get("exec_center_heartbeats", {}).get(run_id)


def _build_execution_center_sessions(
    api: runtime_api.ExecutionCenterAPI, tasks: list[dict], *, now: datetime
) -> tuple[list[dict], dict[str, dict]]:
    """Fetches every v2 run, joins it with its Kanban task (if any) and
    project config, and projects it through `session_view.build_session_view`
    — all business logic lives in `command_center.runtime.session_view`,
    this is just the join. Also performs the read-only heartbeat probe for
    every currently-Running run as a side effect."""
    tasks_by_id = {t["id"]: t for t in tasks if t.get("id")}
    # Clear per-render git-status cache so stale results from the previous
    # page load are discarded (many runs share the same workspace path).
    session_view.clear_git_status_cache()
    runs = api.list_runs(limit=200)
    # Batch the three per-run reads into one query each (audit H5 N+1). This loop
    # used to open ~3 fresh sqlite connections per run — up to ~600 per render —
    # every 2-5s and on every Home render; now it is 3 queries for the whole board.
    run_ids = [run["id"] for run in runs]
    latest_by_run = log_tail.latest_events_for_runs(api.db_path, run_ids)
    completion_by_run = api.get_completions_for_runs(run_ids)
    report_by_run = api.get_reports_for_runs(run_ids)
    # Historical median duration per task_type, computed once for the whole
    # board — the realistic denominator for progress/"осталось" when a task
    # carries no explicit estimate. See `session_view.median_completed_run_seconds`.
    _median_cache: dict[str | None, float | None] = {}

    def _reference_seconds(run: dict, task: dict | None) -> float | None:
        # Priority: the task's own estimate (2h → 7200 s) → historical median for
        # its type → None (caller falls back to the timeout cap). This is what
        # makes "осталено" track expected execution, not the timeout budget.
        estimate_hours = (task or {}).get("estimate_hours")
        if estimate_hours:
            return float(estimate_hours) * 3600.0
        tt = run.get("task_type")
        if tt not in _median_cache:
            _median_cache[tt] = session_view.median_completed_run_seconds(runs, task_type=tt)
        return _median_cache[tt] or session_view.median_completed_run_seconds(runs)

    sessions: list[dict] = []
    for run in runs:
        kanban_task = tasks_by_id.get(run.get("task_id"))
        project_cfg = project_config.get_project_config(run.get("project")) if run.get("project") else None
        latest = latest_by_run.get(run["id"])
        report_path = (kanban_task or {}).get("report_path")
        if not report_path:
            report_row = report_by_run.get(run["id"])
            report_path = report_row["path"] if report_row else None
        # Probe liveness *before* deriving the display status, and key it off
        # the persisted `run.state` (RUNNING) rather than the derived display
        # status — otherwise staleness (which is itself an input to the display
        # status) would be circular. A fresh probe on a live PID makes
        # `heartbeat_stale` False (age ~0); only a RUNNING run whose PID can no
        # longer be confirmed lets the last probe age past the threshold, at
        # which point the run displays `STATUS_STALE` (a warning, not a
        # failure).
        if run.get("state") == "RUNNING":
            _execution_center_record_heartbeat(run["id"], run.get("pid"), now)
        heartbeat_stale = run.get("state") == "RUNNING" and session_view.is_heartbeat_stale(
            _execution_center_heartbeat_probe_at(run["id"]), now
        )
        completion = completion_by_run.get(run["id"])
        session = session_view.build_session_view(
            run,
            kanban_task=kanban_task,
            project_cfg=project_cfg,
            latest_event=latest,
            report_path=report_path,
            now=now,
            heartbeat_stale=heartbeat_stale,
            completion=completion,
            reference_seconds=_reference_seconds(run, kanban_task),
        )
        sessions.append(session)
    return sessions, tasks_by_id


def _render_execution_center_completion(
    api: runtime_api.ExecutionCenterAPI, session: dict
) -> None:
    """Compact "autonomous completion" panel for a session card. Reads only
    `session["completion"]` (a pure projection built in `session_view`) — no
    orchestration happens here. Clearly separates "process finished" from "task
    completed and merged", and renders safely when optional fields are missing."""
    # A read-only task (review/audit/gate) has no merge lifecycle, so any
    # completion row seeded for it by the auto-merge pipeline is spurious — its
    # "Merge заблокирован"/PR fields describe a merge that was never meant to
    # happen. Showing that panel on a successful analysis is pure confusion, so
    # it is suppressed here; the card's progress already reads 100 % «Готово».
    if session_view.is_read_only_task_type(session.get("task_type")):
        return
    completion = session.get("completion")
    if not completion:
        return
    with st.container(border=True):
        badge_color = "green" if completion["is_done"] else (
            "red" if completion["display"] == "Requires Attention" else "orange"
        )
        head = st.columns([3, 1])
        head[0].markdown("**Автономное завершение задачи**")
        head[1].badge(completion["display"], color=badge_color)
        # Queryable caption text (used by tests / screen readers) that makes the
        # process-vs-task distinction explicit.
        if completion["is_done"]:
            st.caption("Завершение: **Done** — задача завершена и смёржена в целевую ветку.")
        else:
            st.caption(
                f"Завершение: **{completion['display']}** — процесс завершён, "
                "но задача ещё не смёржена в целевую ветку."
            )
        cols = st.columns(2)
        with cols[0]:
            st.write(f"Состояние: `{completion['state'] or '—'}`")
            st.write(f"Ветка → цель: `{completion['branch'] or '—'}` → `{completion['base_branch'] or '—'}`")
            st.write(f"Коммит: `{(completion['head_commit'] or '—')[:12]}`")
            st.write(f"Валидация: {completion['validation_summary'] or '—'}")
        with cols[1]:
            pr_number = completion["pull_request_number"]
            pr_state = completion["pull_request_state"] or "—"
            if pr_number and completion["pull_request_url"]:
                st.write(f"PR: [#{pr_number}]({completion['pull_request_url']}) · {pr_state}")
            else:
                st.write(f"PR: #{pr_number or '—'} · {pr_state}")
            if completion["replaced_pull_request_number"]:
                st.write(f"Заменяет закрытый PR: #{completion['replaced_pull_request_number']}")
            st.write(f"Merge-коммит: `{(completion['merge_commit'] or '—')[:12]}`")
            st.write(f"Проверено: {completion['last_checked_at'] or '—'}")
        if completion["recommended_action"]:
            note = st.warning if (completion["requires_human"] or completion["display"] == "Requires Attention") else st.info
            note(f"Рекомендуемое действие: {completion['recommended_action']}")
        if completion.get("manual_merge_available"):
            _render_manual_merge_button(
                api,
                api.get_completion(session["run_id"]),
                key=f"exec_manual_merge_{session['run_id']}",
            )


def _render_execution_center_provenance(session: dict) -> None:
    """Render only the canonical provenance projection attached by the API."""
    provenance = session.get("provenance")
    if not provenance:
        return
    with st.expander("Provenance run → delivery", expanded=False):
        st.write(f"Base → HEAD: `{(provenance.get('base_sha') or 'unknown')[:12]}` → `{(provenance.get('head_sha') or 'unknown')[:12]}`")
        pr = provenance.get("pr") or {}
        st.write(
            f"PR: `#{pr.get('number') or 'unknown'}` · head "
            f"`{(pr.get('head_sha') or 'unknown')[:12]}`"
        )
        conclusions = provenance.get("ci") or []
        st.write(
            "CI: "
            + (", ".join(f"{item.get('name')}: {item.get('conclusion')}" for item in conclusions) or "unknown")
        )
        st.write(f"Accepted SHA: `{provenance.get('accepted_sha') or 'unknown'}`")
        st.write(f"Deployed SHA: `{provenance.get('deployed_sha') or 'unknown'}`")
        if provenance.get("unknown_fields"):
            st.caption("Unknown evidence: " + ", ".join(provenance["unknown_fields"]))


_PROMPT_PREVIEW_CHARS = 700

_OPEN_TASK_DETAIL_KEY = "open_task_detail_id"


def _open_task_detail(task_id: str) -> None:
    """Request the task-detail dialog for `task_id` on the next rerun. A single
    shared trigger key so *every* view — a run card, a triage row, a tree node —
    drills into the same task detail the same way (mission: a task must be
    reachable from more than a couple of screens)."""
    st.session_state[_OPEN_TASK_DETAIL_KEY] = task_id


@st.dialog("Задача", width="large")
def _task_detail_dialog(task: dict, tasks_by_id: dict[str, dict]) -> None:
    """A compact, read-mostly task detail reachable from anywhere on the board.

    Deliberately *not* `render_task_card`: that card embeds `render_agent_launcher`,
    which opens its own `st.dialog` on launch — and Streamlit forbids a dialog
    inside a dialog. This shows the essentials (objective, live state, the
    blocking chain, history) plus a jump to the full Kanban card for editing,
    which is the one place the launcher can legally live."""
    title = task.get("title") or "Без названия"
    st.markdown(f"### {title}")
    st.caption(
        f"{task.get('project') or '—'} · "
        f"{TASK_TYPE_LABELS.get(task.get('task_type'), task.get('task_type') or '—')} · "
        f"`{task.get('id')}`"
    )

    cols = st.columns(3)
    with cols[0]:
        st.badge(task.get("priority") or "Medium", color=PRIORITY_COLORS.get(task.get("priority"), "blue"))
    with cols[1]:
        launch_status = task.get("launch_status") or "Ready"
        st.badge(launch_status, color=LAUNCH_STATUS_COLORS.get(launch_status, "gray"))
    with cols[2]:
        st.badge(task.get("status") or "—", color="gray")

    progress = int(task.get("progress") or 0)
    st.progress(progress / 100, text=f"{task.get('current_stage') or '—'} — {progress}%")

    if task.get("goal"):
        st.markdown(f"🎯 **Цель.** {task['goal']}")
    if task.get("pull_request_url"):
        st.link_button("Pull Request", task["pull_request_url"], icon=":material/merge:")
    completion = _get_execution_center_api().get_completion_by_task(task.get("id"))
    _render_manual_merge_button(
        _get_execution_center_api(),
        completion,
        key=f"task_detail_manual_merge_{task.get('id')}",
    )

    st.markdown("**Зависимости**")
    _render_dependency_tree(task, tasks_by_id)

    st.markdown("**История**")
    render_task_timeline(task)

    footer = st.columns([2, 2, 3])
    with footer[0]:
        if st.button("В очередь", icon=":material/playlist_add:", key="task_detail_enqueue", width="stretch"):
            execution_queue.enqueue_and_persist(ROOT, task, tasks_by_id)
            st.success("Добавлено в очередь запуска.")
    with footer[1]:
        workspace_path = task.get("workspace_path") or task.get("repository_path")
        if st.button("Workspace", icon=":material/folder_open:", key="task_detail_ws",
                     disabled=not workspace_path, width="stretch"):
            ok, msg = launch.open_folder_at(workspace_path)
            (st.success if ok else st.error)(msg)
    with footer[2]:
        if st.button("Открыть на Kanban (полная карточка)", icon=":material/view_kanban:",
                     key="task_detail_kanban", type="primary", width="stretch"):
            st.session_state[_OPEN_TASK_DETAIL_KEY] = None
            st.session_state.pending_nav = "kanban"
            st.rerun()


def _maybe_open_task_detail(tasks_by_id: dict[str, dict]) -> None:
    """Open the task-detail dialog if a view requested it this run. Called once
    from the board body; the trigger key is cleared as it opens so the dialog
    does not reappear on the next fragment refresh."""
    task_id = st.session_state.get(_OPEN_TASK_DETAIL_KEY)
    if not task_id:
        return
    st.session_state[_OPEN_TASK_DETAIL_KEY] = None
    task = tasks_by_id.get(task_id)
    if task is not None:
        _task_detail_dialog(task, tasks_by_id)


def _render_execution_center_intent(session: dict) -> None:
    """The run's *intent*: what it is meant to achieve (task goal) and the
    instruction it was actually launched with (prompt).

    Rendered as plain text, never an expander, so this can appear inside a
    card that is itself nested in one — Streamlit forbids nesting expanders,
    and the attention rows on the board rely on that being safe. A long prompt
    is truncated to a readable preview with the full text one toggle away,
    because the point here is orientation, not reading a two-page brief."""
    run_id = session["run_id"]
    goal = (session.get("task_goal") or "").strip()
    prompt = (session.get("prompt") or "").strip()
    if not goal and not prompt:
        return

    if goal:
        st.markdown(f"🎯 **Цель.** {goal}")

    if not prompt:
        return

    show_key = f"exec_card_prompt_full_{run_id}"
    truncated = len(prompt) > _PROMPT_PREVIEW_CHARS

    label_bits = ["Промпт"]
    if session.get("task_type"):
        label_bits.append(f"тип `{session['task_type']}`")
    if session.get("prompt_version"):
        label_bits.append(f"версия {session['prompt_version']}")

    # Controls come *before* the text they govern: a button click is itself the
    # rerun, so a toggle rendered below the block it expands would only take
    # effect on the following interaction. Placing it first means the state is
    # already current when the block below reads it — no explicit `st.rerun`,
    # which would be wrong here anyway (this renders both inside and outside a
    # fragment, and `scope="fragment"` is only legal in one of those).
    head = st.columns([2, 1, 1])
    head[0].caption(" · ".join(label_bits))
    if truncated:
        with head[1]:
            if st.button(
                "Свернуть" if st.session_state.get(show_key, False) else "Показать целиком",
                key=f"exec_card_prompt_toggle_{run_id}",
                icon=":material/unfold_more:",
            ):
                st.session_state[show_key] = not st.session_state.get(show_key, False)
    with head[2]:
        if st.button("Копировать", key=f"exec_card_copy_prompt_{run_id}", icon=":material/content_copy:"):
            ok, msg = launch.copy_to_clipboard(prompt)
            (st.success if ok else st.error)(msg)

    show_full = st.session_state.get(show_key, False)
    body = prompt if (show_full or not truncated) else prompt[:_PROMPT_PREVIEW_CHARS].rstrip() + " …"
    st.code(body, language=None, wrap_lines=True)


def _render_execution_center_card(
    api: runtime_api.ExecutionCenterAPI,
    session: dict,
    tasks_by_id: dict[str, dict],
    *,
    now: datetime,
    rail_bucket: str | None = None,
) -> None:
    run_id = session["run_id"]
    display_status = _execution_center_display_status(session)
    with st.container(border=True):
        if rail_bucket:
            board_style.card_rail(rail_bucket)
        header_cols = st.columns([3, 1])
        header_cols[0].markdown(f"##### {session['task_title']}")
        header_cols[1].badge(display_status, color=_execution_center_status_badge_color(display_status))
        # Inspector select (UX-2c): load this run into the top-bar Inspector.
        if st.button("🔍 В инспектор", key=f"exec_card_inspect_{run_id}", icon=":material/search:", help="Открыть прогон в Инспекторе"):
            inspector.select_run(run_id)
            st.rerun()
        # `display_status` is repeated here as plain caption text (not just
        # the `st.badge` pill above) so the run's display status stays
        # queryable in tests and screen readers alike.
        st.caption(
            f"Статус: **{display_status}** · Проект: **{session['project_id']}** · "
            f"Executor: `{session['executor']}` · Источник: {session['launch_source']}"
        )

        # Prefer the live, run-derived progress (moves at real milestones);
        # fall back to the task's stage progress only if the run has no live
        # value to show. See `session_view.derive_live_progress`.
        bar_progress = session.get("live_progress")
        bar_stage = session.get("live_stage")
        if bar_progress is None:
            bar_progress = session.get("progress")
            bar_stage = session.get("current_stage")
        if bar_progress is not None:
            # Percent is an evidenced delivery milestone. Elapsed time is a
            # separate fact and is never presented as percent complete or ETA.
            parts = [f"{bar_progress}%", bar_stage or "—"]
            elapsed = session.get("elapsed_seconds")
            if elapsed is not None:
                parts.append(f"прошло {session_view.format_elapsed(elapsed)}")
            if session["status"] in session_view.LIVE_PROCESS_DISPLAY_STATUSES:
                parts.append("ETA недоступно: недостаточно исторических данных")
            st.progress(min(max(bar_progress, 0), 100) / 100, text=" · ".join(parts))

        # Goal and prompt belong on the face of the card, not behind a button.
        # An operator judging a running agent asks "what is it trying to do"
        # first; previously the goal was nowhere in this screen and the prompt
        # was reachable only as "Копировать промпт", three clicks deep inside
        # the Logs panel — which meant it could be copied but never read.
        _render_execution_center_intent(session)

        info_cols = st.columns(2)
        with info_cols[0]:
            st.write(f"Workspace: `{session['workspace_path'] or '—'}`")
            st.write(f"Репозиторий: `{session['repository_path'] or '—'}`")
            st.write(f"Ожидаемая ветка: `{session['expected_branch'] or '—'}`")
            st.write(f"Текущая ветка: `{session['actual_branch'] or '—'}`")
            git_status = session.get("git_status")
            if git_status:
                dirty_label = "есть изменения" if git_status.get("dirty") else "чисто"
                st.caption(
                    f"Git-статус: {dirty_label} "
                    f"({git_status.get('modified_count', 0)} изменено, {git_status.get('untracked_count', 0)} новых)"
                )
        with info_cols[1]:
            st.write(f"Начат: {session['started_at'] or '—'}")
            st.write(f"Прошло: {session_view.format_elapsed(session['elapsed_seconds'])}")
            st.write(f"PID: `{session['process_id'] or '—'}`")
            if session["status"] in session_view.LIVE_PROCESS_DISPLAY_STATUSES:
                probe_at = _execution_center_heartbeat_probe_at(run_id)
                age = session_view.heartbeat_age_seconds(probe_at, now)
                stale = session_view.is_heartbeat_stale(probe_at, now)
                age_text = f"{int(age)} с назад" if age is not None else "ещё не подтверждено"
                st.write("Heartbeat (проверка живости UI, не сигнал агента): " + age_text + (" ⚠️" if stale else ""))

        # Explicitly distinguish "agent started but early output not yet
        # received" (a valid PID exists; this is NOT a start failure) from
        # "agent failed to start" (a FAILED run with no PID — rendered in the
        # Failed section with its error). This is the direct UI counterpart to
        # the mission's required distinction.
        if session["status"] == session_view.STATUS_STARTING:
            st.info(
                "Агент запущен (процесс создан, PID есть), но первый вывод ещё не получен. "
                "Это ожидание раннего вывода, а не ошибка запуска — Claude может не выдавать "
                "stdout сразу."
            )
        elif session["status"] == session_view.STATUS_STALE:
            st.warning(
                "Процесс всё ещё числится запущенным, но UI давно не подтверждал его живость "
                "(проверка живости устарела). Это предупреждение, а не отказ запуска."
            )

        if session["latest_event"]:
            st.caption(
                f"Последнее событие ({session['latest_event'].get('at') or '—'}): "
                f"{session['latest_event'].get('summary') or '—'}"
            )
        if session.get("blocker_reason"):
            st.warning(f"Причина блокировки: {session['blocker_reason']}")
        elif session["last_error"]:
            st.error(f"Последняя ошибка: {session['last_error']}")

        _render_execution_center_provenance(session)
        _render_execution_center_completion(api, session)

        # Localized labels (Russian) throughout — the console UI is otherwise
        # Russian, and an English row of controls in the middle of it was one of
        # the consistency defects the UX analysis called out. Widget `key=`s are
        # unchanged, so every test that drives these buttons by key still works.
        button_cols = st.columns(6)
        with button_cols[0]:
            if st.button(
                "Папка", key=f"exec_card_ws_{run_id}", icon=":material/folder_open:",
                disabled=not session["workspace_path"], help="Открыть рабочую папку",
            ):
                ok, msg = launch.open_folder_at(session["workspace_path"])
                (st.success if ok else st.error)(msg)
        with button_cols[1]:
            if st.button(
                "Терминал", key=f"exec_card_term_{run_id}", icon=":material/terminal:",
                disabled=not session["workspace_path"], help="Открыть терминал в workspace",
            ):
                ok, msg = launch.open_terminal_at(session["workspace_path"])
                (st.success if ok else st.error)(msg)
        with button_cols[2]:
            logs_key = f"exec_card_logs_open_{run_id}"
            if st.button("Логи", key=f"exec_card_logs_btn_{run_id}", icon=":material/description:"):
                st.session_state[logs_key] = not st.session_state.get(logs_key, False)
        with button_cols[3]:
            real_task = tasks_by_id.get(session["task_id"]) if session["task_id"] else None
            if st.button(
                "Задача", key=f"exec_card_task_{run_id}", icon=":material/task_alt:", disabled=real_task is None,
                help="Открыть детали задачи",
            ):
                _open_task_detail(session["task_id"])
                st.rerun()
        with button_cols[4]:
            report_key = f"exec_card_report_open_{run_id}"
            if st.button(
                "Отчёт", key=f"exec_card_report_btn_{run_id}", icon=":material/summarize:",
                disabled=not session["report_path"],
            ):
                st.session_state[report_key] = not st.session_state.get(report_key, False)
        with button_cols[5]:
            if session["status"] in session_view.LIVE_PROCESS_DISPLAY_STATUSES:
                cancel_ack = st.checkbox("Подтвердить", key=f"exec_card_cancel_ack_{run_id}")
                if st.button(
                    "Отменить", key=f"exec_card_cancel_btn_{run_id}", icon=":material/stop_circle:",
                    disabled=not cancel_ack,
                ):
                    # `disabled=` above is the primary, client-side gate, but
                    # `AppTest.click()` (and, in principle, a malformed
                    # client request) does not itself respect it — re-check
                    # `cancel_ack` server-side, the same defense-in-depth
                    # convention every other confirm-then-act control in this
                    # codebase uses, before ever calling `request_cancel`.
                    if not cancel_ack:
                        st.error("Отмена заблокирована: подтвердите отмену перед выполнением.")
                    else:
                        # The only path to `Supervisor.cancel` — signals
                        # exactly the PID+identity recorded at launch, never
                        # an arbitrary PID, never a git command.
                        try:
                            api.request_cancel(run_id, confirmed=True)
                            st.success("Запрос на отмену отправлен.")
                        except (runtime_supervisor.SupervisorError, KeyError) as exc:
                            st.error(str(exc))
                        st.rerun()

        # Logs and report render into plain bordered containers, not expanders.
        # They are already gated by their own toggle buttons, so the expander
        # added no affordance — only the constraint that this card could never
        # appear inside another expander. The board's collapsed sections depend
        # on exactly that being allowed.
        if st.session_state.get(f"exec_card_logs_open_{run_id}"):
            with st.container(border=True):
                st.markdown("**Логи и таймлайн сессии**")
                events = log_tail.tail_events(api.db_path, run_id)
                if events:
                    st.code("\n".join(log_tail.render_log_lines(events)), language=None)
                else:
                    st.caption("Логи пока недоступны.")
                timeline = log_tail.session_timeline(api.db_path, run_id)
                if timeline:
                    st.markdown("**Таймлайн (launch/cancel/completion/failure/reconciliation):**")
                    for event in timeline:
                        payload = event.get("payload") or {}
                        st.caption(f"{event.get('created_at', '—')} — {payload.get('lifecycle', event['event_type'])}")

        if st.session_state.get(f"exec_card_report_open_{run_id}"):
            with st.container(border=True):
                st.markdown("**Отчёт**")
                report_full_path = agent_runner.resolve_report_path({"report_path": session["report_path"]})
                if report_full_path is None:
                    st.warning("Путь к отчёту не проходит проверку безопасности — файл не открыт.")
                elif report_full_path.exists():
                    st.markdown(read_text(report_full_path))
                else:
                    st.caption("Файл отчёта не найден на диске.")
                if session.get("commit_hash"):
                    st.write(f"Commit: `{session['commit_hash']}`")
                if session.get("pull_request_url"):
                    st.write(f"Pull Request: {session['pull_request_url']}")


def _render_capacity_panel(api: runtime_api.ExecutionCenterAPI) -> None:
    """How loaded the machine is and how much free agent capacity remains —
    the answer to "can anything even start right now, and on whom".

    Built from the same `scheduler.build_load_snapshot` + `default_registry`
    the planner itself uses, so this panel and the autopilot can never disagree
    about how many slots are free. Read-only."""
    settings = task_pipeline.pipeline_settings.load_settings(ROOT)
    load = scheduler.build_load_snapshot(api.db_path)
    registry = scheduler.default_registry(max_concurrency=settings.max_agent_concurrency)
    summary = live_board.capacity_summary(
        running_by_agent=dict(load.running_by_agent),
        global_running=load.global_running,
        global_limit=settings.max_global_concurrency,
        agents=[(a.agent_id, a.max_concurrency, a.available) for a in registry.all()],
    )

    st.markdown("##### Загрузка")
    with st.container(border=True):
        tone = "red" if summary.saturated else ("orange" if summary.global_free == 1 else "green")
        head = st.columns([3, 2], vertical_alignment="center")
        head[0].markdown(f"**{summary.global_running} / {summary.global_limit}** прогонов")
        with head[1]:
            st.badge(
                "нет мест" if summary.saturated else f"свободно {summary.global_free}",
                color=tone,
            )
        st.progress(
            min(summary.global_running / summary.global_limit, 1.0) if summary.global_limit else 0.0,
            text=f"Свободных агентов: {summary.free_agent_count}",
        )
        for agent in summary.agents:
            if not agent.available:
                st.caption(f"🔴 `{agent.agent_id}` — недоступен")
            elif agent.free == 0:
                st.caption(f"🟠 `{agent.agent_id}` — занят {agent.used}/{agent.max_concurrency}")
            else:
                st.caption(f"🟢 `{agent.agent_id}` — {agent.used}/{agent.max_concurrency} · свободно {agent.free}")
        if summary.saturated:
            st.caption("Все места заняты — новые задачи ждут освобождения слота.")


def _short_path(path: str | None, *, keep: int = 2) -> str:
    """Last `keep` segments of a path — the part that identifies a worktree.

    The board's side column is ~25 % of the width; a full
    `/Users/…/Projects/ai-command-center-ci-review` wraps to three lines and
    tells the reader nothing the last segment does not."""
    if not path:
        return "—"
    parts = Path(path).parts
    return "…/" + "/".join(parts[-keep:]) if len(parts) > keep else path


def _render_execution_center_project_overview(sessions: list[dict], now: datetime) -> None:
    by_project: dict[str, list[dict]] = {}
    for session in sessions:
        by_project.setdefault(session["project_id"], []).append(session)
    if not by_project:
        return

    stale_run_ids = frozenset(
        s["run_id"]
        for s in sessions
        if s["status"] in session_view.LIVE_PROCESS_DISPLAY_STATUSES
        and session_view.is_heartbeat_stale(_execution_center_heartbeat_probe_at(s["run_id"]), now)
    )

    # A vertical strip, sized for the board's narrow side column. Projects are
    # standing context — "who is where, and is anything degraded" — not the
    # thing an operator acts on, so they no longer occupy a full-width row
    # above the runs that do need acting on. Degraded projects sort first:
    # if the strip is ever cut short by height, it is cut at the healthy end.
    st.markdown("##### Проекты")
    health_rank = {"Degraded": 0, "Attention": 1, "OK": 2}
    overviews = []
    for project_id in sorted(by_project):
        cfg = project_config.get_project_config(project_id)
        overviews.append(
            project_overview.build_project_overview(
                project_id, sessions=by_project[project_id], project_cfg=cfg, now=now, stale_run_ids=stale_run_ids
            )
        )
    overviews.sort(key=lambda o: (health_rank.get(o["health"], 3), o["project_id"]))

    for overview in overviews:
        health_color = {"OK": "green", "Attention": "orange", "Degraded": "red"}.get(overview["health"], "gray")
        with st.container(border=True):
            head = st.columns([2, 1])
            head[0].markdown(f"**{overview['project_id']}**")
            with head[1]:
                st.badge(overview["health"], color=health_color)
            st.caption(
                f"▶ {overview['running_count']} · ⏳ {overview['waiting_count']} · "
                f"✅ сегодня {overview['completed_today_count']}"
            )
            # Only meaningful while something of this project's is actually
            # up; on an idle project these three lines were three dashes.
            if overview["running_count"]:
                st.caption(f"`{_short_path(overview['current_workspace'])}`")
                st.caption(
                    f"{overview['current_executor'] or '—'} · ветка `{overview['current_branch'] or '—'}`"
                )

            # The side strip is navigation: picking a project opens its task
            # tree in the main column, where there is width for it. Rendering
            # the tree here instead would put fifty levelled rows into a
            # quarter-width column.
            project_id = overview["project_id"]
            selected = st.session_state.get(_PROJECT_TREE_KEY) == project_id
            if st.button(
                "Скрыть дерево" if selected else "Дерево задач",
                key=f"exec_project_tree_{project_id}",
                icon=":material/account_tree:",
                width="stretch",
                type="primary" if selected else "secondary",
            ):
                st.session_state[_PROJECT_TREE_KEY] = None if selected else project_id
                st.rerun()


# How many terminal runs the board keeps on screen. History is bounded, not
# complete: the full record lives in Журнал запусков, and a board that renders
# every run ever executed is the "простыня" this layout exists to end.
_BOARD_HISTORY_LIMIT = 20


_CONSOLE_PANEL_KEY = "exec_board_open_panel"


def _render_console_actions(tasks: list[dict], tasks_by_id: dict[str, dict]) -> None:
    """The console's action bar: create a task, see the waves, read a report —
    each as a panel that opens *here*, not as a separate page.

    Three of the app's twenty nav entries existed only to hold a form or a
    list that is consulted for a few seconds and closed. Splitting them across
    pages meant losing the execution context to file a task about the run you
    were looking at. One panel is open at a time, so the bar never becomes a
    third wall of its own."""
    open_panel = st.session_state.get(_CONSOLE_PANEL_KEY)
    labels = (
        ("create", "Создать задачу", ":material/add_task:"),
        ("waves", "Волны", ":material/waves:"),
        ("reports", "Отчёты", ":material/summarize:"),
    )
    cols = st.columns(len(labels) + 1)
    for idx, (panel, label, icon) in enumerate(labels):
        with cols[idx]:
            if st.button(
                label,
                key=f"console_panel_{panel}",
                icon=icon,
                width="stretch",
                type="primary" if open_panel == panel else "secondary",
            ):
                st.session_state[_CONSOLE_PANEL_KEY] = None if open_panel == panel else panel
                st.rerun()

    open_panel = st.session_state.get(_CONSOLE_PANEL_KEY)
    if open_panel == "create":
        _render_inline_create_task(tasks)
    elif open_panel == "waves":
        with st.container(border=True):
            waves_panel.render_waves_page(tasks, tasks_by_id, ROOT)
    elif open_panel == "reports":
        _render_inline_reports()


def _render_inline_create_task(tasks: list[dict]) -> None:
    """A minimal create form — project, title, type, priority, goal.

    Deliberately not the full Создать задачу page: this exists to capture a
    task the moment you see the need for it, with everything else editable on
    the task card afterwards. It commits through the same locked `create_task`
    every other creation path uses, never a snapshot write."""
    with st.container(border=True):
        st.markdown("##### Новая задача")
        with st.form("console_create_task", clear_on_submit=True):
            row = st.columns([2, 4])
            project = row[0].selectbox("Проект", models.PROJECT_IDS, key="console_create_project")
            title = row[1].text_input("Название", key="console_create_title")
            row2 = st.columns([2, 2, 2])
            task_type = row2[0].selectbox("Тип", TASK_TYPES, key="console_create_type")
            priority = row2[1].selectbox("Приоритет", PRIORITIES, index=PRIORITIES.index("Medium"),
                                         key="console_create_priority")
            status = row2[2].selectbox(
                "Колонка", MANUAL_KANBAN_STATUSES, key="console_create_status"
            )
            goal = st.text_area("Цель", key="console_create_goal", height=80)
            if st.form_submit_button("Создать", type="primary", icon=":material/add_task:"):
                if not title.strip():
                    st.error("Название обязательно.")
                else:
                    created = create_task(
                        project, title.strip(), task_type, status, goal=goal.strip() or None, priority=priority
                    )
                    st.success(f"Создана: {created.get('title')}")
                    _render_hero_playbook_suggestion(created, tasks)


def _render_hero_playbook_suggestion(created: dict, tasks: list[dict]) -> None:
    """After a new scenario is created, check whether its context (project +
    task type, falling back to task type alone or title/goal similarity)
    matches a `hero_playbooks` combo with a strong historical track record —
    VOYN-MIN-HERO's "a new scenario automatically suggests a Hero Playbook
    for similar context" acceptance. Silent when nothing meets the match
    bar; a low-confidence guess is worse than no suggestion."""
    catalog = hero_playbooks.build_playbook_catalog(tasks)
    suggestion = hero_playbooks.suggest_hero_playbook(created, catalog)
    if suggestion is None:
        return
    playbook = suggestion.playbook
    st.info(
        f"🏆 Hero Playbook: агент «{playbook.agent}» для «{playbook.task_type}» — {suggestion.reason}."
    )


def _render_inline_reports() -> None:
    """The newest reports, readable without leaving the console.

    One button, a short list, and the report text inline — rather than a page
    that renders every report file stacked end to end."""
    with st.container(border=True):
        st.markdown("##### Отчёты")
        files = artifacts.list_markdown_files(REPORTS_DIR)
        if not files:
            st.caption("Отчётов пока нет.")
            return
        newest = sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)[:15]
        chosen = st.selectbox(
            "Отчёт",
            newest,
            format_func=lambda p: f"{p.name} · {format_mtime(p)}",
            key="console_report_pick",
        )
        if chosen is not None:
            st.markdown(read_text(chosen))


def _render_board_summary(
    board: dict[str, list[dict]], counts: execution_metrics.ExecutionCounts
) -> None:
    """One line that answers "what is the state of the machine" before any
    scrolling. Deliberately the first thing rendered — the previous layout led
    with the planner's wave, so this answer was several screens down.

    Rendered as `board_style`'s tinted, accented tiles rather than flat
    `st.metric` boxes: a running count and a failure count must not look
    identical, which four grey boxes made them."""
    board_style.stat_tiles(
        board,
        counts={
            bucket: counts.for_bucket(bucket)
            for bucket in live_board.BUCKET_ORDER
        },
    )


# --------------------------------------------------------------------------
# Attention triage — turn the wall of failures into something an operator can
# act on: read one concrete reason + suggested fix per item, select several,
# and relaunch them all with one shared instruction, or hide the ones already
# handled.
# --------------------------------------------------------------------------

_ATTENTION_SELECT_KEY = "exec_attention_selected"          # set[run_id] checked now
_ATTENTION_DISMISSED_KEY = "exec_attention_dismissed"      # set[run_id] hidden this session
_ATTENTION_FLASH_KEY = "exec_attention_flash"
_ATTENTION_SHOW_HIDDEN_KEY = "exec_attention_show_hidden"


def _attention_advice(session: dict) -> tuple[str, str]:
    """(what went wrong, what to do) for one attention item — concrete, not the
    generic "Requires Attention" the status badge already says.

    Reuses the completion pipeline's own `recommended_action` and the planner's
    per-reason-code remediation vocabulary so the advice here matches what the
    autopilot panel and the launch gate say about the identical condition."""
    completion = session.get("completion") or {}
    reason = (
        session.get("blocker_reason")
        or session.get("last_error")
        or completion.get("validation_summary")
        or "Прогон остановился, не дойдя до завершения."
    )
    action = (
        completion.get("recommended_action")
        or task_pipeline.remediation_for(completion.get("reason_code"))
        or "Исправьте причину и перезапустите — можно прямо отсюда, с общей инструкцией."
    )
    return str(reason), str(action)


def _build_fix_instruction(task: dict, session: dict) -> str:
    """The instruction for an operator-requested fix relaunch: the task's own
    objective plus exactly what failed last time, and a directive to diagnose
    and fix it. No operator-typed note — the agent works out *what* to fix from
    the failure it is handed, which is the whole point of "let the AI decide".
    Same shape as `task_pipeline._rework_prompt`, human-initiated."""
    base = (task.get("prompt") or task.get("goal") or task.get("title") or "").strip()
    reason, _ = _attention_advice(session)
    lines = [
        base,
        "",
        "## Исправление (перезапуск оператором)",
        "",
        "Предыдущая попытка не дошла до завершения. Разберись, почему она упала, "
        "исправь причину и доведи задачу до состояния, в котором проверка проходит.",
    ]
    if reason:
        lines += ["", "### Что пошло не так в прошлой попытке", "", reason]
    return "\n".join(lines).strip()


def _fix_attention_sessions(
    api: runtime_api.ExecutionCenterAPI,
    sessions: list[dict],
    tasks_by_id: dict[str, dict],
) -> list[tuple[str, bool, str]]:
    """Relaunch each selected attention item as a new, operator-confirmed
    attempt carrying the shared fix instruction.

    Goes through ``launch_service.execute_agent_launch_v2`` — the same path
    the Kanban launcher uses — so it inherits executor fallback (tries the
    next available agent when the configured one is in
    ``failed_executors``), workspace provisioning + verification (the
    worktree path passes the fail-closed gate via
    ``repository_already_validated``), and the per-task timeout. The
    ``confirmed=True`` flag bypasses the planner's conservative gates
    (a ``terminal_failure`` verdict, a dirty-tree warning) that exist
    precisely because *no* human was in the loop. The one gate it never
    bypasses is the fail-closed workspace isolation in
    ``Supervisor.start_raw`` — that is a safety boundary, not a
    convenience refusal.

    Returns (task_title, ok, detail) per item, in order."""
    results: list[tuple[str, bool, str]] = []
    for session in sessions:
        title = session.get("task_title") or session.get("run_id") or "—"
        task = tasks_by_id.get(session.get("task_id"))
        if task is None:
            results.append((title, False, "Задача не найдена — возможно, это ad-hoc прогон без задачи."))
            continue
        project_id = project_config.canonical_project_id(task.get("project"))
        cfg = project_config.get_project_config(project_id)
        if cfg.get("sensitive"):
            results.append((title, False, "Чувствительный проект (BANK/LEGAL) — запускайте с карточки задачи."))
            continue
        workspace = task.get("workspace_path") or task.get("repository_path") or cfg.get("repository_path")
        if not workspace:
            results.append((title, False, "Не настроен workspace задачи."))
            continue

        # --- Executor fallback (AICC-DESKTOP-017) ------------------------
        # The configured executor is retried as-is unless it already failed to
        # *start* (``failed_executors`` — a recorded startup failure with no
        # output, e.g. an expired OAuth token), in which case we fall through to
        # the next available agent in the project's ``allowed_agents`` chain.
        # We deliberately do NOT gate the configured executor on the live
        # ``provider.availability()`` probe here: that probe shells out to the
        # provider CLI and can report ``False`` for transient reasons (a
        # daemon restarting, a probe timeout under load) that are not evidence
        # the agent cannot run this task, and in test/CI the real binary is
        # absent even though the run is faked — gating on it would block the
        # retry the operator explicitly asked for. ``select_available_executor``
        # (which does probe) is only consulted once the configured executor is
        # known to have failed to start.
        configured_executor = task.get("executor") or "claude_code"
        failed = set(task.get("failed_executors") or [])
        selected_executor = execution_queue.select_remediation_executor(task, cfg)
        if selected_executor is None:
            results.append(
                (title, False, "ни один из разрешённых исполнителей не доступен — проверьте установку/авторизацию агентов")
            )
            continue
        if configured_executor in failed and selected_executor == configured_executor:
            models.append_timeline_event(
                task,
                "remediation_retry",
                f"Оператор повторно разрешил проверку восстановленного исполнителя «{configured_executor}».",
            )
        else:
            original = configured_executor
            if selected_executor != original:
                task["executor"] = selected_executor
                task.setdefault("timeline", []).append(
                    {
                        "ts": models.iso_now(),
                        "type": "executor_fallback",
                        "from": original,
                        "to": selected_executor,
                        "reason": "configured executor failed to start (attention triage fix)",
                    }
                )

        source_repository_path = cfg.get("repository_path")
        expected_branch = task.get("branch")
        base_branch = cfg.get("base_branch") or "main"

        try:
            run = launch_service.execute_agent_launch_v2(
                project=project_id,
                # "Исправить" must always launch a write-capable remediation
                # attempt. Reusing the failed run's task type could relaunch a
                # read-only review/final-gate agent, which can diagnose the
                # defect but is intentionally unable to change any files.
                task_type="remediation",
                prompt=_build_fix_instruction(task, session),
                timeout_seconds=agent_runner.timeout_for_task(task),
                repository_path=Path(workspace),
                execution_center_api=api,
                confirmed=True,
                task=task,
                executor_id=selected_executor,
                expected_branch=expected_branch,
                base_branch=base_branch,
                source_repository_path=source_repository_path,
                max_global_concurrency=cfg.get("max_global_concurrency"),
            )
        except (
            launch_service.DuplicateActiveLaunchError,
            runtime_context_service.ConfirmationRequiredError,
            agent_runner.RunnerError,
            project_config.ProviderAuthorizationError,
            runtime_supervisor.SupervisorError,
            workspace_provisioning.WorkspaceVerificationError,
            runtime_supervisor.WorkspaceVerificationFailed,
        ) as exc:
            results.append((title, False, str(exc)))
            continue
        results.append((title, True, run["id"]))
    return results


def _render_attention_triage(
    api: runtime_api.ExecutionCenterAPI,
    attention: list[dict],
    tasks_by_id: dict[str, dict],
    *,
    now: datetime,
) -> None:
    """The attention bucket as a triage list: select items, relaunch them all
    with one shared fix instruction, or hide the ones already handled.

    This replaces a stack of near-identical "Requires Attention" cards — which
    told the operator a problem existed but gave them no way to act on it in
    bulk — with a worklist that answers "what do I do with these": a checkbox
    per item, one concrete reason and one suggested action per row, and a single
    instruction box that drives a confirmed relaunch of everything ticked."""
    selected: set[str] = st.session_state.setdefault(_ATTENTION_SELECT_KEY, set())
    dismissed: set[str] = st.session_state.setdefault(_ATTENTION_DISMISSED_KEY, set())

    flash = st.session_state.pop(_ATTENTION_FLASH_KEY, None)
    if flash:
        for title, ok, detail in flash:
            if ok:
                st.success(f"↻ {title}: запущено (прогон `{detail[:8]}`).")
            else:
                st.warning(f"⚠ {title}: {detail}")

    visible = [s for s in attention if s["run_id"] not in dismissed]
    hidden_count = len(attention) - len(visible)

    board_style.section_head(live_board.BUCKET_ATTENTION, len(visible))
    if not visible:
        st.caption(
            "Нет остановившихся прогонов."
            + (f" Скрыто: {hidden_count}." if hidden_count else "")
        )
        return

    shown = visible[:_BOARD_HISTORY_LIMIT]

    # Bulk action bar — select items, then relaunch them. No instruction box:
    # the agent works out what to fix from the failure carried into its prompt
    # (see `_build_fix_instruction`), which is what the operator asked for —
    # "let the AI decide". One less control, and nothing to fill in before a fix.
    with st.container(border=True):
        st.caption("Выберите задачи и нажмите «Исправить» — агент сам определит, что чинить, по причине сбоя.")
        bar = st.columns([2, 2, 2, 3])
        with bar[0]:
            if st.button("Выбрать все", key="exec_attention_select_all", width="stretch"):
                selected.update(s["run_id"] for s in shown)
                st.rerun()
        with bar[1]:
            if st.button("Снять выбор", key="exec_attention_clear_sel", width="stretch"):
                selected.clear()
                st.rerun()
        with bar[2]:
            chosen = [s for s in shown if s["run_id"] in selected]
            if st.button(
                f"Исправить ({len(chosen)})",
                key="exec_attention_fix_selected",
                type="primary",
                icon=":material/build:",
                disabled=not chosen,
                width="stretch",
            ):
                st.session_state[_ATTENTION_FLASH_KEY] = _fix_attention_sessions(
                    api, chosen, tasks_by_id
                )
                for s in chosen:
                    selected.discard(s["run_id"])
                st.rerun()
        with bar[3]:
            chosen = [s for s in shown if s["run_id"] in selected]
            if st.button(
                f"Скрыть выбранные ({len(chosen)})",
                key="exec_attention_hide_selected",
                icon=":material/visibility_off:",
                disabled=not chosen,
                width="stretch",
            ):
                dismissed.update(s["run_id"] for s in chosen)
                selected.difference_update(s["run_id"] for s in chosen)
                st.rerun()
        if hidden_count:
            st.caption(f"Скрыто в этой сессии: {hidden_count}.")
            if st.button("Показать скрытые", key="exec_attention_unhide"):
                dismissed.clear()
                st.rerun()

    for session in shown:
        _render_attention_triage_row(api, session, tasks_by_id, selected, dismissed, now=now)

    if len(visible) > _BOARD_HISTORY_LIMIT:
        st.caption(
            f"Показаны {_BOARD_HISTORY_LIMIT} из {len(visible)} — остальные в «Журнале запусков»."
        )


def _render_attention_triage_row(
    api: runtime_api.ExecutionCenterAPI,
    session: dict,
    tasks_by_id: dict[str, dict],
    selected: set[str],
    dismissed: set[str],
    *,
    now: datetime,
) -> None:
    """One triage row: checkbox, title, concrete reason + suggested action, and
    per-row Открыть / Исправить / Скрыть."""
    run_id = session["run_id"]
    status = session["display_status"]
    reason, action = _attention_advice(session)
    open_key = f"exec_attention_open_{run_id}"

    with st.container(border=True):
        board_style.card_rail(live_board.bucket_for_status(status))
        head = st.columns([1, 6, 2], vertical_alignment="center")
        with head[0]:
            checked = st.checkbox(
                "Выбрать", key=f"exec_attention_cb_{run_id}",
                value=run_id in selected, label_visibility="collapsed",
            )
            if checked:
                selected.add(run_id)
            else:
                selected.discard(run_id)
        head[1].markdown(f"**{session['task_title']}**")
        with head[2]:
            st.badge(status, color=_execution_center_status_badge_color(status))

        # `Статус: **X**` stays as queryable caption text (tests and screen
        # readers both read it), beside the badge above.
        st.caption(
            f"Статус: **{status}** · Проект: **{session['project_id'] or '—'}** · "
            f"начат {session.get('started_at') or '—'}"
        )
        # The concrete failure, in an error/warning box so it is impossible to
        # miss and stays queryable as such — then the suggested action beside it.
        reason_box = st.warning if status == session_view.STATUS_BLOCKED else st.error
        reason_box(f"Что не так: {reason}")
        st.markdown(f"🛠 **Что делать.** {action}")

        actions = st.columns([1, 1, 1, 1, 2], vertical_alignment="center")
        with actions[0]:
            if st.button("Открыть", key=f"exec_attention_toggle_{run_id}", icon=":material/unfold_more:", width="stretch"):
                st.session_state[open_key] = not st.session_state.get(open_key, False)
        with actions[1]:
            if st.button(
                "Задача", key=f"exec_attention_detail_{run_id}", icon=":material/task_alt:",
                disabled=session.get("task_id") is None, width="stretch",
            ):
                _open_task_detail(session["task_id"])
                st.rerun()
        with actions[2]:
            if st.button("Исправить", key=f"exec_attention_fix_one_{run_id}", icon=":material/build:", width="stretch"):
                st.session_state[_ATTENTION_FLASH_KEY] = _fix_attention_sessions(
                    api, [session], tasks_by_id
                )
                st.rerun()
        with actions[3]:
            if st.button("Скрыть", key=f"exec_attention_hide_one_{run_id}", icon=":material/visibility_off:", width="stretch"):
                dismissed.add(run_id)
                selected.discard(run_id)
                st.rerun()

        if st.session_state.get(open_key, False):
            _render_execution_center_card(api, session, tasks_by_id, now=now)


def _render_board_sections(
    api: runtime_api.ExecutionCenterAPI,
    board: dict[str, list[dict]],
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
    queue_entries: list[dict],
    *,
    now: datetime,
) -> None:
    """The board's main column, in the operator's reading order: what is
    running, what broke, what is queued, what is finished.

    The ordering is the entire redesign. Previously this rendered seven
    equal-weight status sections below a full-width project grid and the
    planner's wave, so three live runs sat under roughly two screens of
    context — which is why "the tasks disappeared" was a reasonable thing to
    say about a dashboard that was in fact showing them."""
    live = board[live_board.BUCKET_LIVE]
    board_style.section_head(live_board.BUCKET_LIVE, len(live))
    if not live:
        st.caption("Сейчас ничего не выполняется.")
    for session in live:
        _render_execution_center_card(
            api, session, tasks_by_id, now=now, rail_bucket=live_board.BUCKET_LIVE
        )

    _render_attention_triage(api, board[live_board.BUCKET_ATTENTION], tasks_by_id, now=now)

    _render_waiting_section(
        api,
        board[live_board.BUCKET_WAITING],
        tasks,
        tasks_by_id,
        queue_entries,
        now=now,
    )

    done = board[live_board.BUCKET_DONE]
    if done:
        with st.expander(f"✓ {live_board.BUCKET_TITLES[live_board.BUCKET_DONE]} ({len(done)})", expanded=False):
            for session in done[:_BOARD_HISTORY_LIMIT]:
                _render_execution_center_card(api, session, tasks_by_id, now=now)


def _render_waiting_section(
    api: runtime_api.ExecutionCenterAPI,
    waiting_sessions: list[dict],
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
    queue_entries: list[dict],
    *,
    now: datetime,
) -> None:
    """"Ожидают запуска" — what is queued to run but has not started yet.

    Two distinct things end up here, and the section makes the difference
    explicit because conflating them was the confusion behind "what is this
    status and why is it always empty":

    1. **The execution queue** — tasks the operator (or a wave) put in line to
       run. `ready` ones will start as soon as an agent slot frees; `waiting`
       ones are held back by a reason (usually an unmet dependency), shown per
       row. THIS is what an operator means by "waiting to launch", and it is
       almost always the populated part.
    2. **Run-level QUEUED sessions** — a run the supervisor has prepared but not
       yet spawned. A run passes through this in milliseconds, so on its own it
       is nearly always empty — which is exactly why the old section looked
       broken.
    """
    open_entries = [
        e for e in queue_entries if e.get("state") in execution_queue.OPEN_STATES
    ]
    open_entries.sort(key=lambda e: (e.get("state") != execution_queue.STATE_READY, e.get("added_at") or ""))

    total = len(open_entries) + len(waiting_sessions)
    board_style.section_head(live_board.BUCKET_WAITING, total)

    if total == 0:
        st.caption(
            "Очередь запуска пуста — сюда попадают задачи, поставленные в очередь "
            "(кнопкой «В очередь», волной или автопилотом) и ждущие свободного слота "
            "агента. Поставьте задачу в очередь, и она появится здесь до старта."
        )
        return

    queue_panel.render_execution_queue_panel(
        tasks,
        tasks_by_id,
        ROOT,
        api,
        project_config.load_project_configs(),
        upsert_tasks,
        key_prefix="exec_queue",
        entries=open_entries,
        show_heading=False,
    )

    if waiting_sessions:
        st.caption("Прогоны, готовящиеся к старту (обычно исчезают за секунды):")
        for session in waiting_sessions:
            _render_execution_center_card(api, session, tasks_by_id, now=now)


_LAUNCH_FLASH_KEY = "exec_board_launch_flash"
_LAUNCH_BOARD_LIMIT = 12
_PROJECT_TREE_KEY = "exec_board_project_tree"

# Flash keys for messages that must survive an immediate `st.rerun()` (same
# pattern as `_LAUNCH_FLASH_KEY`): the frame that renders a message right
# before `st.rerun()` is replaced by the rerun, so the message is stored here
# and re-rendered (then popped) on the post-rerun frame instead.
_IMPORT_TASK_FLASH_KEY = "import_task_package_flash"
_PROJECT_SETTINGS_FLASH_KEY = "project_settings_saved_flash"


def _render_project_tree_section(
    api: runtime_api.ExecutionCenterAPI,
    project_id: str,
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
    running_task_ids: frozenset[str],
) -> None:
    """A project's whole plan as dependency levels, coloured by what each task
    is actually doing.

    This is the "open a project and see the tree" view: level 0 is what can
    start now, each level below it unlocks when the one above is merged. Colour
    carries the state — green merged, orange running, red stopped, blue ready,
    grey waiting — so the shape of the remaining work reads without reading a
    single status word.

    Ready tasks carry their own launch button, gated by the same
    `live_board.launch_gate` the launch panel uses, so a level can be started
    from the level view rather than by hunting the task down elsewhere."""
    project_tasks = [t for t in tasks if project_config.project_matches(t.get("project"), project_id)]
    if not project_tasks:
        st.info(f"У проекта {project_id} нет задач.")
        return

    nodes = live_board.project_tree(project_tasks, tasks_by_id, running_task_ids=running_task_ids)
    done, total = live_board.project_progress(nodes)

    st.markdown(f"#### 🌳 {project_id} — дерево задач")
    st.progress(done / total if total else 0.0, text=f"Смёржено {done} из {total}")

    active_runs = api.list_runs(states=runtime_db.EXECUTION_CENTER_ACTIVE_STATES)
    current_level = None
    for node in nodes:
        if node.level != current_level:
            current_level = node.level
            level_nodes = [n for n in nodes if n.level == current_level]
            level_done = sum(1 for n in level_nodes if n.state == live_board.NODE_DONE)
            st.markdown(
                f"**Уровень {current_level}** · {level_done}/{len(level_nodes)} "
                + ("✅ пройден" if level_done == len(level_nodes) else "в работе")
            )

        task = tasks_by_id.get(node.task_id, {})
        with st.container(border=True):
            row = st.columns([5, 2, 2, 2], vertical_alignment="center")
            row[0].markdown(f"{node.mark} :{node.color}[**{node.title[:70]}**]")
            row[1].caption(f"{node.state_label} · {node.priority or '—'}")
            with row[2]:
                if st.button(
                    "Детали", key=f"exec_tree_detail_{node.task_id}", icon=":material/task_alt:",
                    help="Открыть детали задачи", width="stretch",
                ):
                    _open_task_detail(node.task_id)
                    st.rerun()
            with row[3]:
                if node.state in (live_board.NODE_READY, live_board.NODE_BLOCKED):
                    gate = live_board.launch_gate(
                        task, tasks_by_id=tasks_by_id, active_runs=active_runs
                    )
                    if st.button(
                        "Запустить",
                        key=f"exec_tree_launch_{node.task_id}",
                        icon=":material/rocket_launch:",
                        type="primary" if node.is_next else "secondary",
                        disabled=not gate.allowed,
                        width="stretch",
                        help=gate.reason if not gate.allowed else "Поставить в очередь и запустить сейчас.",
                    ):
                        recheck = live_board.launch_gate(
                            task,
                            tasks_by_id=tasks_by_id,
                            active_runs=api.list_runs(states=runtime_db.EXECUTION_CENTER_ACTIVE_STATES),
                        )
                        if not recheck.allowed:
                            st.session_state[_LAUNCH_FLASH_KEY] = {"ok": False, "message": recheck.reason}
                        else:
                            _launch_task_from_board(api, task, tasks, tasks_by_id)
                        st.rerun()
            if node.is_next:
                st.caption("⟵ Следующая по плану: этот уровень открыт, начинать с неё.")


def _render_dependency_tree(task: dict, tasks_by_id: dict[str, dict]) -> None:
    """The task's blocking chain as indented text.

    Text rather than the Graphviz chart used on the task card: this renders
    inline under a launch button, where the question is the narrow one the
    button raises — "what is holding this, and is it done?" — and a rendered
    graph answers it slower than four indented lines. The chart remains on the
    task card for reading the shape of a neighbourhood."""
    nodes = live_board.dependency_tree(task, tasks_by_id)
    if not nodes:
        st.caption("Зависимостей нет.")
        return
    for node in nodes:
        indent = "&nbsp;" * 4 * (node.depth - 1)
        mark = "✅" if node.done else "⏳"
        st.markdown(
            f"<div style='font-size:0.82rem;opacity:0.85'>{indent}"
            f"{live_board.relation_mark(node.relation)} {mark} {html.escape(node.title[:64])} "
            f"<code>{html.escape(node.status)}</code></div>",
            unsafe_allow_html=True,
        )


def _launch_task_from_board(
    api: runtime_api.ExecutionCenterAPI,
    task: dict,
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
) -> None:
    """Enqueue this task and launch that one entry — the same locked path the
    Execution Queue panel uses, never a second launch implementation.

    `enqueue_and_persist` is idempotent per task, and `launch_ready` re-derives
    readiness under the queue lock, so this cannot double-launch a task another
    session queued in between. It also keeps the guarantee that matters most:
    an entry whose pre-flight carries warnings (dirty tree, detached HEAD) is
    *not* launched here — a board button is a batch action with no per-task
    human in the loop, exactly the case `launch_ready` refuses. The refusal and
    its reason are flashed back rather than swallowed."""
    execution_queue.enqueue_and_persist(ROOT, task, tasks_by_id)
    entries = execution_queue.reevaluate_and_persist(ROOT, tasks_by_id)
    entry = next(
        (e for e in entries if e.get("task_id") == task.get("id") and e.get("state") in execution_queue.OPEN_STATES),
        None,
    )
    if entry is None:
        st.session_state[_LAUNCH_FLASH_KEY] = {"ok": False, "message": "Задача не попала в очередь запуска."}
        return

    _, results = execution_queue.launch_ready(
        ROOT,
        entries,
        tasks,
        tasks_by_id,
        project_config.load_project_configs(),
        api,
        entry_ids=[entry["id"]],
    )
    # `launch_ready` mutates the launched task dict in place, exactly like
    # `launch_service` does; commit that with the same locked bulk upsert the
    # queue panel uses. Never a whole-snapshot write — see the note above
    # `upsert_tasks` on why `save_tasks(tasks)` does not exist here.
    upsert_tasks(tasks)
    launched = [r for r in results if r.launched]
    if launched:
        st.session_state[_LAUNCH_FLASH_KEY] = {
            "ok": True,
            "message": f"Запущено: {task.get('title') or task.get('id')}.",
        }
        return
    skipped = results[0] if results else None
    st.session_state[_LAUNCH_FLASH_KEY] = {
        "ok": False,
        "message": (skipped.message if skipped else "Запуск не выполнен.")
        + " Задача осталась в очереди — запустите её с подтверждением из карточки задачи.",
    }


def _render_launch_board(
    api: runtime_api.ExecutionCenterAPI, tasks: list[dict], tasks_by_id: dict[str, dict]
) -> None:
    """Launch a task without leaving the board, with its dependency chain and
    an honest reason whenever the button is disabled.

    The gate (`live_board.launch_gate`) reuses the autopilot planner's reason
    codes, so a task the wave calls `workspace_busy` shows the same words here.
    It is an affordance, not a safety boundary — the fail-closed checks stay in
    `launch_service`/`Supervisor.start_raw`, which is why the button being
    enabled is never treated as permission by anything downstream."""
    flash = st.session_state.pop(_LAUNCH_FLASH_KEY, None)
    if flash:
        (st.success if flash["ok"] else st.warning)(flash["message"])

    # One read of the active-run table for the whole board, rather than one per
    # rendered button.
    active_runs = api.list_runs(states=runtime_db.EXECUTION_CENTER_ACTIVE_STATES)
    candidates = [
        task
        for task in tasks
        if (task.get("status") or "") not in ("Done",)
        and task.get("status") in ("Next", "In Progress", "Backlog", "Blocked")
    ]
    gated = [
        (task, live_board.launch_gate(task, tasks_by_id=tasks_by_id, active_runs=active_runs))
        for task in candidates
    ]
    # Launchable first, then conflicts (which clear on their own), then the
    # rest — the operator's own order of interest.
    gated.sort(key=lambda pair: (not pair[1].allowed, not pair[1].is_conflict, pair[0].get("title") or ""))
    ready_count = sum(1 for _, gate in gated if gate.allowed)

    with st.expander(f"🚀 Запуск задачи ({ready_count} готовы)", expanded=False):
        if not gated:
            st.caption("Нет задач, доступных для запуска.")
            return
        st.caption(
            "Кнопка заблокирована, когда запуск конфликтует: занятый workspace, "
            "уже активная попытка или незавершённые зависимости."
        )
        for task, gate in gated[:_LAUNCH_BOARD_LIMIT]:
            with st.container(border=True):
                row = st.columns([5, 2, 2])
                row[0].markdown(f"**{(task.get('title') or task.get('id'))[:70]}**")
                row[1].caption(f"{task.get('project') or '—'} · {task.get('priority') or '—'}")
                with row[2]:
                    if st.button(
                        "Запустить",
                        key=f"exec_board_launch_{task.get('id')}",
                        icon=":material/rocket_launch:",
                        type="primary" if gate.allowed else "secondary",
                        disabled=not gate.allowed,
                        width="stretch",
                        help=gate.reason if not gate.allowed else "Поставить в очередь и запустить сейчас.",
                    ):
                        # Re-check server-side: `disabled=` is a client-side
                        # affordance and `AppTest.click()` does not honour it.
                        # The same defense-in-depth convention as the card's
                        # cancel control.
                        recheck = live_board.launch_gate(
                            task, tasks_by_id=tasks_by_id, active_runs=api.list_runs(
                                states=runtime_db.EXECUTION_CENTER_ACTIVE_STATES
                            )
                        )
                        if not recheck.allowed:
                            st.session_state[_LAUNCH_FLASH_KEY] = {"ok": False, "message": recheck.reason}
                        else:
                            _launch_task_from_board(api, task, tasks, tasks_by_id)
                        st.rerun()

                if not gate.allowed:
                    st.caption(f"⛔ {gate.reason} · код `{gate.code}`")
                    if gate.action:
                        st.caption(f"Что делать: {gate.action}")
                _render_dependency_tree(task, tasks_by_id)

        if len(gated) > _LAUNCH_BOARD_LIMIT:
            st.caption(f"Показаны {_LAUNCH_BOARD_LIMIT} из {len(gated)}.")


def _run_autopilot_tick(api: runtime_api.ExecutionCenterAPI):
    """One bounded `task_pipeline.tick`, or `None` if it could not even be
    attempted.

    Isolated behind a broad `except` on purpose: the autopilot is an optional,
    opt-in convenience layered on top of the Live Execution Center, and an
    unexpected fault inside it must degrade to "no autopilot this refresh"
    rather than take down the dashboard that operators use to see and cancel
    real running processes. The failure is surfaced in the panel, not
    swallowed silently."""
    try:
        return task_pipeline.tick(ROOT, api, project_config.load_project_configs())
    except Exception as exc:  # noqa: BLE001 — never let autopilot break the dashboard
        st.session_state[autopilot_panel.TICK_ERROR_KEY] = str(exc)
        return None


# How often the autopilot may actually plan, independent of how often the board
# redraws. Measured against a real database, one `task_pipeline.tick` costs
# ~540 ms while reading and rendering the entire board costs ~150 ms — so on
# the 2-5 s display refresh the pipeline owned most of every cycle, and each
# refresh blanked the page for half a second before drawing anything. That is
# what read as "the dashboard blinks" and, caught mid-tick, as "the sections
# disappeared".
#
# The two cadences are genuinely independent concerns: the display interval is
# how fresh the operator's picture is, the tick interval is how eagerly work is
# planned. Nothing is lost by planning every 15 s — the tick is idempotent,
# holds a host-wide lock, and the guide is explicit that if it is not run, the
# autopilot simply does nothing.
_PIPELINE_TICK_MIN_INTERVAL_SECONDS = 15.0
_PIPELINE_TICK_AT_KEY = "exec_center_pipeline_tick_monotonic"


def _maybe_run_autopilot_tick(api: runtime_api.ExecutionCenterAPI):
    """`_run_autopilot_tick`, rate-limited to one tick per
    `_PIPELINE_TICK_MIN_INTERVAL_SECONDS`.

    Returns `None` when the tick was skipped, which callers already treat as
    "no new wave this refresh" — the last real wave stays on screen rather
    than being replaced by an empty one.

    `time.monotonic` rather than wall-clock: this is an interval, and a clock
    adjustment must not be able to stall the autopilot or let it free-run."""
    now = time.monotonic()
    last = st.session_state.get(_PIPELINE_TICK_AT_KEY)
    if last is not None and (now - last) < _PIPELINE_TICK_MIN_INTERVAL_SECONDS:
        return None
    st.session_state[_PIPELINE_TICK_AT_KEY] = now
    return _run_autopilot_tick(api)


def _render_live_execution_center_body(api: runtime_api.ExecutionCenterAPI, tasks: list[dict]) -> None:
    """One refresh tick's worth of work: reconcile+sync, then re-render the
    whole dashboard from freshly-read state. Called directly (no
    auto-refresh) or from one of the fixed-interval poller fragments below.

    Reconciliation runs against a *freshly loaded* task list inside
    `tasks_repository.mutate_tasks` — never the possibly several-seconds-old
    `tasks` this function was called with — and only persists when something
    actually changed (`persist_if`), so an idle poll tick costs a lock
    acquisition (cheap, uncontended) but not a disk write. `tasks` is then
    rebound to that fresh, reconciled list for the rest of this render."""
    now = datetime.now()

    # Desktop autopilot (AICC-DESKTOP-016). The bounded pipeline tick runs from
    # *this* existing refresh checkpoint — the same one that already owns
    # reconcile-and-sync — and never from a second Supervisor, a background
    # thread, or a poller of its own. `tick` returns immediately doing nothing
    # when autopilot is not explicitly opted in (the default) or when another
    # process already holds the pipeline lock, so this call is safe to make on
    # every refresh. It subsumes the reconcile+sync and queue re-evaluation
    # below; those still run for the disabled case, which is the normal one.
    #
    # Throttled — see `_maybe_run_autopilot_tick`. The tick is the one part of a
    # refresh that costs half a second, and it runs at most once per
    # `_PIPELINE_TICK_MIN_INTERVAL_SECONDS`. The spinner is shown ONLY on those
    # rare tick refreshes, never on the frequent light ones: a spinner on every
    # 3-second poll is exactly the "страница то активна, то сереет" flicker —
    # Streamlit dims the fragment while a spinner is open, so an always-on
    # spinner greys the board on every single refresh. The light reconcile+sync
    # below is ~100 ms and needs no spinner; it re-renders without dimming.
    # No st.spinner around the tick: st.spinner dims the fragment while it is
    # open, and a dim on every throttled tick is exactly the "страница то
    # активна, то сереет" flicker operators reported (it recurred even after the
    # settings-revert fix). `_maybe_run_autopilot_tick` already self-throttles to
    # at most once per _PIPELINE_TICK_MIN_INTERVAL_SECONDS and returns None
    # (doing nothing) both when the tick is not yet due and when autopilot is not
    # opted in — so the frequent light refreshes cost nothing and never dim, and
    # the rare planning tick now runs silently in place instead of greying the
    # board.
    tick_result = _maybe_run_autopilot_tick(api)

    # Reconcile live runs + re-project execution state, then project every
    # *verified* completion onto the Kanban `Done` lane. The completion
    # projection previously ran only inside the (default-off) autopilot tick, so
    # a merge verified present in the target branch never reached the board
    # unless autopilot was enabled — stranding genuinely merged tasks in Backlog
    # with their dependents blocked (audit DATA-D2). `sync_on_refresh` is
    # idempotent and persists only on change, so it is safe on every refresh.
    tasks, _projected_done_ids = task_pipeline.sync_on_refresh(ROOT, api)

    # Queue readiness has no poller of its own (see `execution_queue`'s
    # module docstring — no hidden scheduler); it piggybacks on this
    # existing reconcile-on-refresh-tick checkpoint instead, exactly like
    # `Supervisor.reconcile()` does. Relabels waiting/ready only — never
    # launches anything.
    execution_queue.reevaluate_and_persist(ROOT, {t["id"]: t for t in tasks if t.get("id")})

    if tick_result is not None and tick_result.ran:
        # Durable outcome for the autopilot panel (AICC-DESKTOP-017): stashing
        # it rather than rendering inline is what keeps launches/skips/merge
        # results visible across the rerun this refresh path performs.
        st.session_state[autopilot_panel.TICK_RESULT_KEY] = tick_result

    sessions, tasks_by_id = _build_execution_center_sessions(api, tasks, now=now)

    # The display status is computed once, here, and carried on the session —
    # the board buckets by it, the cards badge by it, and neither can drift
    # from the other by re-deriving it independently.
    for session in sessions:
        session["display_status"] = _execution_center_display_status(session)
    board = live_board.split_board(sessions, display_status="display_status")

    # Drop superseded attempts from the attention bucket: a task that failed
    # once and then succeeded (or is running again) must not keep its old failed
    # run sitting in "Requires Attention" — a newer run for that task already
    # moved past it (this was "the task shows as needing attention even though
    # it finished"). Only the attention bucket is filtered; the superseded run
    # still exists in the run journal and its own terminal bucket.
    superseded = live_board.superseded_run_ids(sessions)
    resolved = live_board.completed_task_run_ids(sessions, tasks_by_id)
    hidden_attention_run_ids = superseded | resolved
    if hidden_attention_run_ids:
        board[live_board.BUCKET_ATTENTION] = [
            s
            for s in board[live_board.BUCKET_ATTENTION]
            if s["run_id"] not in hidden_attention_run_ids
        ]

    queue_entries = execution_queue.load_queue(ROOT)
    reconciled_queue = execution_queue.reconcile_missing_run_links(ROOT, queue_entries)
    if reconciled_queue != queue_entries:
        execution_queue.save_queue(ROOT, reconciled_queue)
        queue_entries = reconciled_queue
    dismissed_attention = st.session_state.get(_ATTENTION_DISMISSED_KEY, set())
    visible_board = {
        bucket: list(rows)
        for bucket, rows in board.items()
    }
    if dismissed_attention:
        visible_board[live_board.BUCKET_ATTENTION] = [
            row
            for row in visible_board[live_board.BUCKET_ATTENTION]
            if row.get("run_id") not in dismissed_attention
        ]
    counts = execution_metrics.counts_for_snapshot(visible_board, queue_entries)

    board_style.begin()
    _render_board_summary(board, counts)
    _render_console_actions(tasks, tasks_by_id)

    # Wide main column for what the operator acts on; narrow side column for
    # standing context. The projects strip and the autopilot wave are context:
    # worth a glance, never worth the top of the screen.
    running_task_ids = frozenset(
        s["task_id"] for s in board[live_board.BUCKET_LIVE] if s.get("task_id")
    )

    _maybe_open_task_detail(tasks_by_id)

    main, side = st.columns([3, 1], gap="medium")
    with main:
        _render_board_sections(
            api,
            board,
            tasks,
            tasks_by_id,
            queue_entries,
            now=now,
        )
        selected_project = st.session_state.get(_PROJECT_TREE_KEY)
        if selected_project:
            _render_project_tree_section(api, selected_project, tasks, tasks_by_id, running_task_ids)
        _render_launch_board(api, tasks, tasks_by_id)
    with side:
        _render_capacity_panel(api)
        _render_execution_center_project_overview(sessions, now)
        # Only a tick that actually ran replaces the wave on screen. A disabled
        # or busy tick carries no wave, and letting it through would wipe the
        # last real one — leaving the operator staring at "нет данных"
        # mid-session.
        autopilot_panel.render_autopilot_wave(
            tick_result if tick_result is not None and tick_result.ran else None,
        )

    st.session_state["exec_center_last_refreshed_at"] = now.strftime("%H:%M:%S")


# Three fixed-interval monitoring pollers (15/30/60s) —
# `st.fragment(run_every=...)`
# requires a static interval per decorated function, so a user-configurable
# interval is implemented as a small fixed set of pollers, dispatched to by
# `render_live_execution_center` below, rather than any unmanaged background
# thread or a dynamically-parameterized refresh mechanism.
@st.fragment(run_every=15.0)
def _render_live_execution_center_poll_15s(api: runtime_api.ExecutionCenterAPI, tasks: list[dict]) -> None:
    _render_live_execution_center_body(api, tasks)


@st.fragment(run_every=30.0)
def _render_live_execution_center_poll_30s(api: runtime_api.ExecutionCenterAPI, tasks: list[dict]) -> None:
    _render_live_execution_center_body(api, tasks)


@st.fragment(run_every=60.0)
def _render_live_execution_center_poll_60s(api: runtime_api.ExecutionCenterAPI, tasks: list[dict]) -> None:
    _render_live_execution_center_body(api, tasks)


_EXECUTION_CENTER_POLLERS = {
    15: _render_live_execution_center_poll_15s,
    30: _render_live_execution_center_poll_30s,
    60: _render_live_execution_center_poll_60s,
}


def render_live_execution_center(api: runtime_api.ExecutionCenterAPI, tasks: list[dict]) -> None:
    """Top-level Live Execution Center v2 dashboard: refresh controls,
    Project Overview row, and the 5-section session dashboard. Reconciles
    every persisted `RUNNING` row against real OS processes and syncs any
    linked Kanban task's `launch_status` on every render — see
    `task_sync.reconcile_and_sync` (always the existing `Supervisor`, never
    a second execution engine)."""
    # One-time migration from the old always-on 2-5 second poller. That mode
    # rebuilt the entire interactive board while the operator was clicking or
    # typing. The default is now stable/manual; monitoring remains an explicit
    # opt-in at a humane cadence.
    if not st.session_state.get("exec_center_refresh_v2_migrated"):
        st.session_state["exec_center_auto_refresh"] = False
        st.session_state["exec_center_refresh_interval"] = 30
        st.session_state["exec_center_refresh_v2_migrated"] = True

    header_cols = st.columns([1.4, 1, 1, 2])
    with header_cols[0]:
        # session_state (seeded by the v2 migration above) is the single source
        # of truth for this keyed widget; passing `value=` alongside a
        # session-state-set key is what Streamlit warns about, so it is omitted.
        auto_refresh = st.toggle(
            "Режим мониторинга",
            key="exec_center_auto_refresh",
            help="Периодически обновляет всю доску. Выключите во время работы с карточками.",
        )
    with header_cols[1]:
        # Sanitize any stale/invalid stored value, then let the keyed widget read
        # it directly — no `index=` default, which Streamlit forbids alongside a
        # session-state-set key.
        if st.session_state.get("exec_center_refresh_interval") not in _EXECUTION_CENTER_POLLERS:
            st.session_state["exec_center_refresh_interval"] = 30
        interval = st.selectbox(
            "Интервал (с)",
            list(_EXECUTION_CENTER_POLLERS),
            key="exec_center_refresh_interval",
        )
    with header_cols[2]:
        st.write("")
        st.button("Обновить сейчас", icon=":material/refresh:", key="exec_center_refresh_now")
    with header_cols[3]:
        st.write("")
        st.caption(f"Обновлено: {st.session_state.get('exec_center_last_refreshed_at') or '—'}")

    # The button click has already caused this script run, so no second
    # ``st.rerun`` is needed. The old explicit rerun doubled the repaint.

    # The autopilot surface renders *before* the poller fragment below, so its
    # controls stay interactive at a fixed position instead of being torn down
    # and rebuilt on every fragment refresh. It reads the tick result the
    # refresh path stashes; it never runs a tick itself.
    # Controls only. The wave is rendered inside the refresh body below, where
    # it re-renders on every tick; here it would freeze at first load.
    with st.expander("Автопилот рабочего стола", icon=":material/auto_mode:"):
        autopilot_panel.render_autopilot_controls(ROOT)

    if auto_refresh:
        _EXECUTION_CENTER_POLLERS[interval](api, tasks)
    else:
        _render_live_execution_center_body(api, tasks)

    with st.expander("Запустить новый прогон (ad-hoc, без привязки к задаче)", icon=":material/smart_toy:"):
        execution_center_form.render_execution_center_launch_form(api)
