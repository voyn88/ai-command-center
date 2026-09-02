from __future__ import annotations

import html
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

from command_center import (
    activity_log,
    agent_runner,
    artifacts,
    chat_service,
    dashboard_truth,
    execution_queue,
    executors,
    git_info,
    models,
    project_config,
    read_model,
    recommend,
    report_parser,
    storage,
    task_import,
    task_pipeline,
    task_view,
)
from command_center.runtime import api as runtime_api
from command_center.runtime import db as runtime_db
from command_center.runtime import runs_read, scheduler, session_view
from command_center.ui import (
    agent_launcher,
    alert_panel,
    aml_panel,
    case_panel,
    compliance_dashboard,
    customer_panel,
    rules_panel,
    sar_panel,
    backlog_proposals,
    execution_center_form,
    execution_center_monitor,
    execution_strip,
    git_readers,
    home_dashboard,
    integration_center,
    leaderboard_panel,
    legacy_task_helpers,
    live_board,
    master_backlog_panel,
    operator_dashboard,
    waves_panel,
    workspace_home_page,
    content_area,
    daily_audit_panel,
    portfolio_overview_panel,
    portfolio_panel,
    proposals_panel,
    project_selector,
    shell,
    sidebar,
    task_cards,
    task_dependencies,
    tokens,
)

ROOT = Path(__file__).resolve().parent
PROJECTS_DIR = ROOT / "projects"
GENERATED_DIR = ROOT / "generated"
REPORTS_DIR = ROOT / "reports"
CONTEXT_DIR = ROOT / "context"
DATA_DIR = storage.resolve_data_dir(ROOT)
TASKS_FILE = DATA_DIR / "tasks.json"
TASKS_EXAMPLE_FILE = DATA_DIR / "tasks.example.json"
START_TASK_SCRIPT = ROOT / "scripts" / "start-task.sh"

# The project registry is `models.PROJECT_IDS` — the single canonical list —
# never a second, hand-maintained dict here. A local `PROJECTS` dict used to
# live in this spot and silently omitted AICOS from every selector/filter
# that read it; see `docs/adr/` for the fix. `project_status_file_path`
# below is the only remaining project-id-keyed lookup this module needs,
# and it is backed by `project_config.PROJECT_STATUS_FILES` (itself keyed
# over every `models.PROJECT_IDS` entry), not a local dict.


def project_status_file_path(project_id: str) -> Path:
    relative = project_config.PROJECT_STATUS_FILES.get(project_id, f"projects/{project_id}.md")
    return ROOT / relative

CONTEXT_FILES: dict[str, str] = {
    "AIOS": "AIOS_CONTEXT.md",
    "BANK": "BANK_CONTEXT.md",
    "LEGAL": "LEGAL_CONTEXT.md",
}

# Canonical source: command_center.artifacts.TASK_TYPES — see that module's
# docstring for why app.py must not define its own duplicate list.
TASK_TYPES: tuple[str, ...] = artifacts.TASK_TYPES

# Moved to command_center/ui/agent_launcher.py (NIGHT-W9 slice 3) together
# with the launcher widgets that render it; re-exported for existing readers.
TASK_TYPE_LABELS: dict[str, str] = agent_launcher.TASK_TYPE_LABELS

AGENT_ROLES: dict[str, dict[str, object]] = {
    "implementation": {
        "title": "Инженер реализации",
        "summary": "Реализует поставленную цель под строгим контролем репозитория.",
        "rules": [
            "Изучить репозиторий перед изменением файлов.",
            "Реализовать только заявленную цель.",
            "Изменять только необходимые для задачи файлы.",
            "Добавить или обновить тесты для изменённого поведения.",
            "Не ослаблять существующие тесты.",
            "Не выполнять commit, push, merge, reset, stash, rebase.",
            "Запустить все применимые проверки.",
        ],
    },
    "review": {
        "title": "Независимый ревьюер",
        "summary": "Проводит read-only ревью без изменения файлов.",
        "rules": [
            "Не изменять ни один файл.",
            "Не выполнять commit, push, merge, reset, stash, rebase.",
            "Проверять фактическое состояние репозитория, а не прошлые заявления.",
            "Проверить поведение, тесты, контракты, безопасность и совместимость.",
            "Указывать находки с точными ссылками на файл и строку.",
            "Возвращать APPROVED только при отсутствии блокирующих проблем.",
        ],
    },
    "remediation": {
        "title": "Инженер по исправлениям",
        "summary": "Исправляет только независимо подтверждённые находки.",
        "rules": [
            "Исправлять только перечисленные находки.",
            "Не переделывать несвязанную архитектуру.",
            "Не изменять несвязанные файлы.",
            "Добавить регрессионные тесты для каждого исправления.",
            "Не ослаблять существующие тесты.",
            "Не выполнять commit, push, merge, reset, stash, rebase.",
            "Запустить все применимые проверки.",
        ],
    },
    "final_gate": {
        "title": "Финальный контролёр релиза",
        "summary": "Независимая финальная read-only проверка перед коммитом.",
        "rules": [
            "Не изменять ни один файл.",
            "Проверить полный diff и состояние рабочего дерева.",
            "Подтвердить, что все требуемые находки устранены.",
            "Подтвердить, что тесты реально покрывают исправленное поведение.",
            "Проверить упаковку, сгенерированные артефакты и документацию.",
            "Вернуть APPROVED FOR COMMIT или NOT APPROVED FOR COMMIT.",
        ],
    },
    "architecture_review": {
        "title": "Архитектурный ревьюер",
        "summary": "Независимый read-only обзор архитектуры.",
        "rules": [
            "Не изменять ни один файл.",
            "Оценить инварианты, владение, контракты, переходы состояний и отказоустойчивость.",
            "Проверить трассируемость между требованиями, архитектурой, рантаймом и тестами.",
            "Выявить неоднозначное или неавторитетное поведение.",
            "Указывать находки с уровнем серьёзности и точными ссылками.",
        ],
    },
}

# Canonical source: command_center.models.KANBAN_STATUSES / TASK_PRIORITIES —
# see that module's docstring for why app.py must not define its own
# duplicate lists (command_center.task_import validates against the same
# vocabulary and must never import app.py).
KANBAN_COLUMNS: list[str] = models.KANBAN_STATUSES
MANUAL_KANBAN_STATUSES: list[str] = [status for status in KANBAN_COLUMNS if status != "Done"]

PRIORITIES: list[str] = models.TASK_PRIORITIES

# Canonical source: command_center.ui.tokens — see that module's docstring
# for why app.py must not define its own duplicate color dicts.
PRIORITY_COLORS: dict[str, str] = tokens.PRIORITY_COLORS
LAUNCH_STATUS_COLORS: dict[str, str] = tokens.LAUNCH_STATUS_COLORS

GLOBAL_FILES: list[str] = ["CURRENT_STATE.md"]

IGNORED_FILE_NAMES = {".DS_Store", ".gitkeep"}

NAV: dict[str, tuple[str, str]] = {
    "dashboard": ("Обзор", ":material/dashboard:"),
    "command": ("Командный центр", ":material/space_dashboard:"),
    "workspace_home": ("Workspace Home", ":material/home_work:"),
    "executive": ("Исполнительная панель", ":material/insights:"),
    "compliance": ("Compliance Dashboard", ":material/security:"),
    "alerts": ("Алерты", ":material/notifications_active:"),
    "customers": ("Клиенты", ":material/people:"),
    "rules": ("Правила AML", ":material/gavel:"),
    "cases": ("Дела AML", ":material/folder_special:"),
    "sar": ("SAR", ":material/report:"),
    "aml": ("AML Monitoring", ":material/policy:"),
    "create": ("Создать задачу", ":material/add_task:"),
    "chat": ("Чат по проекту", ":material/forum:"),
    "kanban": ("Kanban", ":material/view_kanban:"),
    "master_backlog": ("Master Backlog", ":material/inventory:"),
    "task_deps": ("Зависимости задач", ":material/account_tree:"),
    "waves": ("Волны", ":material/waves:"),
    "agents": ("AI-агенты", ":material/smart_toy:"),
    "execution_center": ("Live Execution Center", ":material/bolt:"),
    "daily_audit": ("Ежедневный аудит", ":material/fact_check:"),
    "runs": ("Журнал запусков", ":material/history:"),
    "timeline": ("Таймлайн", ":material/timeline:"),
    "projects": ("Проекты", ":material/folder_open:"),
    "integration": ("Integration Center", ":material/lan:"),
    "generated": ("Сгенерированные задачи", ":material/description:"),
    "reports": ("Отчёты", ":material/summarize:"),
    "context": ("Глобальный контекст", ":material/menu_book:"),
    "git_center": ("Git Center", ":material/commit:"),
    "workspace": ("Workspace Launcher", ":material/rocket_launch:"),
    "focus": ("Focus Mode", ":material/center_focus_strong:"),
    "portfolio": ("Портфель", ":material/inventory_2:"),
    "portfolio_overview": ("Portfolio Overview", ":material/hub:"),
}


# --------------------------------------------------------------------------
# File and text helpers
# --------------------------------------------------------------------------


def read_text(path: Path) -> str:
    if not path.exists():
        return "Файл пока не создан."
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"Ошибка чтения файла: {exc}"
    return content if content.strip() else "Файл пока пуст."


# Moved to command_center/artifacts.py (NIGHT-W9 slice 5); re-exported.
format_mtime = artifacts.format_mtime


# Moved to command_center/ui/task_cards.py (NIGHT-W9 slice 4); re-exported.
format_estimate = task_cards.format_estimate


# list_markdown_files / project_from_path / infer_task_type_from_filename now live in
# command_center/artifacts.py (Streamlit-free — see WORKSPACE_HOME_ARCHITECTURE.md
# §9/§9.1/§9.2). Imported at module top as `artifacts`; call sites below use
# `artifacts.list_markdown_files(...)` etc.


def gather_activity(limit: int = 20) -> list[tuple[Path, float]]:
    files: list[Path] = []
    for directory in (GENERATED_DIR, REPORTS_DIR, PROJECTS_DIR, CONTEXT_DIR):
        if directory.exists():
            files.extend(
                path
                for path in directory.rglob("*")
                if path.is_file() and path.name not in IGNORED_FILE_NAMES
            )
    for name in GLOBAL_FILES:
        candidate = ROOT / name
        if candidate.exists():
            files.append(candidate)

    dated = [(path, path.stat().st_mtime) for path in files]
    dated.sort(key=lambda item: item[1], reverse=True)
    return dated[:limit]


def parse_project_statuses() -> dict[str, str]:
    """Best-effort extraction of 'Status: X' lines per project section in CURRENT_STATE.md."""
    content = read_text(ROOT / "CURRENT_STATE.md")
    statuses: dict[str, str] = {}
    current_project: str | None = None

    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            heading = stripped[3:].lower()
            current_project = next(
                (key for key in models.PROJECT_IDS if key.lower() in heading), None
            )
        elif stripped.lower().startswith("status:") and current_project and current_project not in statuses:
            statuses[current_project] = stripped.split(":", 1)[1].strip()

    return statuses


# --------------------------------------------------------------------------
# Task persistence (data/tasks.json)
# --------------------------------------------------------------------------


# Task persistence wrappers moved to `command_center/ui/legacy_task_helpers.py`
# (NIGHT-W9-AICC-ARCH slice 1) — pure delegation to `tasks_repository`/`models`,
# no second engine. Re-exported here so every existing call site (and every
# `app.<name>` test reference) keeps working unchanged. The single writer of
# `data/tasks.json` remains `tasks_repository.py` (docs/AUTHORITY_MAP.md);
# `tests/architecture/test_tasks_json_single_writer_fitness.py` enforces that
# this file never grows a direct write to that store again.

normalize_task = legacy_task_helpers.normalize_task
load_tasks = legacy_task_helpers.load_tasks
upsert_tasks = legacy_task_helpers.upsert_tasks
new_task_record = legacy_task_helpers.new_task_record
create_task = legacy_task_helpers.create_task
update_task_status = legacy_task_helpers.update_task_status
task_label = legacy_task_helpers.task_label
unmet_dependencies = legacy_task_helpers.unmet_dependencies
is_blocked = legacy_task_helpers.is_blocked


def delete_task(task_id: str) -> None:
    """Locked delete plus the runtime.db footprint cascade (session/run/event/
    report/completion) so a deleted Kanban card leaves no orphan rows in the
    unified Runs/Timeline/metrics views (audit AR-1). The cascade stays here
    because `get_execution_center_api` is this app's `st.cache_resource`
    singleton; `legacy_task_helpers.delete_task` takes it as a callback."""
    legacy_task_helpers.delete_task(
        task_id, on_deleted=lambda tid: get_execution_center_api().delete_task(tid)
    )


# --------------------------------------------------------------------------
# Task generation (scripts/start-task.sh)
# --------------------------------------------------------------------------


def run_start_task_script(
    project: str,
    task_type: str,
    objective: str,
    timeout: int = 30,
) -> tuple[bool, str, str]:
    if not START_TASK_SCRIPT.exists():
        return False, "", f"Скрипт не найден: {START_TASK_SCRIPT}"
    if not os.access(START_TASK_SCRIPT, os.X_OK):
        return False, "", f"Скрипт не является исполняемым: {START_TASK_SCRIPT}"

    try:
        result = subprocess.run(
            [str(START_TASK_SCRIPT), project, task_type, objective],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return False, "", f"Превышено время ожидания выполнения скрипта ({timeout} сек)."
    except OSError as exc:
        return False, "", f"Не удалось запустить скрипт: {exc}"

    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


# --------------------------------------------------------------------------
# Git (read-only) — moved to command_center/ui/git_readers.py (NIGHT-W9 slice 2)
# --------------------------------------------------------------------------

# Read-only status/log/diff/branch/remote/worktree wrappers over
# `command_center.git_info`, pinned to this repo's ROOT, now live in
# `command_center/ui/git_readers.py`. Re-exported here so every existing call
# site keeps working. The write side of git stays in
# `command_center/runtime/git_ops.py` — never mixed into the readers.

run_git_command = git_readers.run_git_command
get_git_status = git_readers.get_git_status
get_git_log = git_readers.get_git_log
get_git_diff_stat = git_readers.get_git_diff_stat
get_git_branches = git_readers.get_git_branches
get_git_remotes = git_readers.get_git_remotes
get_git_worktrees = git_readers.get_git_worktrees


# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------


def _parse_iso_ts(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return None


def build_timeline_events(
    tasks: list[dict],
    runs: list[dict] | None = None,
    activity_events: list[dict] | None = None,
    limit: int = 200,
) -> list[dict]:
    events: list[dict] = []

    for task in tasks:
        created = task.get("created_at")
        created_ts = _parse_iso_ts(created)
        if created_ts is not None:
            events.append(
                {
                    "ts": created_ts,
                    "icon": ":material/add_task:",
                    "label": f"Задача создана: {(task.get('title') or '')[:80]}",
                    "project": task.get("project"),
                }
            )

        updated = task.get("updated_at")
        if updated and updated != created:
            updated_ts = _parse_iso_ts(updated)
            if updated_ts is not None:
                events.append(
                    {
                        "ts": updated_ts,
                        "icon": ":material/sync_alt:",
                        "label": f"Статус «{task.get('status')}»: {(task.get('title') or '')[:80]}",
                        "project": task.get("project"),
                    }
                )

    for run in runs or []:
        run_ts = _parse_iso_ts(run.get("created_at"))
        if run_ts is not None:
            verdict = (run.get("parsed") or {}).get("verdict")
            suffix = f" · {verdict}" if verdict else ""
            events.append(
                {
                    "ts": run_ts,
                    "icon": ":material/smart_toy:",
                    "label": f"Запуск {run.get('task_type')} ({models.RUN_STATUS_LABELS.get(run.get('status'), run.get('status'))}){suffix}",
                    "project": run.get("project"),
                }
            )

    for event in activity_events or []:
        event_ts = _parse_iso_ts(event.get("ts"))
        if event_ts is not None:
            events.append(
                {
                    "ts": event_ts,
                    "icon": ":material/bolt:",
                    "label": event.get("message") or event.get("type") or "",
                    "project": event.get("project"),
                }
            )

    for path, mtime in gather_activity(limit=limit):
        project = None
        for base in (GENERATED_DIR, REPORTS_DIR):
            if base in path.parents:
                candidate = artifacts.project_from_path(path, base)
                project = candidate if candidate != "—" else None
                break
        try:
            rel = path.relative_to(ROOT)
        except ValueError:
            rel = path
        events.append(
            {
                "ts": mtime,
                "icon": ":material/description:",
                "label": f"Файл: {rel}",
                "project": project,
            }
        )

    events.sort(key=lambda event: event["ts"], reverse=True)
    return events[:limit]


# --------------------------------------------------------------------------
# Agent workflow (v1.2): launcher, next-task suggestion, project chat helpers
# --------------------------------------------------------------------------


def _build_project_context_text(project: str) -> str:
    cfg = project_config.get_project_config(project)
    parts: list[str] = []
    for rel_path in cfg.get("context_file_paths", []):
        path = ROOT / rel_path
        if path.exists():
            parts.append(f"### {rel_path}\n\n{read_text(path)}")
    return "\n\n".join(parts)


def _save_message_as_report(conversation: dict, message: dict) -> Path:
    project = conversation["project"]
    report_dir = REPORTS_DIR / project
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}_chat_{conversation['id'][:8]}.md"
    path = report_dir / filename
    content = (
        "# Сообщение чата, сохранённое как отчёт\n\n"
        f"Project: {project}\n"
        f"Conversation: {conversation['id']}\n"
        f"Role: {message['role']}\n"
        f"Provider: {message.get('provider') or '—'}\n"
        f"Timestamp: {message.get('created_at')}\n\n"
        "---\n\n"
        f"{message['content']}\n"
    )
    path.write_text(content, encoding="utf-8")
    activity_log.log_event(
        "report_saved", project=project, conversation_id=conversation["id"], message=filename
    )
    return path


# Agent launcher widgets moved to `command_center/ui/agent_launcher.py`
# (NIGHT-W9-AICC-ARCH slice 3) — pure move, no behavior change. Re-exported
# here so every existing call site keeps working. The `ExecutionCenterAPI`
# singleton accessor stays below in this file (`get_execution_center_api`,
# `st.cache_resource` — one Supervisor per server process) and is handed to
# the module via `agent_launcher.configure(...)` right after its definition.

_preselected_executor_id = agent_launcher._preselected_executor_id
_claude_cli_preflight = agent_launcher._claude_cli_preflight
render_agent_launcher = agent_launcher.render_agent_launcher
render_create_next_task_widget = agent_launcher.render_create_next_task_widget


# --------------------------------------------------------------------------
# Task Card — shared component (Title/Progress/Stage/Project/Executor/
# Repository/Workspace/Branch/Git/PR/Tests + action row), used by kanban
# and focus mode so every task-summary view stays visually/behaviorally
# consistent instead of each page duplicating its own inline markup.
# --------------------------------------------------------------------------


# Pure task-card read-model logic lives in `command_center.task_view` — see
# `docs/adr/0001-engineering-control-center-v2-increment-1.md`. These
# render_* functions only turn its plain-data output into widgets.


# Task Card widgets moved to `command_center/ui/task_cards.py`
# (NIGHT-W9-AICC-ARCH slice 4) — pure move, no behavior change. Re-exported
# here so every existing call site keeps working. The ExecutionCenterAPI
# singleton and the delete cascade are reached through the same seams as
# slice 3 (`agent_launcher.configure(...)` below / `legacy_task_helpers`).

_set_launch_status = task_cards._set_launch_status
_render_manual_merge_button = task_cards._render_manual_merge_button
render_task_timeline = task_cards.render_task_timeline
render_dependency_graph = task_cards.render_dependency_graph
render_task_card = task_cards.render_task_card
render_next_task_callout = task_cards.render_next_task_callout


# --------------------------------------------------------------------------
# Live Execution Center (v2 Session Supervisor UI — Sprint 2 Increment 1)
#
# Thin consumer of the frozen Sprint 1 runtime (`command_center.runtime`):
# every launch/status/event/cancel operation below goes through
# `runtime_api.ExecutionCenterAPI`, never touching `Supervisor` internals,
# raw SQL, or OS signals directly. See `command_center/runtime/api.py` and
# `command_center/runtime/supervisor.py` for what those calls actually do.
# --------------------------------------------------------------------------

# Moved to command_center/ui/execution_center_form.py (NIGHT-W9 slice 5);
# re-exported for existing readers (incl. workspace-home run badges below).
EXECUTION_CENTER_STATE_LABELS: dict[str, str] = execution_center_form.EXECUTION_CENTER_STATE_LABELS

# Non-terminal states — a run in one of these is still worth polling. The set
# itself lives in `runtime_db` (beside `TERMINAL_STATES`) so both `app.py` and
# Streamlit-free `command_center` modules (e.g. `workspace_home.py`) share the
# same source of truth.
EXECUTION_CENTER_ACTIVE_STATES: frozenset[str] = runtime_db.EXECUTION_CENTER_ACTIVE_STATES


@st.cache_resource
def get_execution_center_api() -> runtime_api.ExecutionCenterAPI:
    """One `ExecutionCenterAPI` (and the `Supervisor` it owns) per Streamlit
    server process, reused across every script rerun.

    A fresh `Supervisor` on every rerun would lose `Supervisor._active` — the
    in-memory registry of subprocess handles a *running* Supervisor instance
    needs to stream stdout/stderr and to signal a cancellation (see
    `supervisor.py`'s module docstring). Persisted run truth (status,
    timestamps, events) always still comes from `ExecutionCenterAPI`'s own
    reads of the runtime database, never from Streamlit session state — the
    singleton only needs to survive so cancellation keeps working, not to
    cache any data itself.

    Calls `.reconcile()` exactly once here, right after construction —
    `st.cache_resource` guarantees this runs once per server process, which
    is exactly "on app restart" for a restarted Streamlit process. This is
    the only place startup reconciliation is triggered; it reuses
    `Supervisor.reconcile()` unchanged (see `runtime/supervisor.py`) rather
    than adding any new engine or duplicating its logic.
    """
    api = runtime_api.ExecutionCenterAPI()
    api.reconcile()
    # Opt-in background sync (audit MAJOR-8): keep the run->task projection
    # current on a host that runs unattended (no Live Execution Center tab open
    # to drive the tick). Off by default — the interactive app relies on a tab's
    # refresh. Started once per server process (this function is
    # `@st.cache_resource`) and shares `tick`'s host-wide `pipeline_lock`, so it
    # never races the page tick.
    if os.environ.get("AICC_BACKGROUND_SYNC"):
        task_pipeline.start_background_sync(ROOT, api, project_config.load_project_configs)
    return api


# Hand the singleton accessor to the extracted launcher module (slice 3 seam).
agent_launcher.configure(execution_center_api_factory=get_execution_center_api)


# Moved to command_center/ui/execution_center_form.py (NIGHT-W9 slice 5).
render_execution_center_launch_form = execution_center_form.render_execution_center_launch_form


# Live Execution Center monitor moved to
# `command_center/ui/execution_center_monitor.py` (NIGHT-W9-AICC-ARCH slice 5)
# — pure move, no behavior change. The `st.cache_resource` ExecutionCenterAPI
# singleton stays above in this file; the monitor reaches it through
# `agent_launcher.configure(...)`'s injected accessor. Names still referenced
# in this file (and the public page renderer) are re-exported by assignment.

_LAUNCH_FLASH_KEY = execution_center_monitor._LAUNCH_FLASH_KEY
_IMPORT_TASK_FLASH_KEY = execution_center_monitor._IMPORT_TASK_FLASH_KEY
_PROJECT_SETTINGS_FLASH_KEY = execution_center_monitor._PROJECT_SETTINGS_FLASH_KEY
_execution_center_display_status = execution_center_monitor._execution_center_display_status
_build_execution_center_sessions = execution_center_monitor._build_execution_center_sessions
_open_task_detail = execution_center_monitor._open_task_detail
_maybe_open_task_detail = execution_center_monitor._maybe_open_task_detail
render_live_execution_center = execution_center_monitor.render_live_execution_center

# --------------------------------------------------------------------------
# Workspace Home — thin renderer over command_center.workspace_home's snapshot.
# No business logic beyond st.* calls; every field shown for BANK/LEGAL is
# already redacted by build_workspace_home_snapshot before it reaches here
# (see WORKSPACE_HOME_ARCHITECTURE.md §5.1/§13 — this renderer is not the
# security boundary and never receives the data that would need redacting).
# --------------------------------------------------------------------------

# Workspace Home page moved to `command_center/ui/workspace_home_page.py`
# (NIGHT-W9-AICC-ARCH slice 6) — pure move, no behavior change; page
# renderers re-exported by assignment.

render_project_planning_intelligence = workspace_home_page.render_project_planning_intelligence
render_workspace_home_page = workspace_home_page.render_workspace_home_page


# --------------------------------------------------------------------------
# Page setup
# --------------------------------------------------------------------------

# Widgets cannot have their session_state key overwritten after they have
# been instantiated in the current run. Cross-page navigation (command
# palette, agent shortcuts, workspace launcher) therefore stages its target
# values under "pending_*" keys and this block applies them before any
# matching widget is created.
_PENDING_KEY_MAP = {
    "pending_nav": "nav_page",
    "pending_create_project": "create_task_project",
    "pending_create_type": "create_task_type",
    "pending_project_browser": "project_browser_select",
    "pending_chat_conv": "chat_conv_select",
    "pending_exec_center_run": "exec_center_highlight_run",
    "pending_exec_center_project": "exec_center_launch_project",
}
for _pending_key, _target_key in _PENDING_KEY_MAP.items():
    if _pending_key in st.session_state:
        st.session_state[_target_key] = st.session_state.pop(_pending_key)

if "show_command_palette" not in st.session_state:
    st.session_state.show_command_palette = False


def _open_command_palette() -> None:
    st.session_state.show_command_palette = True


def build_commands() -> list[dict]:
    commands = [
        {"label": f"Перейти: {label}", "icon": icon, "action": ("nav", key)}
        for key, (label, icon) in NAV.items()
        if key not in sidebar.HIDDEN_FROM_SIDEBAR
    ]
    commands.extend(
        {"label": f"Новая задача: {project}", "icon": ":material/add_task:", "action": ("new_task", project)}
        for project in models.PROJECT_IDS
    )
    return commands


# Data loading happens before the shell render so the top command bar (search,
# live glyph, Inspector) has the task map + api available without a second pass.
tasks = load_tasks()
tasks_by_id = {task["id"]: task for task in tasks}
task_counts = read_model.task_snapshot(tasks)
project_configs = project_config.load_project_configs()

page_key = shell.render_shell(
    page_title="AI Command Center",
    page_icon="🧭",
    sidebar_collapsed=st.session_state.get("nav_page") == "focus",
    title="🧭 AI Command Center",
    caption="Единый центр управления проектами, задачами и AI-процессами",
    nav=NAV,
    project_count=len(models.PROJECT_IDS),
    on_open_palette=_open_command_palette,
    tasks_by_id=tasks_by_id,
    api=get_execution_center_api(),
)


def _dismiss_command_palette() -> None:
    st.session_state.show_command_palette = False


@st.dialog(
    "Командная палитра",
    width="large",
    on_dismiss=_dismiss_command_palette,
)
def _command_palette_dialog() -> None:
    query = st.text_input(
        "Поиск",
        key="palette_query",
        placeholder="Введите название страницы или действие...",
        label_visibility="collapsed",
    )
    commands = build_commands()
    query_clean = query.strip().lower()
    matches = (
        [command for command in commands if query_clean in command["label"].lower()]
        if query_clean
        else commands
    )

    if not matches:
        st.caption("Ничего не найдено.")

    for index, command in enumerate(matches[:20]):
        if st.button(
            command["label"],
            key=f"palette_cmd_{index}",
            icon=command["icon"],
            width="stretch",
        ):
            kind, value = command["action"]
            if kind == "nav":
                st.session_state.pending_nav = value
            elif kind == "new_task":
                st.session_state.pending_nav = "create"
                st.session_state.pending_create_project = value
            st.session_state.show_command_palette = False
            st.rerun()


if st.session_state.show_command_palette:
    _command_palette_dialog()


# Execution Strip (UX-2a): cross-page live status bar. A polling fragment, so
# it updates every 5 s on its own without blanking the page behind it. Rendered
# before the page dispatch so it is visible on every page (pages that call
# `st.stop()` have already mounted it by then).
execution_strip.render_execution_strip(get_execution_center_api(), ROOT)


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

def _home_greeting() -> str:
    hour = datetime.now().hour
    part = "Доброе утро" if 5 <= hour < 12 else "Добрый день" if 12 <= hour < 18 else "Добрый вечер"
    return f"{part} 👋"


def _runs_per_day(runs: list[dict], days: int = 7) -> tuple[int, ...]:
    """A real short series: runs started per day over the last `days` — the
    honest trend for a KPI sparkline (never random)."""
    today = datetime.now().date()
    buckets = [0] * days
    for r in runs:
        started = r.get("started_at")
        if not started:
            continue
        try:
            d = datetime.fromisoformat(started).date()
        except (ValueError, TypeError):
            continue
        delta = (today - d).days
        if 0 <= delta < days:
            buckets[days - 1 - delta] += 1
    return tuple(buckets)


def _run_started_date(run: dict) -> datetime | None:
    """Parse a run's ``started_at`` ISO timestamp to a date, or ``None``."""
    started = run.get("started_at")
    if not started:
        return None
    try:
        return datetime.fromisoformat(started)
    except (ValueError, TypeError):
        return None


# Windowed (sprint) run health — the honest denominator for the dashboard's
# "Здоровье проекта" gauge. The cumulative `len(runs)` denominator used before
# mixed a 200-run cap with all-time history and produced a number that drifted
# away from "how are we doing *lately*". A 7-day window tracks recent execution
# quality and is always well inside the Live Board's `limit=200` fetch.
HEALTH_WINDOW_DAYS = 7


def _window_terminal_runs(runs: list[dict], *, days: int = HEALTH_WINDOW_DAYS) -> list[dict]:
    today = datetime.now().date()
    out = []
    for r in runs:
        if r.get("state") not in runtime_db.TERMINAL_STATES:
            continue
        started = r.get("started_at")
        if not started:
            continue
        try:
            d = datetime.fromisoformat(started).date()
        except (ValueError, TypeError):
            continue
        if 0 <= (today - d).days < days:
            out.append(r)
    return out


def _window_success_rate(runs: list[dict], *, days: int = HEALTH_WINDOW_DAYS) -> int | None:
    """Success rate over terminal runs started in the last `days`. Returns
    `None` when there are no windowed terminal runs — the caller renders an
    explicit "Нет данных" empty state instead of a misleading 0%."""
    window = _window_terminal_runs(runs, days=days)
    if not window:
        return None
    completed = sum(1 for r in window if r.get("state") == "COMPLETED")
    return int(round(100 * completed / len(window)))


# Human-readable labels for the v1.2 activity log event types — the dashboard's
# "Последняя активность" card renders real lifecycle events (run_started,
# report_saved, …) instead of bare file mtimes from `gather_activity`, which
# exposed internal path names rather than anything the user did.
_ACTIVITY_LABELS: dict[str, str] = {
    "run_started": "Запущен агент",
    "run_completed": "Прогон завершён",
    "run_failed": "Прогон завершён с ошибкой",
    "run_queued": "Задача в очереди",
    "report_saved": "Сохранён отчёт",
    "task_created_from_message": "Создана задача",
    "task_moved_to_remediation": "Задача → remediation",
    "next_task_created": "Создана следующая задача",
    "manual_field_correction": "Ручная правка",
    "conversation_created": "Новый разговор",
    "message_added": "Новое сообщение",
    "verdict_extracted": "Извлечён вердикт",
}


def render_home_dashboard(
    api: runtime_api.ExecutionCenterAPI,
    tasks: list[dict],
    counts: read_model.TaskSnapshot,
) -> None:
    """The Home dashboard from the approved design — KPI tiles, execution queue,
    project health, recent activity, a Kanban overview and quick actions, with an
    AI-Supervisor side panel. Pure presentation over the live task/run state via
    `command_center.ui.home_dashboard`; every number is real."""
    home_dashboard.inject_css()
    now = datetime.now()
    # Operator name is configurable via the AICC_OPERATOR env var — never a
    # hardcoded person. Unset → a neutral greeting with no name, so a fresh
    # install does not greet "Artyom".
    owner = os.environ.get("AICC_OPERATOR", "").strip()

    runs = api.list_runs(limit=200)
    sessions, tasks_by_id = _build_execution_center_sessions(api, tasks, now=now)
    for s in sessions:
        s["display_status"] = _execution_center_display_status(s)
    stale_run_ids = frozenset(
        session["run_id"]
        for session in sessions
        if session["display_status"] == session_view.STATUS_STALE
    )
    truth = dashboard_truth.build_dashboard_truth(
        counts,
        runs=runs,
        total_run_count=api.count_runs(),
        stale_run_ids=stale_run_ids,
        run_window_limit=200,
    )
    board = live_board.split_board(sessions, display_status="display_status")

    running = board[live_board.BUCKET_LIVE]
    projects_with_tasks = {
        project_config.canonical_project_id(t.get("project")) or t.get("project")
        for t in tasks
        if t.get("project")
    }

    greeting = f"{_home_greeting()} {owner}" if owner else _home_greeting()
    st.markdown(
        f"<h2 class='hx-page-title'>{html.escape(greeting)}</h2>",
        unsafe_allow_html=True,
    )
    st.caption("Вот что происходит с вашими проектами сегодня.")

    # Next-action hero (UX-2b): the one thing an operator opens the dashboard
    # for — "what should I do next" — promoted above the KPI row. The callout is
    # advisory (never creates/launches); a deep-link button jumps to the task.
    recommendation = recommend.recommend_next_task(
        tasks, active_runs=[r for r in runs if r.get("state") in runtime_db.EXECUTION_CENTER_ACTIVE_STATES]
    )
    if recommendation is not None:
        hero_task = recommendation.task
        with st.container(border=True):
            st.markdown(f"##### ➡ Следующая задача: {hero_task.get('title') or 'Без названия'}")
            st.caption(
                f"{hero_task.get('project')} · {hero_task.get('status')} · "
                f"приоритет {hero_task.get('priority', 'Medium')}"
            )
            st.caption("Почему: " + "; ".join(recommendation.reasons))
            hero_cols = st.columns([1, 1, 1, 1])
            with hero_cols[0]:
                if st.button("Открыть задачу", key="home_hero_open_task", icon=":material/task_alt:", width="stretch"):
                    st.session_state.pending_nav = "kanban"
                    st.rerun()
            with hero_cols[1]:
                if st.button("Запустить", key="home_hero_launch", icon=":material/play_arrow:", type="primary", width="stretch"):
                    st.session_state.pending_nav = "execution_center"
                    st.session_state.pending_exec_center_project = hero_task.get("project")
                    st.rerun()
    else:
        st.info("➡ Нет открытых незаблокированных задач — создайте новую.")

    # Real 24h run delta (UX-2b): runs started today vs yesterday, so the
    # "Агенты" KPI carries an honest day-over-day trend instead of a static
    # count. Both windows read from the already-loaded `runs` (limit=200).
    today = now.date()
    runs_today = sum(1 for r in runs if _run_started_date(r) and _run_started_date(r).date() == today)
    runs_yesterday = sum(
        1 for r in runs
        if _run_started_date(r) and _run_started_date(r).date().toordinal() == today.toordinal() - 1
    )
    if runs_yesterday:
        delta = runs_today - runs_yesterday
        runs_delta_txt = f"в окне {len(runs)}/200: сегодня {runs_today} ({'+' if delta >= 0 else ''}{delta} к вчера)"
    else:
        runs_delta_txt = f"в окне {len(runs)}/200: сегодня {runs_today}"

    # KPI sparklines removed: the four KPIs (Проекты/Агенты/Задачи/Ревью) measure
    # different things, but the old code fed the *same* `_runs_per_day` series to
    # all four — an identical trend under every tile that falsely implied each
    # metric had its own history. None of these KPIs has a genuine per-day series
    # derivable from the loaded runs, so the honest choice is no sparkline rather
    # than a duplicated, misleading one.
    home_dashboard.kpi_tiles([
        home_dashboard.Kpi("Проекты", len(projects_with_tasks),
                           (
                               f"TaskSnapshot: {counts.attention} задач требуют внимания"
                               if counts.attention
                               else "TaskSnapshot: нет задач, требующих внимания"
                           ),
                           "📁", "violet", ()),
        home_dashboard.Kpi("Живые прогоны", len(running), runs_delta_txt, "🤖", "blue", ()),
        home_dashboard.Kpi(
            "Задачи",
            counts.total,
            f"TaskSnapshot: {counts.active} активных · {counts.done} завершено",
            "✓",
            "green",
            (),
        ),
        home_dashboard.Kpi(
            "Ревью",
            counts.by_lane["Review"],
            f"TaskSnapshot: {counts.by_lane['Review']} в колонке Review",
            "★",
            "amber",
            (),
        ),
    ])

    # Clickable KPI deep-links (UX-2b): the inert HTML tiles above cannot host
    # Streamlit click handlers, so a matching row of buttons gives every KPI a
    # real destination. Each navigates via the existing `pending_*` mechanism.
    kpi_btns = st.columns(4)
    _kpi_targets = [
        ("📁 Проекты", "projects", "home_kpi_projects"),
        ("🤖 Execution Center", "execution_center", "home_kpi_agents"),
        ("✓ Kanban", "kanban", "home_kpi_tasks"),
        ("★ Ревью (Kanban)", "kanban", "home_kpi_review"),
    ]
    for i, (label, nav, key) in enumerate(_kpi_targets):
        with kpi_btns[i]:
            if st.button(label, key=key, width="stretch", icon=":material/arrow_forward:"):
                st.session_state.pending_nav = nav
                st.rerun()

    main, side = st.columns([3, 1.2], gap="large")

    with main:
        col_q, col_h = st.columns(2, gap="medium")
        with col_q:
            home_dashboard.card_open("Очередь выполнения", "Все")
            rows = []
            for s in (running + board[live_board.BUCKET_WAITING])[:5]:
                st_disp = s["display_status"]
                acc = {"Running": "green", "Starting": "green", "Waiting": "amber",
                       "Completed": "blue"}.get(st_disp, "indigo")
                rows.append({
                    "icon": "⚙", "name": s.get("task_title") or "—", "meta": s.get("project_id") or "—",
                    "pct": s.get("live_progress"), "accent": acc,
                    "status": st_disp, "status_accent": acc,
                })
            if rows:
                home_dashboard.queue_rows(rows)
                # Clickable queue rows (UX-2b): a button per running/waiting
                # session deep-links to the Live Execution Center with that run
                # highlighted via the existing `pending_exec_center_run`.
                qbtns = st.columns(min(len(rows), 5)) if rows else None
                for i, s in enumerate((running + board[live_board.BUCKET_WAITING])[:5]):
                    if qbtns is not None:
                        with qbtns[i % len(qbtns)]:
                            if st.button(
                                f"→ {(s.get('task_title') or '—')[:14]}",
                                key=f"home_queue_open_{s['run_id']}",
                                width="stretch",
                                help="Открыть прогон в Execution Center",
                            ):
                                st.session_state.pending_nav = "execution_center"
                                st.session_state.pending_exec_center_run = s["run_id"]
                                st.rerun()
            else:
                st.caption("Сейчас ничего не выполняется — запустите агента из Execution Center.")
            home_dashboard.queue_footer(
                truth.run_metric.value,
                len(running),
                len(board[live_board.BUCKET_DONE]),
                len(board[live_board.BUCKET_ATTENTION]),
                loaded=len(runs),
                window_limit=200,
            )
            home_dashboard.card_close()
            if st.button("Открыть Execution Center", key="home_open_exec", type="primary", width="stretch"):
                st.session_state.pending_nav = "execution_center"
                st.rerun()

        with col_h:
            home_dashboard.card_open("Здоровье проекта", "Детали")
            # Windowed health: success rate over terminal runs started in the
            # last 7 days (sprint window), not a cumulative blend over a
            # 200-capped run list. The old `len(running)*20` / `len(attention)*10`
            # multipliers were magic numbers dressing up counts as percentages.
            window_success = _window_success_rate(runs)
            task_ratio = int(100 * counts.done / counts.total) if counts.total else 0
            if window_success is None:
                home_dashboard.health_gauge(0, "7д, окно ≤200: нет данных", accent="slate")
            else:
                grade = "Отлично" if window_success >= 85 else "Хорошо" if window_success >= 60 else "Требует внимания"
                accent = "green" if window_success >= 85 else "blue" if window_success >= 60 else "amber"
                home_dashboard.health_gauge(window_success, f"7д, окно ≤200: {grade}", accent=accent)
            home_dashboard.metric_list([
                ("Задачи завершены", task_ratio, "green"),
                ("Прогоны успешны (7д, окно ≤200)", window_success if window_success is not None else 0, "blue"),
            ])
            home_dashboard.card_close()

        col_a, col_k = st.columns(2, gap="medium")
        with col_a:
            home_dashboard.card_open("Последняя активность")
            # Real lifecycle events from the append-only activity log
            # (run_started / report_saved / manual_field_correction …) instead
            # of `gather_activity` file mtimes, which surfaced internal path
            # names rather than anything the user or agents actually did.
            act_rows = []
            for event in activity_log.load_activity(limit=6):
                label = _ACTIVITY_LABELS.get(event.get("type", ""), event.get("type", "Событие"))
                ts = event.get("ts") or ""
                when = ""
                if ts:
                    try:
                        when = datetime.fromisoformat(ts).strftime("%d.%m %H:%M")
                    except (ValueError, TypeError):
                        when = ts[:16]
                meta = " · ".join(p for p in (event.get("project"), when) if p)
                act_rows.append({"icon": "•", "name": label, "meta": meta or "—"})
            if act_rows:
                home_dashboard.simple_rows(act_rows)
            else:
                st.caption("Активности пока нет.")
            home_dashboard.card_close()

        with col_k:
            home_dashboard.card_open("Обзор Kanban · TaskSnapshot", "Доска")
            accents = ["slate", "blue", "green", "amber", "red", "violet"]
            overview_lanes = list(read_model.CANONICAL_LANES)
            cols = [
                (lane, counts.by_lane[lane], accents[i % len(accents)])
                for i, lane in enumerate(overview_lanes)
            ]
            if counts.other:
                cols.append(("Другие", counts.other, "amber"))
            home_dashboard.kanban_overview(cols)
            home_dashboard.card_close()
            # Clickable Kanban columns (UX-2b): each lane count deep-links to the
            # Kanban board (the page-level filter defaults to "Все", so the
            # operator lands on the full board and can filter further).
            kbtns = st.columns(len(overview_lanes))
            for i, lane in enumerate(overview_lanes):
                with kbtns[i]:
                    if st.button(
                        f"→ {lane}",
                        key=f"home_kanban_{lane}",
                        width="stretch",
                        help="Открыть Kanban",
                    ):
                        st.session_state.pending_nav = "kanban"
                        st.rerun()

        home_dashboard.card_open("Быстрые действия")
        qa = st.columns(5)
        actions = [
            ("Быстро: новая задача", "create"), ("Быстро: запустить агента", "execution_center"),
            ("Быстро: Workspace", "workspace_home"), ("Быстро: Git Center", "git_center"),
            ("Быстро: отчёты", "reports"),
        ]
        for i, (label, nav) in enumerate(actions):
            with qa[i]:
                if st.button(label, key=f"home_qa_{nav}", width="stretch"):
                    st.session_state.pending_nav = nav
                    st.rerun()
        home_dashboard.card_close()

        home_dashboard.card_open("Доставка и runtime", "Canonical provenance")
        if truth.deliveries:
            home_dashboard.delivery_rows(
                [item.__dict__ for item in truth.deliveries[:6]],
                window_label=truth.run_window_label,
            )
        else:
            st.markdown(
                "<div role='status' aria-live='polite'>Доказательства запусков отсутствуют.</div>",
                unsafe_allow_html=True,
            )
            st.caption(truth.run_window_label)
        home_dashboard.card_close()

    with side:
        settings = task_pipeline.pipeline_settings.load_settings(ROOT)
        # Supervisor status reflects real run state, not a hardcoded 94%/"Active".
        # The gauge shows the same windowed health as the project-health card; the
        # status pill and caption describe what the supervisor is actually doing.
        window_success = _window_success_rate(runs)
        # audit D5: count "needs attention" with the exact same actionable,
        # dismiss-aware computation the Execution Strip banner and the Live Center
        # headline use (`execution_strip.current_counts` — drops superseded,
        # completed-task and operator-dismissed rows), so this caption, the strip
        # and the top-bar glyph never show two different "attention" numbers on
        # one screen. queue_entries=[] because attention/running don't depend on
        # the durable queue.
        run_counts = execution_strip.current_counts(
            runs, tasks, [],
            dismissed_attention_run_ids=st.session_state.get("exec_attention_dismissed", set()),
        )
        if run_counts.attention:
            sup_status, sup_accent = "Требует внимания", "amber"
            sup_label = f"Автопилот {'включён' if settings.enabled else 'выключен'} — {run_counts.attention} прогонов требуют внимания"
        elif run_counts.live:
            sup_status, sup_accent = "В работе", "green"
            sup_label = f"Автопилот {'включён' if settings.enabled else 'выключен'} — {run_counts.live} прогонов выполняется"
        else:
            sup_status, sup_accent = "Ожидает", "slate"
            sup_label = "Автопилот включён — ожидает задач" if settings.enabled else "Автопилот выключен"
        sup_percent = window_success if window_success is not None else 0
        home_dashboard.supervisor_status(
            sup_percent, sup_label, status=sup_status, accent=sup_accent,
        )
        home_dashboard.card_open("Проекты")
        proj_rows = []
        for p in sorted(projects_with_tasks):
            project_tasks = [
                t for t in tasks if project_config.project_matches(t.get("project"), p)
            ]
            project_counts = read_model.task_snapshot(project_tasks)
            p_active = [
                t for t in project_tasks if t.get("status") in read_model.ACTIVE_LANES
            ]
            p_att = [t for t in p_active if t.get("launch_status") in ("Failed", "Requires Attention", "Blocked")]
            proj_rows.append({
                "icon": "▪", "name": p, "meta": f"{project_counts.active} активных",
                "right": "Внимание" if p_att else "OK", "right_accent": "amber" if p_att else "green",
            })
        home_dashboard.simple_rows(proj_rows or [{"icon": "▪", "name": "Нет активных проектов", "meta": ""}])
        home_dashboard.card_close()
        # Clickable project rows (UX-2b): a button per project opens it in the
        # Projects view via the existing `pending_project_browser` mechanism.
        if projects_with_tasks:
            pbtns = st.columns(min(len(sorted(projects_with_tasks)), 3))
            for i, p in enumerate(sorted(projects_with_tasks)):
                with pbtns[i % len(pbtns)]:
                    if st.button(f"→ {p}", key=f"home_proj_{p}", width="stretch", help="Открыть проект"):
                        st.session_state.pending_nav = "projects"
                        st.session_state.pending_project_browser = p
                        st.rerun()

        home_dashboard.card_open("Активные агенты")
        agent_rows = []
        for s in running[:6]:
            agent_rows.append({
                "icon": "🤖", "name": s.get("executor") or "claude_code",
                "meta": (s.get("task_title") or "")[:36],
                "right": "Running", "right_accent": "green",
            })
        home_dashboard.simple_rows(agent_rows or [{"icon": "🤖", "name": "Нет активных прогонов", "meta": ""}])
        home_dashboard.card_close()

    st.divider()
    proposals_panel.render_proposals_inbox(api, key_prefix="home_proposals")


def render_project_chat(project: str, tasks: list[dict], tasks_by_id: dict[str, dict]) -> None:
    """Project-scoped chat: conversations, provider send, save-to-report,
    convert-a-message-to-a-task, and launch-Claude-from-the-conversation.

    The project is passed in (not selected here), so this renders both as the
    'Чат' tab inside the project view and from the standalone chat page handler —
    the page is kept so an existing deep link still works, it just delegates
    here (AICC task 02661825: everything about a project lives inside it)."""
    conversations = chat_service.load_conversations()
    project_conversations = [c for c in conversations if c.get("project") == project]
    chat_cfg = project_configs[project]

    if chat_cfg["sensitive"]:
        st.warning(
            f"{project} — чувствительный проект (BANK/LEGAL). Файлы не прикрепляются "
            "автоматически — добавляйте разрешённый контекст вручную."
        )

    conv_options = ["+ Новый разговор"] + [c["id"] for c in project_conversations]
    conv_labels = {c["id"]: f"{c.get('title', '—')} · {c.get('updated_at', '—')}" for c in project_conversations}
    chosen_conv_id = st.selectbox(
        "Разговор",
        conv_options,
        format_func=lambda value: "Новый разговор" if value == "+ Новый разговор" else conv_labels.get(value, value),
        key=f"chat_conv_select_{project}",
    )

    if chosen_conv_id == "+ Новый разговор":
        new_conv_title = st.text_input(
            "Название нового разговора", key=f"chat_new_title_{project}", placeholder="Например: обсуждение архитектуры P1"
        )
        project_task_options = ["Без привязки"] + [
            task["id"] for task in tasks if project_config.project_matches(task.get("project"), project)
        ]
        link_task_id = st.selectbox(
            "Привязать к задаче (необязательно)",
            project_task_options,
            format_func=lambda value: "Без привязки" if value == "Без привязки" else task_label(tasks_by_id[value]),
            key=f"chat_link_task_{project}",
        )
        if st.button("Создать разговор", key=f"chat_create_conv_btn_{project}", icon=":material/add_comment:"):
            new_conv = models.new_conversation(
                project,
                new_conv_title.strip() or "Новый разговор",
                task_id=None if link_task_id == "Без привязки" else link_task_id,
            )
            conversations.append(new_conv)
            chat_service.save_conversations(conversations)
            activity_log.log_event(
                "conversation_created", project=project, task_id=new_conv.get("task_id"),
                conversation_id=new_conv["id"], message=new_conv["title"],
            )
            st.session_state.pending_chat_conv = new_conv["id"]
            st.rerun()
    else:
        active_conversation = chat_service.get_conversation(conversations, chosen_conv_id)
        if active_conversation is None:
            st.error("Разговор не найден.")
            return
        linked_task = tasks_by_id.get(active_conversation.get("task_id") or "")
        caption = f"Проект: {active_conversation['project']} · создан {active_conversation['created_at']}"
        if linked_task:
            caption += f" · задача: {task_label(linked_task)}"
        st.caption(caption)

        include_context = st.checkbox(
            "Включить контекст проекта в запрос провайдеру", value=True, key=f"chat_include_ctx_{active_conversation['id']}"
        )

        for message in active_conversation.get("messages", []):
            with st.chat_message("user" if message["role"] == "user" else "assistant"):
                role_label = "Вы" if message["role"] == "user" else "Ассистент"
                provider_suffix = f" · {message['provider']}" if message.get("provider") else ""
                st.caption(f"{role_label} · {message.get('created_at', '—')}{provider_suffix}")
                st.write(message["content"])
                msg_action_cols = st.columns(2)
                with msg_action_cols[0]:
                    if st.button("Сохранить в отчёты", key=f"chat_save_report_{message['id']}", icon=":material/save:"):
                        saved_path = _save_message_as_report(active_conversation, message)
                        st.success(f"Сохранено: `{saved_path.relative_to(ROOT)}`")
                with msg_action_cols[1]:
                    if st.button("Сделать задачей", key=f"chat_to_task_{message['id']}", icon=":material/add_task:"):
                        st.session_state[f"chat_convert_open_{message['id']}"] = True

                if st.session_state.get(f"chat_convert_open_{message['id']}"):
                    with st.form(f"chat_convert_form_{message['id']}"):
                        conv_task_type = st.selectbox(
                            "Тип задачи", TASK_TYPES, format_func=lambda v: TASK_TYPE_LABELS.get(v, v),
                            key=f"chat_convert_type_{message['id']}",
                        )
                        conv_objective = st.text_area(
                            "Цель задачи", value=message["content"], key=f"chat_convert_obj_{message['id']}"
                        )
                        if st.form_submit_button("Создать задачу"):
                            objective_clean = conv_objective.strip()
                            if not objective_clean:
                                st.error("Укажите цель задачи.")
                            else:
                                new_task_from_msg = create_task(
                                    active_conversation["project"],
                                    models.derive_short_title(objective_clean),
                                    conv_task_type,
                                    "Backlog",
                                    goal=objective_clean,
                                    # An assistant message is untrusted agent output;
                                    # converting it to a task must not launder that into
                                    # a trusted run (SEC-D-02). A user's own message stays
                                    # trusted.
                                    untrusted_import=(message["role"] == "assistant"),
                                )
                                activity_log.log_event(
                                    "task_created_from_message", project=active_conversation["project"],
                                    task_id=new_task_from_msg["id"], conversation_id=active_conversation["id"],
                                    message="Задача создана из сообщения чата",
                                )
                                st.session_state[f"chat_convert_open_{message['id']}"] = False
                                st.success("Задача создана.")
                                st.rerun()

        st.divider()
        chat_providers = chat_service.available_providers()
        provider_status = {provider.name: provider.is_available() for provider in chat_providers}
        chosen_provider_name = st.selectbox(
            "Провайдер",
            [provider.name for provider in chat_providers],
            format_func=lambda name: chat_service.get_provider(name).label,
            key=f"chat_provider_{active_conversation['id']}",
        )
        provider_available, provider_reason = provider_status[chosen_provider_name]
        if provider_reason:
            st.info(provider_reason)
        if chosen_provider_name == "openai":
            st.caption("Использование OpenAI API оплачивается отдельно от подписки ChatGPT.")

        user_input = st.chat_input("Введите сообщение...", key=f"chat_input_{active_conversation['id']}")
        if user_input:
            conversations = chat_service.load_conversations()
            user_message = models.new_message("user", user_input, provider=None)
            chat_service.append_message(conversations, active_conversation["id"], user_message)
            activity_log.log_event(
                "message_added", project=active_conversation["project"], conversation_id=active_conversation["id"],
                message="Сообщение пользователя добавлено",
            )

            if chosen_provider_name != "local" and provider_available:
                context_text = _build_project_context_text(project) if include_context else ""
                updated_conversation = chat_service.get_conversation(conversations, active_conversation["id"])
                try:
                    with st.spinner("Ожидание ответа провайдера..."):
                        response_text = chat_service.get_provider(chosen_provider_name).send(
                            messages=updated_conversation["messages"],
                            project_context=context_text,
                            project_id=project,
                            repository_path=chat_cfg.get("repository_path"),
                            timeout_seconds=180,
                        )
                    conversations = chat_service.load_conversations()
                    assistant_message = models.new_message("assistant", response_text, provider=chosen_provider_name)
                    chat_service.append_message(conversations, active_conversation["id"], assistant_message)
                    activity_log.log_event(
                        "message_added", project=active_conversation["project"], conversation_id=active_conversation["id"],
                        message=f"Ответ провайдера {chosen_provider_name} добавлен",
                    )
                except Exception as exc:  # noqa: BLE001 — surfaced to the user, never crashes the page
                    st.error(f"Ошибка провайдера: {exc}")
            st.rerun()

        st.divider()
        st.markdown("##### Запустить Claude Code из этого разговора")
        last_user_message = next(
            (m["content"] for m in reversed(active_conversation.get("messages", [])) if m["role"] == "user"), ""
        )
        render_agent_launcher(
            key_prefix=f"chat_launch_{active_conversation['id']}",
            project=project,
            default_prompt=last_user_message,
            tasks=tasks,
            task_id=active_conversation.get("task_id"),
        )


def _project_audit_prompt(project: str) -> str:
    """The read-only audit brief. It must end with a machine-parsable section so
    `backlog_proposals.parse_candidate_tasks` can turn the report into tasks."""
    return (
        f"Проведи READ-ONLY аудит проекта {project} по четырём осям: архитектура, "
        "соблюдение правил/конвенций, качество кода и тестов, UX. Ничего в коде НЕ меняй. "
        "В конце отчёта ОБЯЗАТЕЛЬНО выведи секцию с заголовком '## Предлагаемые задачи', "
        "где каждым пунктом списка дай одно улучшение в формате "
        "'- **Короткий заголовок** — что и зачем сделать'. От 5 до 15 пунктов, по приоритету."
    )


def _latest_audit_report_text(project: str) -> str | None:
    """Newest project report that looks like an audit, so the Audit tab can turn
    it into candidate backlog tasks. Falls back to the newest report of any kind."""
    files = artifacts.list_markdown_files(REPORTS_DIR / project)
    audit_files = [f for f in files if any(k in f.name.lower() for k in ("audit", "architecture", "аудит"))]
    chosen = audit_files or files
    if not chosen:
        return None
    latest = max(chosen, key=lambda path: path.stat().st_mtime)
    return read_text(latest)


def _roadmap_reformat_prompt(project: str, wishes: str) -> str:
    """Brief for a roadmap rebuild. Ends with the same machine-parsable section
    the audit uses, so its output flows through the same candidate pipeline."""
    wishes_clean = (wishes or "").strip() or "(без дополнительных пожеланий — опирайся на текущее состояние проекта)"
    return (
        f"Пересобери roadmap проекта {project} с учётом пожеланий пользователя:\n{wishes_clean}\n\n"
        "Проанализируй текущее состояние (задачи, вехи, волны) и предложи обновлённый план. "
        "НЕ предлагай работу, которая уже сделана или уже есть в задачах. "
        "В конце ОБЯЗАТЕЛЬНО выведи секцию с заголовком '## Предлагаемые задачи', где каждым "
        "пунктом дай одну задачу в формате '- **Короткий заголовок** — что и зачем сделать', "
        "в порядке приоритета."
    )


def _latest_roadmap_report_text(project: str) -> str | None:
    """Newest project report that looks like a roadmap rebuild."""
    files = artifacts.list_markdown_files(REPORTS_DIR / project)
    roadmap_files = [f for f in files if any(k in f.name.lower() for k in ("roadmap", "переформат", "план"))]
    if not roadmap_files:
        return None
    latest = max(roadmap_files, key=lambda path: path.stat().st_mtime)
    return read_text(latest)


def render_dashboard_analytics(
    api: runtime_api.ExecutionCenterAPI,
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
    counts: read_model.TaskSnapshot,
) -> None:
    """Compact cross-project analytics for the consolidated main dashboard."""
    active_tasks = [task for task in tasks if task.get("status") in read_model.ACTIVE_LANES]
    blocked_tasks = [task for task in tasks if task.get("status") == "Blocked"]
    completion_rate = (
        round(counts.done / counts.total * 100) if counts.total else 0
    )

    with st.container(horizontal=True):
        st.metric("Всего задач", counts.total, border=True)
        st.metric("Активные", counts.active, border=True)
        st.metric("В статусе Blocked", counts.blocked, border=True)
        st.metric(
            "Выполнено",
            f"{counts.done} ({completion_rate}%)",
            border=True,
        )
        if counts.other:
            st.metric("Другие статусы", counts.other, border=True)
        st.metric("Требуют внимания", counts.attention, border=True)

    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Проекты")
        for project in models.PROJECT_IDS:
            project_tasks = [
                task
                for task in tasks
                if project_config.project_matches(task.get("project"), project)
            ]
            project_counts = read_model.task_snapshot(project_tasks)
            with st.container(border=True):
                cols = st.columns([3, 1, 1, 1, 1])
                cols[0].markdown(f"**{project}**")
                cols[1].metric("Актив.", project_counts.active)
                cols[2].metric("Blocked", project_counts.blocked)
                cols[3].metric("Готово", project_counts.done)
                if cols[4].button(
                    "Открыть",
                    key=f"dashboard_analytics_project_{project}",
                    icon=":material/arrow_forward:",
                ):
                    st.session_state.pending_nav = "projects"
                    st.session_state.pending_project_browser = project
                    st.rerun()

    with right:
        st.markdown("#### Приоритеты")
        priority_counts = {
            priority: sum(
                1 for task in active_tasks if task.get("priority") == priority
            )
            for priority in task_view.kanban_priority_options(active_tasks)
        }
        if any(priority_counts.values()):
            st.bar_chart(priority_counts)
        else:
            st.info("Нет активных задач.")

        st.markdown("#### Фактическая загрузка агентов")
        settings = task_pipeline.pipeline_settings.load_settings(ROOT)
        load = scheduler.build_load_snapshot(api.db_path)
        registry = scheduler.default_registry(
            max_concurrency=settings.max_agent_concurrency
        )
        for agent in registry.all():
            used = int(load.running_by_agent.get(agent.agent_id, 0))
            free = max(0, agent.max_concurrency - used) if agent.available else 0
            tone = "🟢" if agent.available and free else "🟠" if agent.available else "🔴"
            st.caption(
                f"{tone} `{agent.agent_id}` — {used}/{agent.max_concurrency}"
                + (f" · свободно {free}" if agent.available else " · недоступен")
            )

    st.markdown("#### Требуют решения")
    if not blocked_tasks:
        st.success("Заблокированных задач нет.")
    for task in blocked_tasks[:10]:
        unmet = unmet_dependencies(task, tasks_by_id)
        names = ", ".join(
            tasks_by_id.get(dep_id, {}).get("title") or dep_id for dep_id in unmet
        )
        with st.container(border=True):
            cols = st.columns([5, 1])
            cols[0].markdown(
                f"**{task.get('title') or 'Без названия'}** · {task.get('project')}"
            )
            cols[0].caption(f"Ожидает: {names or 'уточнения причины'}")
            if cols[1].button(
                "Kanban",
                key=f"dashboard_blocked_{task.get('id')}",
                icon=":material/view_kanban:",
            ):
                st.session_state.pending_nav = "kanban"
                st.rerun()


if page_key == "dashboard":
    dashboard_api = get_execution_center_api()
    dashboard_section = st.segmented_control(
        "Раздел",
        ["Обзор", "Аналитика", "Репозитории и артефакты"],
        default="Обзор",
        key="dashboard_section",
    )
    # Unlike ``st.tabs``, this renders only the selected section.  Repository
    # discovery and analytics no longer execute invisibly on every overview
    # refresh, which keeps the home page responsive on large workspaces.
    if dashboard_section == "Аналитика":
        render_dashboard_analytics(dashboard_api, tasks, tasks_by_id, task_counts)
    elif dashboard_section == "Репозитории и артефакты":
        render_workspace_home_page(dashboard_api, tasks, tasks_by_id)
    else:
        render_home_dashboard(dashboard_api, tasks, task_counts)


# --------------------------------------------------------------------------
# Command Center — the tokens-styled dashboard over the /api/v1 surface
# --------------------------------------------------------------------------

elif page_key == "command":
    operator_dashboard.render()


# --------------------------------------------------------------------------
# Task dependencies + priority order (VOYN-W2-TASKS)
# --------------------------------------------------------------------------

elif page_key == "task_deps":
    content_area.page_header(
        "Зависимости и приоритет",
        "Граф зависимостей задач проекта и явный порядок приоритета. "
        "Порядок нельзя выставить так, чтобы задача обгоняла свою зависимость.",
    )
    _deps_project = project_selector.render_project_selector(
        tasks, key="task_deps_project_selector"
    )
    task_dependencies.render(_deps_project)


# --------------------------------------------------------------------------
# Workspace Home
# --------------------------------------------------------------------------

elif page_key == "workspace_home":
    content_area.page_header(
        "Workspace Home",
        "Кросс-проектная сводка: репозитории, прогоны, артефакты и отчёты — "
        "в одном месте, с health-метриками и рекомендациями следующих действий.",
    )
    render_workspace_home_page(get_execution_center_api(), tasks, tasks_by_id)


# --------------------------------------------------------------------------
# AML Alerts (DATA-1)
# --------------------------------------------------------------------------

elif page_key == "compliance":
    compliance_dashboard.render()

elif page_key == "alerts":
    alert_panel.render()

# --------------------------------------------------------------------------
# AML Customers & Risk
# --------------------------------------------------------------------------

elif page_key == "customers":
    customer_panel.render()

# --------------------------------------------------------------------------
# AML Rules
# --------------------------------------------------------------------------

elif page_key == "rules":
    rules_panel.render()

# --------------------------------------------------------------------------
# AML Cases
# --------------------------------------------------------------------------

elif page_key == "cases":
    case_panel.render()

# --------------------------------------------------------------------------
# SAR Filing
# --------------------------------------------------------------------------

elif page_key == "sar":
    sar_panel.render()

# --------------------------------------------------------------------------
# AML Monitoring
# --------------------------------------------------------------------------

elif page_key == "aml":
    aml_panel.render()


# --------------------------------------------------------------------------
# Executive Dashboard
# --------------------------------------------------------------------------

elif page_key == "executive":
    st.subheader("Исполнительная панель", anchor="executive")

    render_next_task_callout(
        tasks,
        active_runs=get_execution_center_api().list_runs(states=runtime_db.EXECUTION_CENTER_ACTIVE_STATES),
    )

    active_tasks = [
        task for task in tasks if task.get("status") in read_model.ACTIVE_LANES
    ]
    blocked_tasks = [task for task in tasks if task.get("status") == "Blocked"]
    completion_rate = (
        f"{(task_counts.done / task_counts.total * 100):.0f}%"
        if task_counts.total
        else "—"
    )
    total_estimate = sum(task.get("estimate_hours", 0.0) for task in active_tasks)

    with st.container(horizontal=True):
        st.metric("Всего задач", task_counts.total, border=True)
        st.metric("Активные", task_counts.active, border=True)
        st.metric("В статусе Blocked", task_counts.blocked, border=True)
        st.metric("Выполнено", f"{task_counts.done} ({completion_rate})", border=True)
        if task_counts.other:
            st.metric("Другие статусы", task_counts.other, border=True)
        st.metric("Требуют внимания", task_counts.attention, border=True)
        st.metric("Оценка нагрузки", format_estimate(total_estimate), border=True)

    st.divider()

    left, right = st.columns([3, 2])

    with left:
        st.markdown("#### Статус проектов")
        statuses = parse_project_statuses()
        for project in models.PROJECT_IDS:
            # Canonical-id match (shared helper) so display-name tasks count
            # under their project — consistent with the Kanban lane and pill.
            project_tasks = [task for task in tasks if project_config.project_matches(task.get("project"), project)]
            project_counts = read_model.task_snapshot(project_tasks)
            status_file = project_status_file_path(project)

            with st.container(border=True):
                header_cols = st.columns([2, 1, 1, 1])
                header_cols[0].markdown(f"**{project}**")
                header_cols[0].caption(statuses.get(project, "—"))
                header_cols[1].metric("Активн.", project_counts.active)
                header_cols[2].metric("Blocked", project_counts.blocked)
                header_cols[3].metric("Готово", project_counts.done)
                st.caption(f"Статус-файл обновлён: {format_mtime(status_file)}")

    with right:
        st.markdown("#### Приоритеты активных задач")
        if active_tasks:
            priority_counts = dict.fromkeys(PRIORITIES, 0)
            for task in active_tasks:
                priority = task.get("priority", "Medium")
                priority_counts[priority] = priority_counts.get(priority, 0) + 1
            st.bar_chart(priority_counts)
        else:
            st.info("Нет активных задач.")

        st.markdown("#### Загрузка по исполнителям")
        owner_counts: dict[str, int] = {}
        for task in active_tasks:
            owner = task.get("owner") or "Не назначено"
            owner_counts[owner] = owner_counts.get(owner, 0) + 1
        if owner_counts:
            with st.container(border=True):
                for owner, count in sorted(owner_counts.items(), key=lambda item: item[1], reverse=True):
                    st.caption(f"{owner} — {count}")
        else:
            st.info("Нет активных задач.")

    st.divider()
    st.markdown("#### Заблокированные задачи")
    if not blocked_tasks:
        st.success("Заблокированных задач нет.")
    else:
        for task in blocked_tasks:
            unmet = unmet_dependencies(task, tasks_by_id)
            names = ", ".join(
                tasks_by_id[dep_id].get("title", "?")[:40] if dep_id in tasks_by_id else f"(удалена) {dep_id}"
                for dep_id in unmet
            )
            with st.container(border=True):
                st.markdown(f"**{(task.get('title') or '')[:80]}** · {task.get('project')}")
                st.caption(f"Ожидает: {names}")

    st.divider()
    st.markdown("#### Метрики запусков агентов")

    # Unified runs: v2 runtime.db (canonical) + legacy v1.2 journal merged —
    # the old `agent_runner.load_runs()` read only the v1.2 journal, which is
    # empty on installs that launch through the Execution Center, so every
    # metric below was always zero. See `command_center.runtime.runs_read`.
    exec_api = get_execution_center_api()
    exec_runs = runs_read.list_unified_runs(exec_api.db_path, root=ROOT)
    today = datetime.now().date()
    runs_today = [
        run
        for run in exec_runs
        if (run_ts := _parse_iso_ts(run.get("created_at"))) is not None
        and datetime.fromtimestamp(run_ts).date() == today
    ]
    successful_runs = [run for run in exec_runs if run.get("status") == "completed"]
    failed_runs = [run for run in exec_runs if run.get("status") in ("failed", "timed_out")]
    awaiting_remediation = [task for task in tasks if task.get("workflow_stage") == "Remediation"]
    awaiting_final_review = [task for task in tasks if task.get("workflow_stage") == "Final Review"]
    approved_for_commit = [task for task in tasks if task.get("latest_verdict") == models.VERDICT_APPROVED_FOR_COMMIT]

    with st.container(horizontal=True):
        st.metric("Запусков сегодня", len(runs_today), border=True)
        st.metric("Успешных", len(successful_runs), border=True)
        st.metric("Неудачных", len(failed_runs), border=True)
        st.metric("Ожидают исправления", len(awaiting_remediation), border=True)
        st.metric("Ожидают финальной проверки", len(awaiting_final_review), border=True)
        st.metric("Одобрено для commit", len(approved_for_commit), border=True)

    exec_left, exec_right = st.columns(2)
    with exec_left:
        st.markdown("##### Средняя длительность по агентам")
        durations_by_agent: dict[str, list[float]] = {}
        for run in exec_runs:
            duration = run.get("duration_seconds")
            if isinstance(duration, (int, float)):
                durations_by_agent.setdefault(run.get("agent", "—"), []).append(duration)
        if durations_by_agent:
            for agent_name, values in durations_by_agent.items():
                st.caption(f"{agent_name}: {sum(values) / len(values):.1f} с (n={len(values)})")
        else:
            st.caption("Запусков пока нет.")

    with exec_right:
        st.markdown("##### Открытые находки (Blocker/High)")
        open_blocker = sum(report_parser.severity_counts(run.get("parsed")).get("Blocker", 0) for run in exec_runs)
        open_high = sum(report_parser.severity_counts(run.get("parsed")).get("High", 0) for run in exec_runs)
        metric_cols = st.columns(2)
        metric_cols[0].metric("Blocker", open_blocker)
        metric_cols[1].metric("High", open_high)


# --------------------------------------------------------------------------
# Task creator
# --------------------------------------------------------------------------

elif page_key == "create":
    st.subheader("Создание AI-задачи", anchor="create-task")

    open_tasks = [task for task in tasks if task.get("status") != "Done"]

    # `project` lives outside `create_task_form` on purpose: its value must be
    # available immediately (a form's inner widgets don't rerun the script
    # until submitted) so the inherited-defaults preview below reacts to the
    # project the user just picked, before they submit anything.
    project = st.selectbox("Проект", models.PROJECT_IDS, key="create_task_project")
    create_task_cfg = project_configs[project]
    inherited = project_config.task_defaults_from_project(create_task_cfg)

    st.caption(
        f"Унаследовано из настроек проекта «{create_task_cfg['display_name']}»: "
        f"workspace `{inherited['workspace_path'] or '—'}` · "
        f"branch `{inherited['branch'] or '—'}` · "
        f"executor `{inherited['executor'] or '—'}` · "
        f"prompt {'задан' if inherited['prompt'] else '—'}. "
        "Изменить можно в разделе «Проекты» → «Настройки проекта», либо переопределить ниже только для этой задачи."
    )

    with st.expander("Переопределить workspace / branch / executor / prompt для этой задачи"):
        override_workspace = st.text_input(
            "Workspace (переопределение)",
            placeholder=inherited["workspace_path"] or "унаследовано из проекта",
            key="create_task_workspace_override",
        )
        override_branch = st.text_input(
            "Branch (переопределение)",
            placeholder=inherited["branch"] or "унаследовано из проекта",
            key="create_task_branch_override",
        )
        executor_override_options = ["(унаследовано из проекта)"] + executors.EXECUTOR_IDS
        override_executor = st.selectbox(
            "Executor (переопределение)",
            executor_override_options,
            key="create_task_executor_override",
        )
        override_prompt = st.text_area(
            "Prompt (переопределение)",
            placeholder=inherited["prompt"] or "унаследовано из проекта",
            key="create_task_prompt_override",
        )

    with st.form("create_task_form"):
        title_input = st.text_input(
            "Название задачи",
            placeholder="Короткий заголовок, например: Исправить сортировку в Kanban",
            key="create_task_title",
        )
        task_type = st.selectbox(
            "Тип задачи",
            TASK_TYPES,
            format_func=lambda value: TASK_TYPE_LABELS.get(value, value),
            key="create_task_type",
        )
        objective = st.text_area(
            "Цель задачи",
            height=160,
            placeholder="Например: проверить текущий статус AIOS и определить следующую задачу",
            key="create_task_objective",
        )
        notes = st.text_area(
            "Заметки (необязательно)",
            height=80,
            placeholder="Свободные заметки, независимые от цели и промпта",
            key="create_task_notes",
        )

        col_a, col_b, col_c = st.columns(3)
        with col_a:
            priority = st.selectbox("Приоритет", PRIORITIES, index=1, key="create_task_priority")
        with col_b:
            owner = st.text_input("Исполнитель", placeholder="Например: Дмитрий", key="create_task_owner")
        with col_c:
            estimate = st.number_input(
                "Оценка (часы)", min_value=0.0, step=0.5, value=0.0, key="create_task_estimate"
            )

        dependencies = st.multiselect(
            "Зависит от",
            options=[task["id"] for task in open_tasks],
            format_func=lambda task_id: task_label(tasks_by_id[task_id]),
            key="create_task_deps",
        )

        initial_status = st.selectbox(
            "Статус Kanban", MANUAL_KANBAN_STATUSES, key="create_task_status"
        )
        submitted = st.form_submit_button(
            "Создать задачу",
            icon=":material/add_task:",
            type="primary",
            key="create_task_form_submit",
        )

    if submitted:
        title_clean = title_input.strip()
        objective_clean = objective.strip()

        if not title_clean:
            st.error("Укажите название задачи.")
        elif not objective_clean:
            st.error("Укажите цель задачи.")
        elif project not in models.PROJECT_IDS:
            st.error("Неизвестный проект.")
        elif task_type not in TASK_TYPES:
            st.error("Неизвестный тип задачи.")
        else:
            final_executor = (
                None if override_executor == "(унаследовано из проекта)" else override_executor
            )
            create_task(
                project,
                title_clean,
                task_type,
                initial_status,
                goal=objective_clean,
                notes=notes.strip(),
                priority=priority,
                owner=owner.strip(),
                estimate_hours=float(estimate),
                depends_on=dependencies,
                workspace_path=override_workspace.strip() or inherited["workspace_path"],
                branch=override_branch.strip() or inherited["branch"],
                executor=final_executor or inherited["executor"],
                prompt=override_prompt.strip() or inherited["prompt"],
            )
            st.success(f"Задача создана и добавлена в Kanban (статус «{initial_status}»).")

            with st.spinner("Выполняется scripts/start-task.sh..."):
                ok, stdout, stderr = run_start_task_script(project, task_type, objective_clean)

            if ok:
                if stdout:
                    with st.expander("Вывод скрипта"):
                        st.code(stdout, language=None)
            else:
                st.warning(
                    "Задача сохранена в Kanban, но scripts/start-task.sh не смог "
                    "сгенерировать Markdown-файл."
                )
                details = stderr or stdout
                if details:
                    with st.expander("Подробности ошибки", expanded=True):
                        st.code(details, language=None)

    st.divider()
    st.markdown("#### Импорт пакета задач")
    st.caption(
        "Загрузите файл или вставьте пакет задач (например, пакет от Founder-аудита). "
        "Поддерживаются JSON, YAML, Markdown и простой текст, в двух формах: «конверт» "
        "`{schema_version, package_id, tasks}` или простой список задач. Ничего не "
        "записывается в `data/tasks.json` до нажатия «Импортировать задачи»."
    )
    # Result of the previous run's applied import, carried across `st.rerun()`
    # via the same flash pattern as `_LAUNCH_FLASH_KEY`: a message rendered
    # right before `st.rerun()` belongs to the pre-rerun frame, which the rerun
    # replaces immediately — the operator (and `AppTest`'s element tree on
    # streamlit >= 1.61, which no longer merges the discarded frame) never sees
    # it unless it is re-rendered on the post-rerun frame like this.
    import_task_flash = st.session_state.pop(_IMPORT_TASK_FLASH_KEY, None)
    if import_task_flash:
        st.success(import_task_flash)
    uploaded_package = st.file_uploader(
        "Файл пакета задач (JSON / YAML / Markdown / текст)",
        type=list(task_import.SUPPORTED_IMPORT_SUFFIXES),
        key="import_task_package_uploader",
    )
    pasted_package = st.text_area(
        "…или вставьте пакет сюда",
        key="import_task_package_paste",
        height=120,
        placeholder='{"tasks": [ … ]}   ·   YAML   ·   Markdown c ```json / ```yaml-блоком',
    )
    paste_format = st.selectbox(
        "Формат вставленного текста",
        ("авто", "json", "yaml", "markdown"),
        key="import_task_package_paste_format",
        help="«авто» распознаёт JSON или YAML. Для вставленного Markdown выберите его явно.",
    )

    parsed_package = None
    import_parse_error: task_import.TaskImportError | None = None
    if uploaded_package is not None:
        # Pass the real filename so the parser can pick YAML/Markdown by
        # extension, not just sniff JSON/YAML out of the raw bytes.
        try:
            parsed_package = task_import.parse_task_package(
                uploaded_package.getvalue(), filename=uploaded_package.name
            )
        except task_import.TaskImportError as exc:
            import_parse_error = exc
    elif pasted_package.strip():
        fmt = None if paste_format == "авто" else paste_format
        try:
            parsed_package = task_import.parse_task_package(pasted_package, fmt=fmt)
        except task_import.TaskImportError as exc:
            import_parse_error = exc

    if import_parse_error is not None or parsed_package is not None:
        if import_parse_error is not None:
            st.error(f"Ошибка разбора пакета: {import_parse_error}")
        else:
            import_validation = task_import.validate_task_package(parsed_package)
            import_preview = task_import.build_import_preview(ROOT, parsed_package, import_validation)

            info_cols = st.columns(5)
            info_cols[0].metric("Всего задач", import_preview.total_tasks)
            info_cols[1].metric("Новые", len(import_preview.new_items))
            info_cols[2].metric("Дубликаты", len(import_preview.duplicate_ids))
            info_cols[3].metric("Ошибки", len(import_preview.errors))
            info_cols[4].metric("Предупреждения", len(import_preview.warnings))
            st.caption(
                f"Package id: `{import_preview.package_id}` · schema: `{import_preview.schema_version}` · "
                f"hash: `{import_preview.package_hash}`"
            )

            if import_preview.rows:
                st.dataframe(
                    [
                        {
                            "ID": row.id,
                            "Импорт": row.outcome,
                            "Проект": row.project,
                            "Kanban": row.status,
                            "Приоритет": row.priority,
                            "Тип": row.task_type,
                            "Название": row.title,
                        }
                        for row in import_preview.rows
                    ],
                    hide_index=True,
                    width="stretch",
                )

            for issue in import_preview.errors:
                st.error(f"[{issue.task_ref or '—'}] {issue.message}")
            for issue in import_preview.warnings:
                st.warning(f"[{issue.task_ref or '—'}] {issue.message}")

            if import_preview.has_blocking_errors:
                st.error("Пакет содержит ошибки валидации — импорт заблокирован.")
            elif not import_preview.new_items:
                st.info("Нет новых задач для импорта — все задачи пакета уже присутствуют в хранилище.")
            elif st.button(
                f"Импортировать задачи ({len(import_preview.new_items)} новых)",
                key="import_task_package_confirm_btn",
                type="primary",
                icon=":material/publish:",
            ):
                try:
                    import_result = task_import.apply_task_package(ROOT, parsed_package, import_validation)
                except task_import.TaskImportError as exc:
                    # Re-checked fresh under lock inside `apply_task_package` — can
                    # still fail here even though the preview above looked clean,
                    # e.g. a concurrent import claimed a dependency's id, or the
                    # lock timed out. Surfaced as an ordinary page error, never an
                    # uncaught exception; nothing was written in either case.
                    st.error(f"Импорт не выполнен: {exc}")
                else:
                    # Flashed (not rendered inline) because the `st.rerun()`
                    # below wipes this frame before the message would be seen —
                    # the pop at the top of this section shows it post-rerun.
                    st.session_state[_IMPORT_TASK_FLASH_KEY] = (
                        f"Импортировано задач: {len(import_result.imported_ids)}. "
                        f"Пропущено дубликатов: {len(import_result.skipped_duplicate_ids)}."
                    )
                    st.rerun()


# --------------------------------------------------------------------------
# Project Chat
# --------------------------------------------------------------------------

elif page_key == "chat":
    st.subheader("Чат по проекту", anchor="project-chat")
    # The chat now lives as the 'Чат' tab inside the project view; this page is
    # kept so an existing deep link still resolves, and delegates to the same
    # `render_project_chat` the tab uses. (task 02661825)
    st.caption("Чат теперь встроен во вкладку «Чат» страницы «Проекты».")
    chat_project = st.selectbox("Проект", models.PROJECT_IDS, key="chat_project_select")
    render_project_chat(chat_project, tasks, tasks_by_id)


# --------------------------------------------------------------------------
# Kanban board
# --------------------------------------------------------------------------

elif page_key == "waves":
    project_filter = project_selector.render_project_selector(tasks, key="waves_project_selector")
    waves_panel.render_waves_page(tasks, tasks_by_id, ROOT, project=project_filter)

elif page_key == "master_backlog":
    master_backlog_panel.render_master_backlog_page()

elif page_key == "kanban":
    st.subheader("Kanban", anchor="kanban")

    project_filter = render_project_planning_intelligence(
        get_execution_center_api(),
        tasks,
        tasks_by_id,
        selector_key="kanban_project_selector",
        recommendation_key_prefix="kanban_reco",
        backlog_reconcile_key_prefix="kanban_reconcile",
    )
    st.divider()

    # Options come from the tasks themselves (canonical priorities + any
    # extra value actually in use, e.g. an imported `P0`), not just the
    # canonical PRIORITIES — otherwise a task whose priority is outside the
    # canonical set is neither selectable nor matched by the default
    # all-selected filter, and silently disappears from every lane (this is
    # exactly why AICC-CI-001, priority `P0`, was missing). See
    # `task_view.kanban_priority_options`.
    priority_options = task_view.kanban_priority_options(tasks)
    priority_filter = st.multiselect(
        "Приоритет", priority_options, default=priority_options, key="kanban_priority_filter"
    )

    filtered_tasks = task_view.filter_kanban_tasks(
        tasks, project=project_filter, priorities=priority_filter
    )
    filtered_counts = read_model.task_snapshot(filtered_tasks)

    kanban_git_status_cache: dict[str, dict] = {}
    kanban_api = get_execution_center_api()
    current_run_ids = [
        task["current_run_id"]
        for task in filtered_tasks
        if task.get("current_run_id")
    ]
    completions_by_run = kanban_api.get_completions_for_runs(current_run_ids)
    completions_by_task = {
        row["task_id"]: row for row in completions_by_run.values()
    }
    current_run_id_set = set(current_run_ids)
    current_runs_by_id = {
        run["id"]: run
        for run in kanban_api.list_runs(limit=200)
        if run.get("id") in current_run_id_set
    }
    kanban_now = datetime.now()

    def _kanban_live_progress(task: dict) -> tuple[int | None, str | None] | None:
        run = current_runs_by_id.get(task.get("current_run_id"))
        if run is None:
            return None
        status = session_view.derive_status(
            run,
            awaiting_handshake=session_view.is_awaiting_handshake(run),
        )
        estimate = task.get("estimate_hours")
        reference_seconds = float(estimate) * 3600 if estimate else run.get(
            "timeout_seconds"
        )
        return session_view.derive_live_progress(
            status,
            session_view.completion_view(
                completions_by_task.get(task.get("id"))
            ),
            task_type=run.get("task_type") or task.get("task_type"),
            elapsed_seconds=session_view.elapsed_seconds(
                run.get("started_at"), run.get("completed_at"), kanban_now
            ),
            timeout_seconds=reference_seconds,
            stage_progress=task.get("progress"),
            stage_label=task.get("current_stage"),
        )

    # Полноширинные вертикальные дорожки Kanban.
    # Горизонтальная разметка через st.columns сжимала карточки
    # и делала заголовки и элементы управления нечитаемыми.
    for status in read_model.CANONICAL_LANES:
        with st.container(border=True):
            status_tasks = [task for task in filtered_tasks if task.get("status") == status]
            st.markdown(f"**{status}**")
            st.caption(f"{filtered_counts.by_lane[status]} задач")

            if not status_tasks:
                st.caption("Пусто")

            for task in status_tasks:
                render_task_card(
                    task,
                    tasks=tasks,
                    tasks_by_id=tasks_by_id,
                    key_prefix=f"kanban_{task.get('id')}",
                    git_status_cache=kanban_git_status_cache,
                    completion=completions_by_task.get(task.get("id")),
                    live_progress=_kanban_live_progress(task),
                    show_kanban_controls=True,
                )

    if filtered_counts.other:
        with st.container(border=True):
            other_tasks = [
                task
                for task in filtered_tasks
                if task.get("status") not in read_model.CANONICAL_LANES
            ]
            st.markdown("**Другие статусы**")
            st.caption(f"{filtered_counts.other} задач")
            for task in other_tasks:
                render_task_card(
                    task,
                    tasks=tasks,
                    tasks_by_id=tasks_by_id,
                    key_prefix=f"kanban_{task.get('id')}",
                    git_status_cache=kanban_git_status_cache,
                    completion=completions_by_task.get(task.get("id")),
                    live_progress=_kanban_live_progress(task),
                    show_kanban_controls=True,
                )

# --------------------------------------------------------------------------
# AI Agents
# --------------------------------------------------------------------------

elif page_key == "agents":
    st.subheader("AI-агенты", anchor="agents")
    agents_section = st.segmented_control(
        "Раздел",
        ["Каталог", "Лидерборд"],
        default="Каталог",
        key="agents_section",
    )

    if agents_section == "Лидерборд":
        leaderboard_panel.render_leaderboard_panel(
            db_path=get_execution_center_api().db_path, root=ROOT
        )
        st.stop()

    st.caption("Каталог типов задач, поддерживаемых scripts/start-task.sh")

    generated_files = artifacts.list_markdown_files(GENERATED_DIR)

    for task_type in TASK_TYPES:
        meta = AGENT_ROLES[task_type]
        type_tasks = [task for task in tasks if task.get("task_type") == task_type]
        type_counts = read_model.task_snapshot(type_tasks)
        generated_count = sum(1 for path in generated_files if artifacts.infer_task_type_from_filename(path) == task_type)

        with st.container(border=True):
            st.markdown(f"### {meta['title']}")
            st.caption(f"`{task_type}` · {meta['summary']}")

            metric_cols = st.columns(3)
            metric_cols[0].metric("Активные задачи", type_counts.active)
            metric_cols[1].metric("Завершено", type_counts.done)
            metric_cols[2].metric("Сгенерировано файлов", generated_count)

            with st.expander("Правила выполнения"):
                for rule in meta["rules"]:
                    st.markdown(f"- {rule}")

            if st.button(
                f"Создать задачу «{meta['title']}»",
                key=f"agent_create_{task_type}",
                icon=":material/add_task:",
            ):
                st.session_state.pending_nav = "create"
                st.session_state.pending_create_type = task_type
                st.rerun()

            with st.expander(f"Запустить «{meta['title']}» напрямую", icon=":material/smart_toy:"):
                agent_launch_project = st.selectbox(
                    "Проект", models.PROJECT_IDS, key=f"agent_launch_project_{task_type}"
                )
                agent_launch_objective = st.text_area(
                    "Цель задачи",
                    key=f"agent_launch_objective_{task_type}",
                    height=120,
                    placeholder="Опишите, что должен сделать агент",
                )
                render_agent_launcher(
                    key_prefix=f"agents_page_launch_{task_type}",
                    project=agent_launch_project,
                    default_prompt=agent_launch_objective,
                    tasks=tasks,
                    default_task_type=task_type,
                )


# --------------------------------------------------------------------------
# Live Execution Center (v2 Session Supervisor)
# --------------------------------------------------------------------------

elif page_key == "execution_center":
    st.subheader("Live Execution Center", anchor="execution-center")
    st.caption(
        "Канонический монитор выполнения: реальные PID-отслеживаемые прогоны через "
        "v2 Session Supervisor (command_center.runtime) — источник истины для статуса "
        "выполнения, сверяемый с реальными OS-процессами при каждом обновлении."
    )

    render_live_execution_center(get_execution_center_api(), tasks)


# --------------------------------------------------------------------------
# Persistent daily product/engineering audit
# --------------------------------------------------------------------------

elif page_key == "daily_audit":
    daily_audit_panel.render_daily_audit_page(get_execution_center_api().db_path)


# --------------------------------------------------------------------------
# Run journal
# --------------------------------------------------------------------------

elif page_key == "runs":
    st.subheader("Журнал запусков", anchor="runs")
    runs_view = st.segmented_control(
        "Представление",
        ["Список", "Таймлайн"],
        default="Список",
        key="runs_view_mode",
    )
    if runs_view == "Таймлайн":
        project_filter = st.selectbox(
            "Фильтр по проекту",
            ["Все"] + models.PROJECT_IDS,
            key="runs_timeline_project_filter",
        )
        events = build_timeline_events(
            tasks,
            runs=runs_read.list_unified_runs(
                get_execution_center_api().db_path, root=ROOT
            ),
            activity_events=activity_log.load_activity(limit=200),
            limit=200,
        )
        if project_filter != "Все":
            events = [
                event
                for event in events
                if project_config.project_matches(
                    event.get("project"), project_filter
                )
            ]
        if not events:
            st.info("Событий пока нет.")
        else:
            current_date: str | None = None
            for event in events[:150]:
                event_date = datetime.fromtimestamp(event["ts"]).strftime(
                    "%d.%m.%Y"
                )
                if event_date != current_date:
                    current_date = event_date
                    st.markdown(f"#### {current_date}")
                time_str = datetime.fromtimestamp(event["ts"]).strftime("%H:%M")
                project_tag = (
                    f" · {event['project']}" if event.get("project") else ""
                )
                st.caption(
                    f"{event['icon']} {time_str}{project_tag} — {event['label']}"
                )
        st.stop()

    # Unified runs (v2 runtime.db + legacy v1.2 journal) — the old
    # `agent_runner.load_runs()` read only the v1.2 journal, which is empty on
    # installs that launch through the Execution Center, so the whole page was
    # blank. See `command_center.runtime.runs_read`.
    all_runs = runs_read.list_unified_runs(get_execution_center_api().db_path, root=ROOT)

    filter_cols = st.columns(4)
    with filter_cols[0]:
        runs_project_filter = st.selectbox("Проект", ["Все"] + models.PROJECT_IDS, key="runs_project_filter")
    with filter_cols[1]:
        runs_agent_filter = st.selectbox(
            "Агент", ["Все"] + sorted({run.get("agent", "—") for run in all_runs}), key="runs_agent_filter"
        )
    with filter_cols[2]:
        runs_status_filter = st.multiselect(
            "Статус", models.RUN_STATUSES, default=models.RUN_STATUSES,
            format_func=lambda v: models.RUN_STATUS_LABELS.get(v, v), key="runs_status_filter",
        )
    with filter_cols[3]:
        verdict_choices = list(models.VERDICT_LABELS.keys())
        runs_verdict_filter = st.multiselect(
            "Вердикт", verdict_choices, default=verdict_choices,
            format_func=lambda v: models.VERDICT_LABELS.get(v, v), key="runs_verdict_filter",
        )

    date_cols = st.columns(2)
    with date_cols[0]:
        runs_date_from = st.date_input("С даты", value=None, key="runs_date_from")
    with date_cols[1]:
        runs_date_to = st.date_input("По дату", value=None, key="runs_date_to")

    task_choices = ["Все"] + sorted({run.get("task_id") for run in all_runs if run.get("task_id")})
    runs_task_filter = st.selectbox(
        "Задача", task_choices,
        format_func=lambda v: "Все" if v == "Все" else task_label(tasks_by_id.get(v, {"project": "—", "title": v, "status": "—"})),
        key="runs_task_filter",
    )

    def _run_matches_filters(run: dict) -> bool:
        if runs_project_filter != "Все" and run.get("project") != runs_project_filter:
            return False
        if runs_agent_filter != "Все" and run.get("agent") != runs_agent_filter:
            return False
        if run.get("status") not in runs_status_filter:
            return False
        run_verdict = (run.get("parsed") or {}).get("verdict")
        if run_verdict and run_verdict not in runs_verdict_filter:
            return False
        if runs_task_filter != "Все" and run.get("task_id") != runs_task_filter:
            return False
        created_ts = _parse_iso_ts(run.get("created_at"))
        if created_ts is not None:
            created_date = datetime.fromtimestamp(created_ts).date()
            if runs_date_from and created_date < runs_date_from:
                return False
            if runs_date_to and created_date > runs_date_to:
                return False
        return True

    filtered_runs = [run for run in all_runs if _run_matches_filters(run)]
    st.caption(f"Найдено запусков: {len(filtered_runs)} из {len(all_runs)}")

    if not filtered_runs:
        st.info("Запусков, соответствующих фильтрам, не найдено.")

    for run in filtered_runs:
        parsed = run.get("parsed") or report_parser.empty_parsed_result()
        effective_parsed = report_parser.apply_manual_corrections(parsed)
        counts = report_parser.severity_counts(parsed)

        with st.container(border=True):
            header_cols = st.columns([3, 1, 1, 1])
            header_cols[0].markdown(f"**{run.get('project')} · {run.get('task_type')} · {run.get('agent')}**")
            header_cols[0].caption(f"{run.get('created_at', '—')} · repo: `{run.get('repository_path', '—')}`")
            header_cols[1].badge(
                models.RUN_STATUS_LABELS.get(run.get("status"), run.get("status")),
                color=models.RUN_STATUS_COLORS.get(run.get("status"), "gray"),
            )
            if effective_parsed.get("verdict"):
                header_cols[2].badge(
                    models.VERDICT_LABELS.get(effective_parsed["verdict"], effective_parsed["verdict"]), color="blue"
                )
            duration = run.get("duration_seconds")
            header_cols[3].caption(f"{duration:.1f}с" if isinstance(duration, (int, float)) else "—")

            if any(counts.values()):
                st.caption("Находки: " + " · ".join(f"{sev}: {counts[sev]}" for sev in models.SEVERITIES if counts[sev]))

            with st.expander("Детали запуска", icon=":material/info:"):
                st.write(f"Run ID: `{run['id']}`")
                st.write(f"Task ID: `{run.get('task_id') or '—'}`")
                pre_run = run.get("pre_run") or {}
                post_run = run.get("post_run") or {}
                st.write(f"Ветка до запуска: {pre_run.get('branch') or '—'} · после: {post_run.get('branch') or '—'}")
                st.write(f"HEAD до запуска: {pre_run.get('head') or '—'} · после: {post_run.get('head') or '—'}")
                st.write(f"Commit hash: {effective_parsed.get('commit_hash') or 'не указан'}")
                st.write(f"Recommended next action: {effective_parsed.get('recommended_next_action') or 'не указано'}")

                st.markdown("**Промпт:**")
                st.code(run.get("prompt", ""), language=None)

                stdout_text = run.get("stdout", "")
                st.markdown("**Stdout (предпросмотр в интерфейсе):**")
                st.code(stdout_text[:5000] or "—", language=None)
                if len(stdout_text) > 5000:
                    st.caption(
                        "Показаны первые 5000 символов вывода в интерфейсе — полный текст "
                        "сохранён в файле отчёта без сокращений."
                    )
                if run.get("stderr"):
                    st.markdown("**Stderr:**")
                    st.code(run["stderr"], language=None)

                if run.get("report_path"):
                    st.write(f"Отчёт: `{run['report_path']}`")
                    report_full_path = agent_runner.resolve_report_path(run)
                    if report_full_path is None:
                        st.warning("Путь к отчёту не проходит проверку безопасности — файл не открыт.")
                    elif report_full_path.exists():
                        with st.expander("Полный текст отчёта"):
                            st.markdown(read_text(report_full_path))

                if run.get("next_task_id"):
                    st.success(f"Следующая задача уже создана: `{run['next_task_id']}`")

                # Manual field correction write-back is a v1.2-journal feature
                # (it appends to runs.jsonl). v2 runs live in runtime.db and are
                # read-only here — persisting a correction would require a v2
                # correction store that does not exist yet, so we surface that
                # honestly rather than silently writing a stale v1.2 snapshot.
                if run.get("source") == "v1.2":
                    st.markdown("**Ручная корректировка полей**")
                    correction_cols = st.columns([1, 2, 1])
                    with correction_cols[0]:
                        correction_field = st.selectbox(
                            "Поле", report_parser.CORRECTABLE_FIELDS, key=f"run_correct_field_{run['id']}"
                        )
                    with correction_cols[1]:
                        correction_value = st.text_input("Значение", key=f"run_correct_value_{run['id']}")
                    with correction_cols[2]:
                        st.write("")
                        if st.button("Сохранить", key=f"run_correct_btn_{run['id']}"):
                            if correction_value.strip():
                                corrected_parsed = report_parser.set_manual_correction(
                                    parsed, correction_field, correction_value.strip()
                                )
                                run["parsed"] = corrected_parsed
                                agent_runner.append_run(run)
                                activity_log.log_event(
                                    "manual_field_correction", project=run.get("project"), task_id=run.get("task_id"),
                                    run_id=run["id"], message=f"{correction_field} -> {correction_value.strip()[:80]}",
                                )
                                st.success("Сохранено.")
                                st.rerun()
                else:
                    st.caption("Ручная корректировка полей доступна только для записей из журнала v1.2; этот прогон хранится в runtime.db и доступен только для чтения.")

            render_create_next_task_widget(run, tasks, key_prefix=f"runs_page_{run['id']}")


# --------------------------------------------------------------------------
# Timeline
# --------------------------------------------------------------------------

elif page_key == "timeline":
    st.subheader("Таймлайн", anchor="timeline")

    project_filter = st.selectbox("Фильтр по проекту", ["Все"] + models.PROJECT_IDS, key="timeline_project_filter")

    events = build_timeline_events(
        tasks, runs=runs_read.list_unified_runs(get_execution_center_api().db_path, root=ROOT),
        activity_events=activity_log.load_activity(limit=200), limit=200,
    )
    if project_filter != "Все":
        # Canonical-id match (shared helper): task-sourced timeline events carry
        # the task's raw `project`, which may be a display name.
        events = [event for event in events if project_config.project_matches(event.get("project"), project_filter)]

    if not events:
        st.info("Событий пока нет.")
    else:
        current_date: str | None = None
        for event in events[:150]:
            event_date = datetime.fromtimestamp(event["ts"]).strftime("%d.%m.%Y")
            if event_date != current_date:
                current_date = event_date
                st.markdown(f"#### {current_date}")
            time_str = datetime.fromtimestamp(event["ts"]).strftime("%H:%M")
            project_tag = f" · {event['project']}" if event.get("project") else ""
            st.caption(f"{event['icon']} {time_str}{project_tag} — {event['label']}")


# --------------------------------------------------------------------------
# Project browser
# --------------------------------------------------------------------------

elif page_key == "projects":
    st.subheader("Проекты", anchor="projects")

    project_choice = st.selectbox("Проект", ["Все", *models.PROJECT_IDS], key="project_browser_select")

    # "Все" is a cross-project overview; picking a project opens it *inside* the
    # project (status, tasks, reports, context, settings, chat as tabs) rather
    # than as separate sidebar pages. (task 02661825)
    if project_choice == "Все":
        st.caption(
            "Обзор всех проектов — выберите проект, чтобы открыть его внутри "
            "(задания, отчёты, контекст, настройки, чат)."
        )
        for overview_pid in models.PROJECT_IDS:
            pid_tasks = [t for t in tasks if project_config.project_matches(t.get("project"), overview_pid)]
            pid_active = [t for t in pid_tasks if t.get("status") != "Done"]
            pid_done = [t for t in pid_tasks if t.get("status") == "Done"]
            with st.container(border=True):
                st.markdown(f"**{project_configs[overview_pid]['display_name']}** (`{overview_pid}`)")
                st.caption(f"{len(pid_active)} активных · {len(pid_done)} завершено · всего {len(pid_tasks)}")
                if st.button(
                    "Открыть проект",
                    key=f"project_overview_open_{overview_pid}",
                    icon=":material/arrow_forward:",
                    width="stretch",
                ):
                    st.session_state["pending_project_browser"] = overview_pid
                    st.rerun()
        st.stop()

    selected_project = project_choice
    project_file = project_status_file_path(selected_project)

    tab_status, tab_generated, tab_reports, tab_context, tab_settings, tab_chat, tab_audit, tab_roadmap = st.tabs(
        ["Статус", "Задания", "Отчёты", "Контекст", "Настройки", "Чат", "Аудит", "Roadmap"]
    )

    with tab_status:
        st.caption(f"Изменён: {format_mtime(project_file)}")
        st.markdown(read_text(project_file))

    with tab_generated:
        files = artifacts.list_markdown_files(GENERATED_DIR / selected_project)
        if not files:
            st.info("Для проекта пока нет сгенерированных задач.")
        else:
            chosen_name = st.selectbox("Файл задания", [path.name for path in files], key="proj_gen_select")
            chosen_path = next(path for path in files if path.name == chosen_name)
            st.caption(f"Изменён: {format_mtime(chosen_path)}")
            chosen_content = read_text(chosen_path)
            st.markdown(chosen_content)
            st.divider()
            render_agent_launcher(
                key_prefix=f"proj_gen_launch_{selected_project}_{chosen_name}",
                project=selected_project,
                default_prompt=chosen_content,
                tasks=tasks,
                default_task_type=artifacts.infer_task_type_from_filename(chosen_path) or "implementation",
            )

    with tab_reports:
        files = artifacts.list_markdown_files(REPORTS_DIR / selected_project)
        if not files:
            st.info("Для проекта пока нет отчётов.")
        else:
            chosen_name = st.selectbox("Файл отчёта", [path.name for path in files], key="proj_report_select")
            chosen_path = next(path for path in files if path.name == chosen_name)
            st.caption(f"Изменён: {format_mtime(chosen_path)}")
            st.markdown(read_text(chosen_path))

    with tab_context:
        context_name = CONTEXT_FILES.get(selected_project)
        if not context_name:
            st.info(f"Для проекта {selected_project} отдельный файл контекста ещё не создан.")
        else:
            context_path = CONTEXT_DIR / context_name
            if not context_path.exists():
                st.warning(f"Файл контекста не найден: context/{context_name}")
            else:
                st.caption(f"Изменён: {format_mtime(context_path)}")
                st.markdown(read_text(context_path))

    with tab_settings:
        cfg = project_configs[selected_project]
        st.write(f"Проект: **{cfg['display_name']}** (`{selected_project}`)")
        if cfg["sensitive"]:
            st.warning(
                "Проект помечен как чувствительный (BANK/LEGAL): файлы для агента не "
                "прикладываются автоматически, контекст добавляется вручную при запуске."
            )

        current_path = cfg.get("repository_path")
        if current_path:
            st.success(f"Текущий путь репозитория: `{current_path}`")
        else:
            st.info("Путь к репозиторию не настроен.")

        suggested_path = project_config.discover_candidate_repository_path(selected_project)
        if suggested_path and not current_path:
            st.info(
                f"Обнаружен вероятный путь репозитория (существующий git-репозиторий на "
                f"этой машине): `{suggested_path}`. Проверьте и сохраните, если это верно."
            )

        new_path_input = st.text_input(
            "Путь к репозиторию",
            value=current_path or suggested_path or "",
            key=f"repo_path_input_{selected_project}",
        )
        settings_cols = st.columns(2)
        with settings_cols[0]:
            if st.button("Сохранить путь", key=f"repo_path_save_{selected_project}", icon=":material/save:"):
                ok, message = project_config.validate_repository_path(new_path_input)
                if ok:
                    project_config.save_repository_path(selected_project, new_path_input.strip())
                    st.success("Путь сохранён.")
                    st.rerun()
                else:
                    st.error(message)
        with settings_cols[1]:
            if current_path and st.button(
                "Очистить путь", key=f"repo_path_clear_{selected_project}", icon=":material/delete:"
            ):
                project_config.save_repository_path(selected_project, None)
                st.success("Путь очищен.")
                st.rerun()

        st.caption(f"Разрешённые агенты: {', '.join(cfg['allowed_agents'])}")
        st.caption(f"Каталог отчётов: `{cfg['reports_dir']}` · Каталог заданий: `{cfg['generated_dir']}`")
        st.caption("Файлы контекста: " + (", ".join(f"`{p}`" for p in cfg["context_file_paths"]) or "—"))

        st.divider()
        st.markdown("#### Настройки проекта (по умолчанию для новых задач)")
        st.caption(
            "Эти значения автоматически наследуются новыми задачами проекта "
            "(workspace, branch, executor, prompt) на странице «Создать задачу»."
        )
        # Outcome of the previous run's save, carried across `st.rerun()` via
        # the `_LAUNCH_FLASH_KEY` flash pattern: advisory validation warnings
        # and the save confirmation rendered right before `st.rerun()` belong
        # to the pre-rerun frame, which the rerun replaces immediately — they
        # must be re-rendered here on the post-rerun frame to be seen at all.
        settings_flash = st.session_state.pop(_PROJECT_SETTINGS_FLASH_KEY, None)
        if settings_flash:
            for warning_message in settings_flash["warnings"]:
                st.warning(warning_message)
            st.success(settings_flash["success"])

        workspace_input = st.text_input(
            "Workspace по умолчанию",
            value=cfg.get("default_workspace_path") or "",
            key=f"default_workspace_input_{selected_project}",
        )
        branch_input = st.text_input(
            "Branch по умолчанию",
            value=cfg.get("default_branch") or "",
            key=f"default_branch_input_{selected_project}",
        )
        executor_options = ["(не задан)"] + executors.EXECUTOR_IDS
        current_executor = cfg.get("default_executor")
        executor_index = executor_options.index(current_executor) if current_executor in executor_options else 0
        executor_input = st.selectbox(
            "Executor по умолчанию",
            executor_options,
            index=executor_index,
            key=f"default_executor_input_{selected_project}",
        )
        prompt_input = st.text_area(
            "Prompt по умолчанию",
            value=cfg.get("default_prompt") or "",
            height=120,
            key=f"default_prompt_input_{selected_project}",
        )
        description_input = st.text_area(
            "Описание проекта",
            value=cfg.get("description") or "",
            height=80,
            key=f"description_input_{selected_project}",
        )

        meta_cols = st.columns(3)
        with meta_cols[0]:
            status_options = project_config.PROJECT_STATUSES
            current_status = cfg.get("status")
            status_index = status_options.index(current_status) if current_status in status_options else 0
            status_input = st.selectbox(
                "Статус проекта", status_options, index=status_index, key=f"status_input_{selected_project}"
            )
        with meta_cols[1]:
            priority_options = project_config.PROJECT_PRIORITIES
            current_priority = cfg.get("priority")
            priority_index = priority_options.index(current_priority) if current_priority in priority_options else 0
            priority_input = st.selectbox(
                "Приоритет проекта", priority_options, index=priority_index, key=f"priority_input_{selected_project}"
            )
        with meta_cols[2]:
            progress_input = st.number_input(
                "Прогресс (%)",
                min_value=0,
                max_value=100,
                value=int(cfg.get("progress") or 0),
                step=5,
                key=f"progress_input_{selected_project}",
            )

        owner_cols = st.columns(3)
        with owner_cols[0]:
            sprint_input = st.text_input(
                "Текущий спринт", value=cfg.get("current_sprint") or "", key=f"sprint_input_{selected_project}"
            )
        with owner_cols[1]:
            milestone_input = st.text_input(
                "Текущая веха", value=cfg.get("current_milestone") or "", key=f"milestone_input_{selected_project}"
            )
        with owner_cols[2]:
            owner_input = st.text_input(
                "Владелец проекта", value=cfg.get("owner") or "", key=f"owner_input_{selected_project}"
            )

        if st.button(
            "Сохранить настройки проекта", key=f"save_project_settings_{selected_project}", icon=":material/save:"
        ):
            candidate = dict(cfg)
            candidate.update(
                {
                    "default_workspace_path": workspace_input.strip() or None,
                    "default_branch": branch_input.strip() or None,
                    "default_executor": None if executor_input == "(не задан)" else executor_input,
                    "default_prompt": prompt_input.strip(),
                    "description": description_input.strip(),
                    "status": status_input,
                    "priority": priority_input,
                    "progress": int(progress_input),
                    "current_sprint": sprint_input.strip() or None,
                    "current_milestone": milestone_input.strip() or None,
                    "owner": owner_input.strip(),
                }
            )
            settings_warnings = list(project_config.validate_project_settings(candidate))

            project_config.save_project_settings(
                selected_project,
                default_workspace_path=candidate["default_workspace_path"],
                default_branch=candidate["default_branch"],
                default_executor=candidate["default_executor"],
                default_prompt=candidate["default_prompt"],
                description=candidate["description"],
                status=candidate["status"],
                priority=candidate["priority"],
                progress=candidate["progress"],
                current_sprint=candidate["current_sprint"],
                current_milestone=candidate["current_milestone"],
                owner=candidate["owner"],
            )
            # Flashed (not rendered inline) because the `st.rerun()` below
            # wipes this frame before the messages would be seen — the pop
            # above the settings form shows them post-rerun. Warnings stay
            # advisory: the save above already happened regardless.
            st.session_state[_PROJECT_SETTINGS_FLASH_KEY] = {
                "warnings": settings_warnings,
                "success": "Настройки проекта сохранены.",
            }
            st.rerun()

    with tab_chat:
        render_project_chat(selected_project, tasks, tasks_by_id)

    with tab_audit:
        st.caption(
            "Read-only аудит проекта (архитектура, правила, качество, UX). Результат "
            "превращается в предлагаемые задачи бэклога — примите нужные."
        )
        if st.button("Запустить аудит", key=f"proj_audit_run_{selected_project}", type="primary"):
            audit_task = create_task(
                selected_project,
                f"Аудит проекта {selected_project}: архитектура/правила/качество/UX",
                "architecture_review",
                "Next",
                goal="Провести read-only аудит проекта и предложить задачи для бэклога.",
                prompt=_project_audit_prompt(selected_project),
            )
            execution_queue.enqueue_and_persist(ROOT, audit_task, {**tasks_by_id, audit_task["id"]: audit_task})
            st.success(
                f"Аудит поставлен в очередь (задача {audit_task['id'][:8]}). Когда read-only "
                "агент завершит отчёт, его предложения появятся ниже."
            )

        report_text = _latest_audit_report_text(selected_project)
        if report_text:
            st.divider()
            candidates = backlog_proposals.parse_candidate_tasks(report_text)
            backlog_proposals.render_candidate_tasks(
                candidates,
                ROOT,
                selected_project,
                key_prefix=f"proj_audit_cand_{selected_project}",
                heading="Предложения из аудита",
            )
        else:
            st.caption("Отчётов аудита пока нет — запустите аудит выше.")

    with tab_roadmap:
        st.caption(
            "Переформатировать Roadmap проекта по новым пожеланиям: агент пересоберёт "
            "задачи/вехи/волны, покажет предпросмотр. Дубли уже сделанного отфильтровываются."
        )
        roadmap_wishes = st.text_area(
            "Пожелания к Roadmap",
            key=f"proj_roadmap_wishes_{selected_project}",
            placeholder="Например: сфокусироваться на надёжности пайплайна и качестве UX",
        )
        if st.button("Переформатировать Roadmap", key=f"proj_roadmap_run_{selected_project}", type="primary"):
            roadmap_task = create_task(
                selected_project,
                f"Переформатировать Roadmap: {selected_project}",
                "architecture_review",
                "Next",
                goal="Пересобрать roadmap/задачи/вехи проекта по новым пожеланиям.",
                prompt=_roadmap_reformat_prompt(selected_project, roadmap_wishes),
            )
            execution_queue.enqueue_and_persist(
                ROOT, roadmap_task, {**tasks_by_id, roadmap_task["id"]: roadmap_task}
            )
            st.success(
                f"Переформатирование поставлено в очередь (задача {roadmap_task['id'][:8]}). "
                "Когда агент завершит, новые задачи появятся ниже как предложения."
            )

        roadmap_report = _latest_roadmap_report_text(selected_project)
        if roadmap_report:
            st.divider()
            proposed = backlog_proposals.parse_candidate_tasks(roadmap_report)
            fresh = backlog_proposals.filter_new_candidates(proposed, tasks)
            if len(fresh) < len(proposed):
                st.caption(f"Отфильтровано дублей уже существующих задач: {len(proposed) - len(fresh)}.")
            backlog_proposals.render_candidate_tasks(
                fresh,
                ROOT,
                selected_project,
                key_prefix=f"proj_roadmap_cand_{selected_project}",
                heading="Новые задачи из обновлённого roadmap",
            )
        else:
            st.caption("Отчётов переформатирования пока нет — запустите выше.")


# --------------------------------------------------------------------------
# Generated tasks browser (global)
# --------------------------------------------------------------------------

elif page_key == "integration":
    # Integration Center (AICC-INT-001): registry list + health badges +
    # per-project drill-down. Read-only — see docs/INTEGRATION_CENTER.md.
    integration_center.render_integration_center(
        tasks,
        runs=runs_read.list_unified_runs(
            get_execution_center_api().db_path, root=ROOT, limit=200
        ),
    )

elif page_key == "generated":
    st.subheader("Сгенерированные задачи", anchor="generated-tasks")

    project_filter = st.selectbox("Фильтр по проекту", ["Все"] + models.PROJECT_IDS, key="gen_filter")

    all_files = artifacts.list_markdown_files(GENERATED_DIR)
    filtered_files = (
        all_files
        if project_filter == "Все"
        else [path for path in all_files if artifacts.project_from_path(path, GENERATED_DIR) == project_filter]
    )

    if not filtered_files:
        st.info("Файлы заданий не найдены.")
    else:
        st.caption(f"Найдено файлов: {len(filtered_files)} (новые сверху)")
        for path in filtered_files:
            rel = path.relative_to(GENERATED_DIR)
            file_project = artifacts.project_from_path(path, GENERATED_DIR)
            with st.expander(f"{rel} · {format_mtime(path)}"):
                content = read_text(path)
                st.markdown(content)
                if file_project != "—":
                    st.divider()
                    render_agent_launcher(
                        key_prefix=f"gen_page_launch_{rel}".replace("/", "_"),
                        project=file_project,
                        default_prompt=content,
                        tasks=tasks,
                        default_task_type=artifacts.infer_task_type_from_filename(path) or "implementation",
                    )


# --------------------------------------------------------------------------
# Reports browser (global)
# --------------------------------------------------------------------------

elif page_key == "reports":
    st.subheader("Отчёты", anchor="reports")

    project_filter = st.selectbox("Фильтр по проекту", ["Все"] + models.PROJECT_IDS, key="report_filter")

    all_files = artifacts.list_markdown_files(REPORTS_DIR)
    filtered_files = (
        all_files
        if project_filter == "Все"
        else [path for path in all_files if artifacts.project_from_path(path, REPORTS_DIR) == project_filter]
    )

    if not filtered_files:
        st.info("Файлы отчётов не найдены.")
    else:
        st.caption(f"Найдено файлов: {len(filtered_files)} (новые сверху)")
        # Unified runs (v2 runtime.db + legacy v1.2) so a report file is joined
        # to its run regardless of which source produced it; the old
        # `agent_runner.load_runs()` only knew about v1.2 runs.
        runs_by_report_path = {
            run["report_path"]: run
            for run in runs_read.list_unified_runs(get_execution_center_api().db_path, root=ROOT)
            if run.get("report_path")
        }
        for path in filtered_files:
            rel = path.relative_to(REPORTS_DIR)
            matching_run = runs_by_report_path.get(f"reports/{rel}")
            with st.expander(f"{rel} · {format_mtime(path)}"):
                st.markdown(read_text(path))
                if matching_run:
                    st.divider()
                    parsed = report_parser.apply_manual_corrections(matching_run.get("parsed") or {})
                    st.markdown("**Извлечённые данные**")
                    st.write(f"Вердикт: {models.VERDICT_LABELS.get(parsed.get('verdict'), parsed.get('verdict') or 'не определён')}")
                    st.write(f"Уверенность парсера: {parsed.get('confidence', 'none')}")
                    counts = report_parser.severity_counts(matching_run.get("parsed"))
                    if any(counts.values()):
                        st.caption("Находки: " + " · ".join(f"{sev}: {counts[sev]}" for sev in models.SEVERITIES if counts[sev]))
                    render_create_next_task_widget(matching_run, tasks, key_prefix=f"reports_page_{matching_run['id']}")


# --------------------------------------------------------------------------
# Global context
# --------------------------------------------------------------------------

elif page_key == "context":
    st.subheader("Глобальный контекст", anchor="context")

    for name in GLOBAL_FILES:
        path = ROOT / name
        with st.expander(f"{name} · {format_mtime(path)}", expanded=(name == "CURRENT_STATE.md")):
            st.markdown(read_text(path))


# --------------------------------------------------------------------------
# Git Center
# --------------------------------------------------------------------------

elif page_key == "git_center":
    st.subheader("Git Center", anchor="git-center")

    # Multi-repo: the portfolio spans several configured repositories, not just
    # the app's own cwd. Surface every project's configured repository_path
    # (plus the app itself) so an operator can inspect any of them from one
    # place instead of only ever seeing AICC here.
    repos: list[tuple[str, Path]] = []
    # A linked worktree stores ``.git`` as a file, not a directory.
    if git_info.get_status(ROOT).get("is_repo"):
        repos.append(("AICC (app)", ROOT))
    for pid in models.PROJECT_IDS:
        cfg = project_configs.get(pid, {})
        repo_str = cfg.get("repository_path") or cfg.get("default_workspace_path")
        if not repo_str:
            continue
        repo_path = Path(repo_str).expanduser()
        if repo_path.is_dir() and repo_path not in [p for _, p in repos]:
            label = f"{cfg.get('display_name') or pid} ({pid})"
            repos.append((label, repo_path))

    if not repos:
        st.info("Не найдено ни одного настроенного git-репозитория.")
    else:
        # Per-repo summary table — one glance at the whole portfolio's git state.
        summary_rows = []
        for label, repo_path in repos:
            st_row = git_info.get_status(repo_path)
            if not st_row.get("is_repo"):
                summary_rows.append({"Проект": label, "Ветка": "—", "Статус": "не репозиторий",
                                     "Изменено": "—", "Неотслеж.": "—", "Коммит": "—"})
            else:
                summary_rows.append({
                    "Проект": label,
                    "Ветка": st_row.get("branch", "—"),
                    "Статус": "Изменения есть" if st_row.get("dirty") else "Чисто",
                    "Изменено": st_row.get("modified_count", 0),
                    "Неотслеж.": st_row.get("untracked_count", 0),
                    "Коммит": f"{st_row.get('last_commit_hash', '—')} {st_row.get('last_commit_subject', '')[:40]}",
                })
        st.dataframe(summary_rows, use_container_width=True, hide_index=True)

        st.divider()
        repo_label = st.selectbox("Репозиторий для детального просмотра",
                                  [label for label, _ in repos], key="git_center_repo_select")
        repo_path = next(p for lbl, p in repos if lbl == repo_label)
        repo_status = git_info.get_status(repo_path)

        if not repo_status.get("is_repo"):
            st.info(f"«{repo_label}» не является git-репозиторием.")
        else:
            with st.container(horizontal=True):
                st.metric("Ветка", repo_status["branch"], border=True)
                st.metric("Статус", "Изменения есть" if repo_status["dirty"] else "Чисто", border=True)
                st.metric("Изменено файлов", repo_status["modified_count"], border=True)
                st.metric("Неотслеживаемых файлов", repo_status["untracked_count"], border=True)

            st.caption(f"Корень репозитория: `{repo_status['root']}`")
            st.caption(f"Последний коммит: `{repo_status['last_commit_hash']}` — {repo_status['last_commit_subject']}")

            refresh_key = f"git_center_remote_refreshed::{repo_path.resolve()}"
            if st.button(
                "Обновить",
                key=f"git_center_refresh_{repo_label}",
                icon=":material/refresh:",
                help="Выполнить git fetch и обновить сравнение с tracking-веткой.",
            ):
                with st.spinner("Обновляем данные remote…"):
                    fetch_ok, fetch_error = git_info.fetch_remotes(repo_path)
                if fetch_ok:
                    st.session_state[refresh_key] = time.time()
                    st.success("Данные remote обновлены.")
                else:
                    st.error(f"Не удалось обновить remote: {fetch_error}")

            refreshed_at = st.session_state.get(refresh_key)
            if isinstance(refreshed_at, (int, float)):
                age_minutes = max(0, int((time.time() - refreshed_at) // 60))
                st.caption(f"Данные remote: обновлено {age_minutes} мин. назад")
                divergence = git_info.get_ahead_behind(repo_path)
                if divergence.get("available"):
                    with st.container(horizontal=True):
                        st.metric("Ahead", divergence["ahead"], border=True)
                        st.metric("Behind", divergence["behind"], border=True)
                    st.caption(f"Сравнение: `{repo_status['branch']}` ↔ `{divergence['upstream']}`")
                else:
                    st.warning(str(divergence.get("error") or "Расхождение с remote недоступно."))
            else:
                st.caption("Данные remote ещё не обновлялись. Нажмите «Обновить», чтобы выполнить git fetch.")

            tab_files, tab_log, tab_diff, tab_branches, tab_remotes = st.tabs(
                ["Изменённые файлы", "История коммитов", "Diff", "Ветки", "Remotes"]
            )

            with tab_files:
                status_lines = repo_status.get("status_lines", [])
                if not status_lines:
                    st.success("Нет изменений — рабочее дерево чистое.")
                else:
                    for line in status_lines:
                        st.caption(f"`{line[:2]}`  {line[3:]}")

            with tab_log:
                commits = git_info.get_log(repo_path, 20)
                if not commits:
                    st.info("История коммитов недоступна.")
                else:
                    for commit in commits:
                        with st.container(border=True):
                            st.markdown(f"**{commit['subject']}**")
                            st.caption(f"`{commit['hash']}` · {commit['author']} · {commit['date']}")

            with tab_diff:
                st.markdown("**Незафиксированные изменения (unstaged)**")
                st.code(git_info.get_diff_stat(repo_path, staged=False) or "Нет изменений.", language=None)
                st.markdown("**Подготовленные изменения (staged)**")
                st.code(git_info.get_diff_stat(repo_path, staged=True) or "Нет изменений.", language=None)

            with tab_branches:
                branches = git_info.get_branches(repo_path)
                if not branches:
                    st.info("Ветки не найдены.")
                else:
                    for branch in branches:
                        marker = "→ " if branch == repo_status["branch"] else "  "
                        st.caption(f"{marker}{branch}")

            with tab_remotes:
                remotes = git_info.get_remotes(repo_path)
                if not remotes:
                    st.info("Удалённые репозитории не настроены.")
                else:
                    for name, url in remotes:
                        st.caption(f"**{name}** — {url}")


# --------------------------------------------------------------------------
# Workspace Launcher
# --------------------------------------------------------------------------

elif page_key == "workspace":
    st.subheader("Workspace Launcher", anchor="workspace-launcher")
    st.caption("Быстрый переход к рабочим пространствам проектов и обзор git worktree.")

    st.markdown("#### Git worktrees")
    repo_status = get_git_status()
    if not repo_status.get("is_repo"):
        st.info("Текущая директория не является git-репозиторием.")
    else:
        worktrees = get_git_worktrees()
        if not worktrees:
            st.info("Информация о worktree недоступна.")
        else:
            for worktree in worktrees:
                with st.container(border=True):
                    st.markdown(f"**{worktree.get('branch', '—')}**")
                    st.caption(f"HEAD: `{worktree.get('head', '—')}`")
                    st.code(worktree.get("path", "—"), language=None)

    st.divider()
    st.markdown("#### Быстрый переход по проектам")

    for project in models.PROJECT_IDS:
        project_file = project_status_file_path(project)
        context_name = CONTEXT_FILES.get(project)
        project_tasks = [
            task
            for task in tasks
            if project_config.project_matches(task.get("project"), project)
        ]
        project_counts = read_model.task_snapshot(project_tasks)
        project_generated = artifacts.list_markdown_files(GENERATED_DIR / project)
        last_activity = format_mtime(project_generated[0]) if project_generated else "—"

        with st.container(border=True):
            header_cols = st.columns([3, 1, 1])
            header_cols[0].markdown(f"**{project}**")
            header_cols[1].metric("Активные", project_counts.active)
            header_cols[2].caption(f"Активность: {last_activity}")

            st.code(str(project_file), language=None)
            if context_name:
                st.code(str(CONTEXT_DIR / context_name), language=None)
            st.caption(f"generated/{project} · reports/{project}")

            btn_cols = st.columns(2)
            with btn_cols[0]:
                if st.button(
                    "Открыть проект",
                    key=f"launch_open_{project}",
                    icon=":material/folder_open:",
                    width="stretch",
                ):
                    st.session_state.pending_nav = "projects"
                    st.session_state.pending_project_browser = project
                    st.rerun()
            with btn_cols[1]:
                if st.button(
                    "Новая задача",
                    key=f"launch_new_{project}",
                    icon=":material/add_task:",
                    width="stretch",
                ):
                    st.session_state.pending_nav = "create"
                    st.session_state.pending_create_project = project
                    st.rerun()


# --------------------------------------------------------------------------
# Focus Mode
# --------------------------------------------------------------------------

elif page_key == "focus":
    if st.button("Выйти из Focus Mode", icon=":material/close:"):
        st.session_state.pending_nav = "dashboard"
        st.rerun()

    st.subheader("Focus Mode", anchor="focus")

    active_tasks = [task for task in tasks if task.get("status") != "Done"]

    if not active_tasks:
        st.info("Нет активных задач для фокуса. Создайте задачу или откройте Kanban.")
    else:
        project_filter = st.selectbox("Проект", ["Все"] + models.PROJECT_IDS, key="focus_project_filter")
        candidates = [
            task
            for task in active_tasks
            if project_filter == "Все" or project_config.project_matches(task.get("project"), project_filter)
        ]

        if not candidates:
            st.info("Нет активных задач для выбранного проекта.")
        else:
            default_index = next(
                (i for i, task in enumerate(candidates) if task.get("status") == "In Progress"), 0
            )
            labels = [task_label(task) for task in candidates]
            chosen_index = st.selectbox(
                "Задача в фокусе",
                options=list(range(len(candidates))),
                format_func=lambda i: labels[i],
                index=default_index,
                key="focus_task_select",
            )
            task = candidates[chosen_index]
            task_id = task["id"]

            with st.container(border=True):
                st.markdown(f"## {task.get('title', 'Без названия')}")
                st.caption(f"{task.get('project')} · {TASK_TYPE_LABELS.get(task.get('task_type', ''), task.get('task_type'))}")

                task_progress = int(task.get("progress") or 0)
                task_stage = task.get("current_stage") or models.EXECUTION_STAGES[0]
                st.progress(task_progress / 100, text=f"{task_stage} — {task_progress}%")

                with st.container(horizontal=True):
                    priority = task.get("priority", "Medium")
                    st.badge(priority, color=PRIORITY_COLORS.get(priority, "blue"))
                    if task.get("owner"):
                        st.badge(task["owner"], color="gray", icon=":material/person:")
                    if task.get("estimate_hours"):
                        st.badge(format_estimate(task["estimate_hours"]), color="gray", icon=":material/schedule:")

                unmet = unmet_dependencies(task, tasks_by_id)
                if unmet:
                    names = ", ".join(
                        tasks_by_id[dep_id].get("title", "?")[:40] if dep_id in tasks_by_id else dep_id
                        for dep_id in unmet
                    )
                    st.warning(f"Заблокировано: {names}")

                st.divider()

                _focus_status = task.get("status", "Backlog")
                _focus_status_options = task_view.kanban_status_options(_focus_status)
                new_status = st.selectbox(
                    "Статус",
                    _focus_status_options,
                    # Preserve a legacy/unknown value while viewing the page;
                    # selecting another option is the explicit migration.
                    index=_focus_status_options.index(_focus_status),
                    key=f"focus_status_{task_id}",
                )
                if new_status != task.get("status"):
                    update_task_status(task_id, new_status)
                    st.rerun()

                st.caption(
                    "Done устанавливается автоматически после проверки результата "
                    "и целевой ветки."
                )


# --------------------------------------------------------------------------
# Portfolio Execution
# --------------------------------------------------------------------------

elif page_key == "portfolio":
    st.subheader("Портфель", anchor="portfolio")
    portfolio_section = st.segmented_control(
        "Раздел",
        ["Обзор", "Исполнение"],
        default="Исполнение",
        key="portfolio_section",
    )
    if portfolio_section == "Исполнение":
        portfolio_panel.render_portfolio_execution_panel(
            root=ROOT,
            execution_center_api=get_execution_center_api(),
        )
    else:
        portfolio_overview_panel.render_portfolio_overview_panel(root=ROOT)

elif page_key == "portfolio_overview":
    st.caption(
        "Обзор портфеля объединён со страницей «Портфель»; этот адрес сохранён для старых закладок."
    )
    portfolio_overview_panel.render_portfolio_overview_panel(root=ROOT)
