"""Agent launcher widgets extracted from ``app.py`` (NIGHT-W9-AICC-ARCH slice 3).

Holds the confirm-then-execute agent launcher dialog
(``render_agent_launcher``) and the "create next task" follow-up widget
(``render_create_next_task_widget``) that used to live inline in ``app.py``
(its "Agent workflow (v1.2)" section), plus their two private preflight
helpers and the ``TASK_TYPE_LABELS`` vocabulary they render with. Pure move —
no behavior change; ``app.py`` re-exports every public name by assignment.

This module renders with Streamlit (the documented ``command_center.ui``
exception) but keeps the package's boundary: no business logic of its own —
validation/classification comes from ``launch``/``launch_service``, task
persistence goes through ``ui.legacy_task_helpers`` → ``tasks_repository``
(the single writer of ``data/tasks.json`` per ``docs/AUTHORITY_MAP.md``), and
run execution goes through the app's one ``ExecutionCenterAPI`` singleton.

That singleton is ``app.py``'s ``st.cache_resource`` accessor — it stays in
``app.py`` (one Supervisor per server process; a second decorated accessor
here would mean a second Supervisor, i.e. a second engine). ``app.py`` hands
it in once via :func:`configure`; the seam keeps this module import-safe
(no circular ``import app``) and pytest-testable with a fake factory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import streamlit as st

from command_center import (
    activity_log,
    agent_runner,
    artifacts,
    executors,
    launch,
    launch_service,
    models,
    project_config,
    tasks_repository,
    workflow,
    workspace_provisioning,
)
from command_center.runtime import context_service as runtime_context_service
from command_center.runtime import supervisor as runtime_supervisor
from command_center.ui import legacy_task_helpers

# Same repository root `app.py` binds its own ROOT to (this file lives at
# command_center/ui/agent_launcher.py, two levels below it).
ROOT = Path(__file__).resolve().parents[2]

# Canonical source: command_center.artifacts.TASK_TYPES — see that module's
# docstring for why no duplicate list may exist.
TASK_TYPES: tuple[str, ...] = artifacts.TASK_TYPES

TASK_TYPE_LABELS: dict[str, str] = {
    "implementation": "Реализация",
    "review": "Ревью",
    "remediation": "Исправление",
    "final_gate": "Финальная проверка",
    "architecture_review": "Архитектурный обзор",
}

# --- ExecutionCenterAPI seam -------------------------------------------------
# `app.py` owns the one `st.cache_resource` `ExecutionCenterAPI` singleton
# (see its `get_execution_center_api` docstring: one Supervisor per Streamlit
# server process). It injects that accessor here, once, right after defining
# it. Tests inject a fake the same way.

_execution_center_api_factory: Callable[[], Any] | None = None


def configure(*, execution_center_api_factory: Callable[[], Any]) -> None:
    global _execution_center_api_factory
    _execution_center_api_factory = execution_center_api_factory


def _get_execution_center_api() -> Any:
    if _execution_center_api_factory is None:
        raise RuntimeError(
            "agent_launcher is not configured: call agent_launcher.configure("
            "execution_center_api_factory=...) with the app's ExecutionCenterAPI "
            "singleton accessor before rendering (app.py does this right after "
            "defining get_execution_center_api)."
        )
    return _execution_center_api_factory()


def _preselected_executor_id(*, cfg: dict, task: dict | None, options: list[str] | None = None) -> str:
    """Which executor the launcher will preselect: the task's own, else the
    project default, else `claude_code` — clamped to `options` (the project's
    authorized providers) exactly as the confirmation dialog's selectbox
    clamps it, so the pre-dialog preflight below can never warn about a
    provider the dialog is not going to offer."""
    configured = (task or {}).get("executor") or cfg.get("default_executor") or "claude_code"
    if options and configured not in options:
        return options[0]
    return configured


def _claude_cli_preflight(executor_id: str) -> str | None:
    """The message to show when `executor_id` is Claude Code but its CLI is
    not on PATH; `None` when nothing is wrong (or another provider is
    selected — each provider carries its own availability probe).

    Probes `runtime_supervisor.CLAUDE_BINARY`, the executable the v2 Session
    Supervisor will actually exec, not a separately-guessed name."""
    if executor_id != "claude_code":
        return None
    available, message = agent_runner.claude_cli_preflight(runtime_supervisor.CLAUDE_BINARY)
    return None if available else message


def render_agent_launcher(
    *,
    key_prefix: str,
    project: str,
    default_prompt: str,
    tasks: list[dict],
    task_id: str | None = None,
    default_task_type: str = "implementation",
) -> None:
    """Confirm-then-execute Claude Code launcher, reused from every required entry
    point (task detail card, Project Chat, AI Agents, generated-task preview).

    The workspace to validate and launch against is resolved exactly once, via
    `launch.resolve_workspace_path` (task workspace_path → project
    default_workspace_path → project repository_path), and that same path is
    then reused for every later step — validation, git reads, the Terminal/
    Folder actions, the actual Claude launch, and persisted launch history —
    instead of being recomputed (and risking drifting) at each step."""
    cfg = project_config.get_project_config(project)
    repo_path = cfg.get("repository_path")
    confirm_key = f"{key_prefix}_confirm_open"
    st.session_state.setdefault(confirm_key, False)
    task_for_launch = next((t for t in tasks if t.get("id") == task_id), None) if task_id else None

    # Preflight (audit MINOR-2): a missing `claude` binary makes the run fail
    # at exec time, which used to be discovered only *after* the operator had
    # walked the entire confirmation flow. Say it here, next to the button
    # that opens that flow. The dialog re-checks the *selected* provider
    # below — this one reports on the provider it will preselect.
    try:
        preselectable_executors = list(project_config.allowed_execution_providers(project))
    except project_config.ProviderAuthorizationError:
        # A broken authorization policy blocks the launch outright and the
        # dialog reports it in full; a CLI warning on top would only be noise.
        preselectable_executors = []
    if preselectable_executors:
        cli_preflight_message = _claude_cli_preflight(
            _preselected_executor_id(cfg=cfg, task=task_for_launch, options=preselectable_executors)
        )
        if cli_preflight_message:
            st.warning(cli_preflight_message)

    if st.button("Запустить агента", key=f"{key_prefix}_open_btn", icon=":material/smart_toy:"):
        # Every launch is confirmed from scratch: a previous dialog's ticked
        # acknowledgements must never carry over into a new one, or an
        # operator would silently inherit a "yes, launch on the wrong branch"
        # from a state of the repository that no longer holds.
        for stale_key in [
            key
            for key in st.session_state
            if isinstance(key, str) and key.startswith(f"{key_prefix}_ack_")
        ]:
            del st.session_state[stale_key]
        st.session_state.pop(f"{key_prefix}_confirmed", None)
        st.session_state[confirm_key] = True

    if not st.session_state[confirm_key]:
        return

    # Rendered as a dialog (not inline `with st.container(...)`) so this
    # multi-column confirmation form always gets a full-width modal surface
    # regardless of how narrow the caller's own layout is — e.g. a single
    # Kanban lane (`st.columns(len(KANBAN_COLUMNS))`), which used to force
    # this entire form into a sliver a few hundred pixels wide.
    @st.dialog("Подтверждение запуска агента", width="large")
    def _render_launch_confirmation() -> None:
        selection = launch.resolve_workspace_path(task=task_for_launch, project_config=cfg)

        if not selection.path:
            st.error(
                f"Не удалось определить workspace для запуска: не заданы ни workspace задачи, "
                f"ни workspace проекта по умолчанию, ни путь к репозиторию для проекта {project} — "
                "ничего не настроено. Настройте путь к репозиторию в разделе «Проекты» → "
                "«Настройки репозитория» или укажите workspace_path у задачи."
            )
            if st.button("Закрыть", key=f"{key_prefix}_cancel_noconfig"):
                st.session_state[confirm_key] = False
                st.rerun()
            return

        if cfg.get("sensitive"):
            st.warning(
                f"Проект {project} — чувствительный (BANK/LEGAL). Файлы не прикрепляются "
                "автоматически: добавьте разрешённый контекст вручную ниже, если он нужен."
            )

        default_type = default_task_type if default_task_type in TASK_TYPES else TASK_TYPES[0]
        task_type = st.selectbox(
            "Тип задачи",
            TASK_TYPES,
            index=TASK_TYPES.index(default_type),
            format_func=lambda value: TASK_TYPE_LABELS.get(value, value),
            key=f"{key_prefix}_task_type",
        )
        try:
            executor_options = list(project_config.allowed_execution_providers(project))
        except project_config.ProviderAuthorizationError as exc:
            st.error(str(exc))
            return
        configured_executor = _preselected_executor_id(
            cfg=cfg, task=task_for_launch, options=executor_options
        )
        executor_id = st.selectbox(
            "Execution provider",
            executor_options,
            index=executor_options.index(configured_executor),
            format_func=lambda value: executors.get_executor(value).label,
            key=f"{key_prefix}_executor",
        )
        selected_executor = executors.get_executor(executor_id)
        provider_availability = selected_executor.availability
        if provider_availability is not None:
            availability_text = (
                f"Доступен: {provider_availability.version or provider_availability.executable or 'да'}"
                if provider_availability.available
                else f"Недоступен ({provider_availability.code}): {provider_availability.message}"
            )
            (st.caption if provider_availability.available else st.error)(availability_text)
        # `ClaudeProvider.availability()` reports "configured", not "installed"
        # (it never probes PATH), so the missing-binary case is caught here —
        # still above the prompt, the confirmation checkbox and the launch
        # button, never after them.
        selected_cli_preflight_message = _claude_cli_preflight(executor_id)
        if selected_cli_preflight_message:
            st.error(selected_cli_preflight_message)
        prompt = st.text_area(
            "Промпт для агента", value=default_prompt, height=220, key=f"{key_prefix}_prompt"
        )
        extra_context = ""
        if cfg.get("sensitive"):
            extra_context = st.text_area(
                "Дополнительный контекст (вставьте вручную при необходимости)",
                key=f"{key_prefix}_extra_context",
                height=120,
            )
        timeout_seconds = st.number_input(
            "Таймаут (секунды)",
            min_value=agent_runner.MIN_TIMEOUT_SECONDS,
            max_value=agent_runner.MAX_TIMEOUT_SECONDS,
            value=agent_runner.DEFAULT_TIMEOUT_SECONDS,
            step=30,
            key=f"{key_prefix}_timeout",
        )

        full_prompt = prompt
        if extra_context.strip():
            full_prompt = f"{prompt}\n\n## Дополнительный контекст (предоставлен пользователем)\n\n{extra_context.strip()}"

        # Shared, non-mutating classification (same service the execution
        # queue uses). Distinguishes a fatal validation error from a
        # missing-but-provisionable workspace so the launch is not permanently
        # blocked just because the isolated worktree does not exist yet — it
        # will be created (and fail-closed verified) downstream on click.
        prep = launch_service.prepare_task_launch(task=task_for_launch, project_config=cfg)
        expected_branch = prep.expected_branch
        base_branch = prep.base_branch
        validation = prep.validation
        actual_branch = (validation.git_status or {}).get("branch") if validation.git_status else None

        st.markdown("**Проверьте перед запуском:**")
        st.write(f"- Проект: `{project}`")
        st.write(f"- Репозиторий проекта: `{repo_path or '—'}`")
        st.write(f"- Выбранный workspace: `{selection.path}`")
        st.write(f"- Источник workspace: {launch.WORKSPACE_SOURCE_LABELS.get(selection.source, selection.source)}")
        st.write(f"- Ожидаемая ветка: `{expected_branch or '—'}`")
        st.write(f"- Текущая ветка: `{actual_branch or '—'}`")
        st.write(f"- Агент: `{executor_id}` ({selected_executor.label})")
        st.write(f"- Тип задачи: `{task_type}`")

        if prep.provisionable:
            # Recoverable, not fatal: don't render the "not found" error, tell
            # the operator the isolated worktree will be created automatically.
            st.info(prep.provision_notice or "Workspace будет создан автоматически как изолированный worktree.")
        else:
            for error in validation.errors:
                st.error(error)
        for warning in validation.warnings:
            st.warning(warning)

        st.markdown("**Workspace-действия (не запускают агента):**")
        workspace_action_cols = st.columns(3)
        with workspace_action_cols[0]:
            if st.button("Открыть Workspace", key=f"{key_prefix}_open_folder"):
                action_ok, action_message = launch.open_folder_at(selection.path)
                (st.success if action_ok else st.error)(action_message)
        with workspace_action_cols[1]:
            if st.button("Открыть терминал", key=f"{key_prefix}_open_terminal"):
                action_ok, action_message = launch.open_terminal_at(selection.path)
                (st.success if action_ok else st.error)(action_message)
        with workspace_action_cols[2]:
            if st.button("Копировать промпт", key=f"{key_prefix}_copy_prompt"):
                action_ok, action_message = launch.copy_to_clipboard(full_prompt)
                (st.success if action_ok else st.error)(action_message)

        # Provenance-based capability gating (audit D7): an imported (untrusted)
        # task runs read-only by default. Offer the operator an explicit, per-task
        # elevation to full development capability (Bash) at the point of launch.
        elevate_trust = False
        untrusted_import = bool(
            task_for_launch and agent_runner.is_untrusted_task(task_for_launch)
        ) and task_type not in agent_runner.READ_ONLY_TASK_TYPES
        if untrusted_import:
            st.warning(
                "Импортированная (недоверенная) задача — по умолчанию запускается "
                "в безопасном read-only режиме (без Bash и изменения файлов)."
            )
            elevate_trust = st.checkbox(
                "Доверять этой задаче: запустить с полными правами разработки (Bash). "
                "Включайте только для задач из проверенного источника.",
                value=bool(task_for_launch.get("trusted_execution_approved")),
                key=f"{key_prefix}_elevate_trust",
            )
        confirmed = st.checkbox(
            "Я подтверждаю запуск внешнего агента с указанными параметрами.",
            key=f"{key_prefix}_confirmed",
        )

        # One acknowledgement per warning, keyed by its stable `code` — never a
        # single collective "подтверждаю несмотря на предупреждения" checkbox.
        # A branch mismatch and a dirty working tree are independent hazards
        # (agent runs on the wrong branch vs. agent edits on top of
        # uncommitted work); the shared checkbox let an operator who had only
        # noticed one of them dismiss both in a single click, which is exactly
        # the accidental launch this gate exists to prevent.
        acknowledged: set[str] = set()
        warning_issues = validation.warning_issues
        if warning_issues:
            st.markdown("**Подтвердите каждое предупреждение отдельно:**")
        for issue in warning_issues:
            if st.checkbox(
                launch.warning_ack_label(issue),
                key=f"{key_prefix}_ack_{issue.code}",
            ):
                acknowledged.add(issue.code)
            st.caption(issue.message)
        unacknowledged = validation.unacknowledged_warning_codes(acknowledged)

        action_cols = st.columns(2)
        with action_cols[0]:
            launch_clicked = st.button(
                "Подтвердить и запустить",
                type="primary",
                key=f"{key_prefix}_launch_btn",
                disabled=(
                    not confirmed
                    # `prep.launchable` supersedes the raw `validation.can_launch`:
                    # a missing-but-provisionable workspace is launchable.
                    or not prep.launchable
                    or bool(unacknowledged)
                    or not bool(provider_availability and provider_availability.available)
                    or bool(selected_cli_preflight_message)
                ),
                icon=":material/play_arrow:",
            )
        with action_cols[1]:
            if st.button("Отмена", key=f"{key_prefix}_cancel_btn"):
                st.session_state[confirm_key] = False
                st.rerun()

        if not launch_clicked:
            return

        # Persist the operator's D7 elevation decision for this untrusted task so
        # the launch below — and every later launch — runs with the chosen
        # capability. Set on the in-memory record too, since that is what the
        # launch path reads this run. Absent/False keeps it read-only.
        if untrusted_import:
            task_for_launch["trusted_execution_approved"] = elevate_trust
            elevate_task_id = task_for_launch.get("id")
            if elevate_task_id:

                def _persist_trust(tasks_list, _tid=elevate_task_id, _value=elevate_trust):
                    for candidate in tasks_list:
                        if candidate.get("id") == _tid:
                            candidate["trusted_execution_approved"] = _value

                tasks_repository.mutate_tasks(ROOT, _persist_trust)

        # Defense in depth: `disabled=` on the button above is the primary
        # gate, but a launch this consequential should not depend solely on
        # a widget attribute — re-check server-side before doing anything.
        # `prep.launchable` admits both an already-valid workspace and a
        # missing-but-provisionable one; a fatal validation error is still
        # refused here, and the fail-closed isolation gate downstream
        # (`provision_and_verify` -> `Supervisor.start_raw`) is unchanged.
        if not prep.launchable:
            st.error("Запуск заблокирован ошибками валидации выше — сначала устраните их.")
            return
        if not confirmed:
            st.error("Подтвердите запуск внешнего агента перед запуском.")
            return
        if unacknowledged:
            missing = "; ".join(
                launch.warning_ack_label(issue)
                for issue in warning_issues
                if issue.code in unacknowledged
            )
            st.error(f"Не подтверждены все предупреждения — запуск заблокирован: {missing}")
            return
        # Same defense in depth for the CLI preflight, and re-probed at click
        # time: the operator may have installed the binary while this dialog
        # was open, so this is a fresh check, not the render-time verdict.
        click_time_preflight_message = _claude_cli_preflight(executor_id)
        if click_time_preflight_message:
            st.error(click_time_preflight_message)
            return

        # `selection.path` was already validated above (existence, is_dir,
        # is a git repo) — resolved the same way `agent_runner.
        # validate_repository` used to (`expanduser().resolve()`), so
        # symlink/`..` tricks can't escape it, but against the *selected*
        # workspace rather than always forcing the project's repository_path.
        resolved_workspace = Path(selection.path).expanduser().resolve()

        # Real, PID-tracked, cancellable v2 run — not a blocking call. The
        # button click above already re-validated `confirmed` and every
        # per-warning acknowledgement server-side, so `confirmed=True` here
        # reflects a genuine, already-checked confirmation, not a bypass of it.
        _repo = tasks_repository.get_repository(ROOT)
        try:
            run = launch_service.execute_agent_launch_v2(
                project=project,
                task_type=task_type,
                prompt=full_prompt,
                timeout_seconds=int(timeout_seconds),
                repository_path=resolved_workspace,
                execution_center_api=_get_execution_center_api(),
                confirmed=True,
                task=task_for_launch,
                executor_id=executor_id,
                validation=validation,
                expected_branch=expected_branch,
                base_branch=base_branch,
                source_repository_path=repo_path,
                on_task_state_changed=(
                    (lambda: _repo.upsert(task_for_launch))
                    if task_for_launch is not None
                    else None
                ),
            )
        except (
            workspace_provisioning.WorkspaceVerificationError,
            runtime_supervisor.WorkspaceVerificationFailed,
        ) as exc:
            # Fail closed: the workspace did not verify (wrong branch, not an
            # isolated worktree, belongs to another repo, ...). The agent was
            # never started; show the structured failure so the operator can
            # fix it, and never fall back to the main repository.
            structured = exc.structured if isinstance(exc, runtime_supervisor.WorkspaceVerificationFailed) else exc.as_dict()
            st.error("Запуск заблокирован: workspace не прошёл обязательную проверку изоляции.")
            st.markdown(
                f"- Проваленная проверка: `{structured['failed_step']}`\n"
                f"- Ожидаемый workspace: `{structured['expected_workspace']}`\n"
                f"- Фактический workspace: `{structured.get('actual_workspace') or '—'}`\n"
                f"- Ожидаемая ветка: `{structured.get('expected_branch') or '—'}`\n"
                f"- Фактическая ветка: `{structured.get('actual_branch') or '—'}`\n"
                f"- Причина: {structured.get('detail') or '—'}\n"
                f"- Рекомендация: {structured.get('remediation') or '—'}"
            )
            return
        except (
            runtime_context_service.ConfirmationRequiredError,
            agent_runner.RunnerError,
            project_config.ProviderAuthorizationError,
            runtime_supervisor.SupervisorError,
            launch_service.DuplicateActiveLaunchError,
        ) as exc:
            st.error(str(exc))
            return

        if task_for_launch is not None:
            # `task_for_launch` was already committed via `on_task_state_changed`
            # above at each of its in-place mutation checkpoints — this final
            # upsert is a defense-in-depth flush in case any *future* code
            # between here and the last checkpoint mutates it again. Never
            # `save_tasks(tasks)`: `tasks` is this script run's own snapshot,
            # loaded once at the top — persisting it verbatim would silently
            # discard whatever a concurrent writer (another tab, an import,
            # ...) committed to `tasks.json` in the meantime.
            _repo.upsert(task_for_launch)

        st.session_state[confirm_key] = False
        st.success(
            f"Запуск начат: `{run['id']}`. Отслеживайте прогресс, workspace, ветку и логи "
            "в разделе «Live Execution Center»."
        )
        st.session_state.pending_nav = "execution_center"
        st.session_state.pending_exec_center_run = run["id"]
        st.rerun()

    _render_launch_confirmation()


def render_create_next_task_widget(run: dict, tasks: list[dict], key_prefix: str) -> None:
    if run.get("status") != "completed":
        st.caption("Кнопка «Создать следующую задачу» доступна только для завершённых запусков.")
        return

    suggestion = workflow.suggest_next_task(run)
    with st.expander("Создать следующую задачу", icon=":material/add_task:"):
        st.caption(
            f"Источник: вердикт «{suggestion['source_verdict'] or 'не определён'}» "
            f"из запуска `{run['id'][:8]}`."
        )
        if suggestion.get("contradictory"):
            st.warning(
                "Отчёт содержит противоречивые вердикты (найдено более одного). "
                "Показан наиболее консервативный вариант — проверьте отчёт вручную, "
                "при необходимости внесите ручную корректировку вердикта на странице "
                "«Журнал запусков» перед созданием задачи."
            )
        project = run.get("project")
        choices = suggestion["task_type_choices"] or TASK_TYPES
        default_type = suggestion["task_type"] or choices[0]
        next_task_type = st.selectbox(
            "Тип следующей задачи",
            choices,
            index=choices.index(default_type) if default_type in choices else 0,
            format_func=lambda value: TASK_TYPE_LABELS.get(value, value),
            key=f"{key_prefix}_next_type",
        )
        objective = st.text_area(
            "Цель следующей задачи (черновик — проверьте перед созданием)",
            value=suggestion["objective_draft"],
            height=220,
            key=f"{key_prefix}_next_objective",
        )
        next_stage = st.selectbox(
            "Стадия workflow",
            models.WORKFLOW_STAGES,
            index=(
                models.WORKFLOW_STAGES.index(suggestion["workflow_stage"])
                if suggestion["workflow_stage"] in models.WORKFLOW_STAGES
                else 0
            ),
            format_func=lambda value: models.WORKFLOW_STAGE_LABELS.get(value, value),
            key=f"{key_prefix}_next_stage",
        )

        if st.button("Создать задачу", key=f"{key_prefix}_create_next_btn", type="primary", icon=":material/add_task:"):
            objective_clean = objective.strip()
            if not objective_clean:
                st.error("Укажите цель задачи.")
                return
            new_task = legacy_task_helpers.create_task(
                project,
                models.derive_short_title(objective_clean),
                next_task_type,
                "Backlog",
                goal=objective_clean,
                parent_task_id=run.get("task_id"),
                prior_run_id=run["id"],
                workflow_stage=next_stage,
                # The objective is drafted from the parent run's report output
                # (untrusted agent text), so mark the follow-up untrusted — it
                # launches read-only until an operator elevates it (SEC-D-02).
                untrusted_import=True,
            )
            run["next_task_id"] = new_task["id"]
            # Only the v1.2 journal is writable here. A v2 run lives in runtime.db;
            # re-appending it would write a duplicate/stale v2 snapshot into the
            # legacy runs.jsonl and let the two stores diverge (audit AR-3). The
            # new task still carries `parent_task_id`/`prior_run_id`, so the
            # forward linkage is preserved regardless of the run's store.
            if run.get("source") == "v1.2":
                agent_runner.append_run(run)
            activity_log.log_event(
                "next_task_created", project=project, task_id=new_task["id"], run_id=run["id"],
                message=f"Создана задача из запуска {run['id'][:8]}",
            )
            st.success(f"Задача создана: {new_task['title'][:60]}")
            st.rerun()
