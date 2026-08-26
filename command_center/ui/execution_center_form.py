"""Execution Center ad-hoc launch form extracted from ``app.py``
(NIGHT-W9-AICC-ARCH slice 5).

Confirm-then-execute launcher for the v2 runtime plus the run-state display
vocabulary (``EXECUTION_CENTER_STATE_LABELS``) it shares with the monitor and
the workspace-home run badges. Pure move, no behavior change; ``app.py``
re-exports both names by assignment.

Thin consumer of the frozen Sprint 1 runtime: every launch goes through
``runtime_api.ExecutionCenterAPI.start_run`` — no Supervisor internals, no
raw SQL, no new engine.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from command_center import agent_runner, executors, launch_service, models, project_config
from command_center.runtime import api as runtime_api
from command_center.runtime import context_service as runtime_context_service
from command_center.runtime import supervisor as runtime_supervisor
from command_center.ui.agent_launcher import TASK_TYPE_LABELS, TASK_TYPES

EXECUTION_CENTER_STATE_LABELS: dict[str, str] = {
    "PREPARED": "Подготовлен",
    "QUEUED": "В очереди",
    "RUNNING": "Выполняется",
    "COMPLETED": "Завершено",
    "FAILED": "Ошибка",
    "CANCELLED": "Отменено",
    "INTERRUPTED": "Прервано",
    "UNKNOWN": "Неизвестно",
}


def render_execution_center_launch_form(api: runtime_api.ExecutionCenterAPI) -> None:
    """Confirm-then-execute launcher for the v2 runtime, mirroring
    `render_agent_launcher`'s existing confirm/warn/disable pattern for
    BANK/LEGAL, but calling `ExecutionCenterAPI.start_run` (non-blocking:
    the Supervisor launches the subprocess and returns immediately with the
    run in state RUNNING) instead of `agent_runner.run_claude_code`
    (synchronous, blocks the whole script inside `st.spinner`)."""
    launch_project = st.selectbox("Проект", models.PROJECT_IDS, key="exec_center_launch_project")
    cfg = project_config.get_project_config(launch_project)
    repo_path = cfg.get("repository_path")
    try:
        executor_options = list(project_config.allowed_execution_providers(launch_project))
    except project_config.ProviderAuthorizationError as exc:
        st.error(str(exc))
        return
    executor_id = st.selectbox(
        "Execution provider",
        executor_options,
        format_func=lambda value: executors.get_executor(value).label,
        key="exec_center_launch_executor",
    )
    selected_executor = executors.get_executor(executor_id)
    provider_availability = selected_executor.availability
    if provider_availability is not None:
        if provider_availability.available:
            st.caption(
                f"Provider: {selected_executor.label} · available"
                + (f" · {provider_availability.version}" if provider_availability.version else "")
            )
        else:
            st.error(
                f"{selected_executor.label} unavailable ({provider_availability.code}): "
                f"{provider_availability.message}"
            )

    task_type = st.selectbox(
        "Тип задачи",
        TASK_TYPES,
        format_func=lambda value: TASK_TYPE_LABELS.get(value, value),
        key="exec_center_launch_task_type",
    )
    instruction = st.text_area(
        "Инструкция для агента", key="exec_center_launch_instruction", height=160
    )
    timeout_seconds = st.number_input(
        "Таймаут (секунды)",
        min_value=agent_runner.MIN_TIMEOUT_SECONDS,
        max_value=agent_runner.MAX_TIMEOUT_SECONDS,
        value=runtime_api.DEFAULT_TIMEOUT_SECONDS,
        step=30,
        key="exec_center_launch_timeout",
    )

    if not repo_path:
        st.error(
            f"Путь к репозиторию не настроен для проекта {launch_project}. "
            "Настройте его в разделе «Проекты» → «Настройки репозитория»."
        )
        return

    st.caption(f"Репозиторий: `{repo_path}`")

    confirmed = st.checkbox(
        f"Я подтверждаю запуск {selected_executor.label} с указанными параметрами.",
        key="exec_center_launch_confirm",
    )
    sensitivity_ack = True
    if cfg.get("sensitive"):
        st.warning(
            f"Проект {launch_project} — чувствительный (BANK/LEGAL). Дополнительный "
            "контент не прикладывается автоматически — инструкция отправляется как есть."
        )
        sensitivity_ack = st.checkbox(
            "Я подтверждаю, что не прикладываю дополнительный чувствительный контент "
            "без явного разрешения.",
            key="exec_center_launch_sensitivity_ack",
        )

    codex_target_unsafe = executor_id == "codex"
    if codex_target_unsafe:
        st.error(
            "Codex CLI requires a dedicated task worktree and intended task branch; "
            "the ad-hoc form targets the canonical project checkout. Launch Codex from a task instead."
        )
    ready = (
        confirmed
        and sensitivity_ack
        and bool(instruction.strip())
        and bool(provider_availability and provider_availability.available)
        and not codex_target_unsafe
    )
    launch_clicked = st.button(
        "Запустить",
        type="primary",
        icon=":material/play_arrow:",
        disabled=not ready,
        key="exec_center_launch_btn",
    )
    if not launch_clicked:
        return

    # Re-checked server-side, not just via the button's (client-side-only)
    # `disabled` state — this is the actual gate, matching the rest of the
    # codebase's defense-in-depth convention (e.g. `_assert_no_forbidden_flags`).
    if not ready:
        st.error("Запуск заблокирован: подтвердите все необходимые пункты перед запуском.")
        return

    conflict = launch_service.find_active_run_conflict(
        api, task_id=None, resolved_workspace=str(Path(repo_path).expanduser().resolve())
    )
    if conflict is not None:
        st.error(
            f"У workspace `{repo_path}` уже есть активный прогон (`{conflict['id']}`, "
            f"статус {conflict['state']}) — дождитесь его завершения или отмените перед новым запуском."
        )
        return

    try:
        run = api.start_run(
            project=launch_project,
            repository_path=repo_path,
            task_type=task_type,
            instruction=instruction,
            confirmed=confirmed,
            timeout_seconds=int(timeout_seconds),
            executor_id=executor_id,
        )
    except (
        runtime_context_service.ConfirmationRequiredError,
        agent_runner.RunnerError,
        project_config.ProviderAuthorizationError,
        runtime_supervisor.SupervisorError,
    ) as exc:
        st.error(str(exc))
        return

    st.success(f"Запуск создан: `{run['id']}` (статус: {EXECUTION_CENTER_STATE_LABELS.get(run['state'], run['state'])})")
    st.session_state.pending_exec_center_run = run["id"]
    st.rerun()
