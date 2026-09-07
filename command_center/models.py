"""Shared constants and record factories for the v1.2 agent workflow.

Follows the same convention as the existing `app.py` task model: plain dicts
(JSON-serializable as-is) plus small `new_*`/`normalize_*` factory functions,
not dataclasses/ORM models. This keeps every record trivially compatible with
`command_center.storage` and with Streamlit's session state.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

# --------------------------------------------------------------------------
# Projects
# --------------------------------------------------------------------------

PROJECT_IDS: list[str] = ["AICC", "AIOS", "AICOS", "PRODUCT", "ECOSYSTEM", "ESF", "AML", "BANK", "LEGAL", "BUSINESS", "PERSONAL"]
SENSITIVE_PROJECT_IDS: set[str] = {"BANK", "LEGAL"}

# Non-canonical project labels that historically reached data/tasks.json from
# outside the app (hand edits, external imports) and are NOT valid ids. Mapped
# to their canonical PROJECT_IDS entry so `tasks_repository.validate_tasks` can
# say "did you mean …?" and `tasks_repository.reconcile_project_aliases` can
# migrate the live store. An unrecognised label with no entry here is left
# alone and surfaced as an integrity issue, never silently guessed (audit
# BLOCKER-4).
PROJECT_ALIASES: dict[str, str] = {
    "AI Command Center": "AICC",
    "Ecosystem": "ECOSYSTEM",
    "AIOS Product": "PRODUCT",
}

# --------------------------------------------------------------------------
# Kanban status / task priority (single source of truth)
#
# Previously hand-duplicated as `app.py`'s own `KANBAN_COLUMNS`/`PRIORITIES`
# module-level lists — moved here so `command_center.task_import` (which must
# never import `app.py`) can validate an imported task's `status`/`priority`
# against the same vocabulary the Kanban board and Create Task form use.
# `app.py` now binds its existing names to these, so every pre-existing call
# site (`KANBAN_COLUMNS`, `PRIORITIES`) is unchanged.
# --------------------------------------------------------------------------

KANBAN_STATUSES: list[str] = ["Backlog", "Next", "In Progress", "Review", "Done"]

TASK_PRIORITIES: list[str] = ["Low", "Medium", "High", "Critical"]

# --------------------------------------------------------------------------
# Workflow stages (parallel to, not a replacement for, the Kanban status)
# --------------------------------------------------------------------------

WORKFLOW_STAGES: list[str] = [
    "Draft",
    "Ready",
    "Running",
    "Remediation",
    "Final Review",
    "Approved",
    "Commit Pending",
    "Push Pending",
    "PR Pending",
    "Done",
]

WORKFLOW_STAGE_LABELS: dict[str, str] = {
    "Draft": "Черновик",
    "Ready": "Готово к запуску",
    "Running": "Выполняется",
    "Remediation": "Исправление",
    "Final Review": "Финальная проверка",
    "Approved": "Одобрено",
    "Commit Pending": "Ожидает commit",
    "Push Pending": "Ожидает push",
    "PR Pending": "Ожидает Pull Request",
    "Done": "Готово",
}

# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------

RUN_STATUSES: list[str] = ["queued", "running", "completed", "failed", "timed_out", "cancelled"]

RUN_STATUS_LABELS: dict[str, str] = {
    "queued": "В очереди",
    "running": "Выполняется",
    "completed": "Завершено",
    "failed": "Ошибка",
    "timed_out": "Истекло время ожидания",
    "cancelled": "Отменено",
}

RUN_STATUS_COLORS: dict[str, str] = {
    "queued": "gray",
    "running": "blue",
    "completed": "green",
    "failed": "red",
    "timed_out": "orange",
    "cancelled": "gray",
}

# --------------------------------------------------------------------------
# Parsed report verdicts
# --------------------------------------------------------------------------

VERDICT_APPROVED_FOR_COMMIT = "APPROVED_FOR_COMMIT"
VERDICT_NOT_APPROVED_FOR_COMMIT = "NOT_APPROVED_FOR_COMMIT"
VERDICT_READY_FOR_FINAL_REVIEW = "READY_FOR_FINAL_REVIEW"
VERDICT_NOT_READY_FOR_FINAL_REVIEW = "NOT_READY_FOR_FINAL_REVIEW"
VERDICT_READY_FOR_COMMIT = "READY_FOR_COMMIT"
VERDICT_FAILED = "FAILED"

VERDICT_LABELS: dict[str, str] = {
    VERDICT_APPROVED_FOR_COMMIT: "Одобрено для commit",
    VERDICT_NOT_APPROVED_FOR_COMMIT: "Не одобрено для commit",
    VERDICT_READY_FOR_FINAL_REVIEW: "Готово к финальной проверке",
    VERDICT_NOT_READY_FOR_FINAL_REVIEW: "Не готово к финальной проверке",
    VERDICT_READY_FOR_COMMIT: "Готово к commit",
    VERDICT_FAILED: "Ошибка",
}

PASSING_VERDICTS: tuple[str, ...] = (
    VERDICT_APPROVED_FOR_COMMIT,
    VERDICT_READY_FOR_COMMIT,
    VERDICT_READY_FOR_FINAL_REVIEW,
)


def is_passing_verdict(verdict: str | None) -> bool:
    """Single source of truth for "is this verdict good enough" — reused by
    the Task Card's verdict badge color and by `launch_service`'s
    stage-advancement logic, so the two never drift out of sync."""
    return verdict in PASSING_VERDICTS


SEVERITIES: list[str] = ["Blocker", "High", "Medium", "Low"]


def utc_now() -> datetime:
    """The naive `datetime` `iso_now()` renders — UTC, with `tzinfo` stripped.

    Shared so every caller that needs a fresh reference point for age/staleness
    math or a cutoff comparable against `iso_now()` output (retention cutoffs,
    orphan-timeout checks, digest windows, completion backoff) reads the same
    clock `iso_now()` writes, rather than a second, independently-drifting copy
    of "what does UTC-naive-now mean here" (see `iso_now`)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_now() -> str:
    """Naive UTC time, second precision, no timezone offset.

    Was naive *local* time through v1.2 (`datetime.now().isoformat(...)`,
    matching `app.py`'s pre-existing v1.1 `new_task_record` convention) — kept
    naive rather than migrated to an aware/offset format, to avoid a
    mixed-format backward-compatibility hazard against existing
    `data/tasks.json` records and every naive-string DB column this convention
    already reaches.

    That local-time choice was itself the defect this now fixes
    (`VOYN-W0-AICC-ISO-NOW-NAIVE-LOCAL`): local wall-clock time is not
    monotonic. On a DST fall-back transition the clock repeats an hour, so a
    call made strictly *later* in real time can render a strictly *smaller*
    string than an earlier call in the same process — and every `ORDER BY
    created_at DESC` (or bare string comparison) in this app silently returns
    the wrong row for the span of the repeated hour. Higher precision does not
    help: the values genuinely repeat, they are not merely tied. UTC has no
    daylight-saving transitions, so sourcing from it removes the non-monotonic
    span entirely while leaving the string's shape — naive, second precision,
    no offset — exactly as every existing parser, comparison and on-disk
    record already expects.

    A database (or process) that predates this fix mixes local-time rows
    written before the upgrade with UTC rows written after: the two epochs are
    indistinguishable by format, and that boundary is the naive convention's
    own residual limit, not something a comparison at read time can recover —
    tracked, together with the fully timezone-aware format that would close it
    for good, as `VOYN-W0-AICC-TZ-AWARE-TIMESTAMPS`.
    """
    return utc_now().isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


# --------------------------------------------------------------------------
# Task workflow-field extension (backward compatible with v1.1 tasks.json)
# --------------------------------------------------------------------------


def default_task_workflow_fields() -> dict:
    return {
        "parent_task_id": None,
        "prior_run_id": None,
        "current_run_id": None,
        "workflow_stage": "Draft",
        "latest_verdict": None,
        "report_path": None,
        "repository_path": None,
        "branch": None,
        "agent": None,
        "last_run_at": None,
    }


def normalize_task_workflow(task: dict) -> dict:
    for key, default in default_task_workflow_fields().items():
        task.setdefault(key, default)
    return task


# --------------------------------------------------------------------------
# Task execution model v2 (backward compatible with v1.1/v1.2 tasks.json)
#
# Adds Progress/Current Stage/Launch/Timeline/Prompt-history to a task
# without renaming or removing any pre-existing field. The one semantic
# shift is `title`: pre-v2 records used `title` to hold the objective text
# that is now split into its own `goal` field — `normalize_task_execution`
# migrates old records by copying that text into `goal` and deriving a
# short `title` from it, so `title` is always present and always short.
# --------------------------------------------------------------------------

EXECUTION_STAGES: list[str] = [
    "Created",
    "Workspace Verified",
    "Architecture Review",
    "Implementation",
    "Coding Complete",
    "Validation",
    "Tests Passed",
    "PR Ready",
    "Completed Locally",
    "Merged",
]

STAGE_PROGRESS: dict[str, int] = {
    "Created": 0,
    "Workspace Verified": 5,
    "Architecture Review": 20,
    "Implementation": 40,
    "Coding Complete": 60,
    "Validation": 75,
    "Tests Passed": 90,
    "PR Ready": 95,
    "Completed Locally": 100,
    "Merged": 100,
}

LAUNCH_STATUSES: list[str] = [
    "Ready",
    "Launching",
    "Running",
    "Blocked",
    "Incomplete",
    "Needs Review",
    "Completed",
    "Failed",
    # Operator-stopped run — distinct from "Failed" so a deliberate stop is
    # never rendered as a defect; the task itself retires to the read-side
    # "Closed" lane (AICC-IMP-006, see `runtime.task_sync`).
    "Cancelled",
    "Requires Attention",
]

TIMELINE_EVENT_TYPES: list[str] = [
    "task_created",
    "workspace_opened",
    "executor_started",
    "architecture_review_finished",
    "implementation_finished",
    "validation_started",
    "tests_passed",
    "pr_created",
    "review_started",
    "merged",
    "completed",
    "launch_requires_attention",
]


def truncate_text(text: str, max_len: int) -> str:
    """Return text bounded to ``max_len``, using one ellipsis when truncated."""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    if max_len == 1:
        return "…"
    return text[: max_len - 1] + "…"


def format_duration(seconds: int | float | None) -> str:
    """Compact human duration for card-sized labels: ``45s``, ``3m 20s``,
    ``2h 05m``, ``1d 4h``. ``None``/negative return ``"—"`` — same defensive
    guard as `session_view.format_elapsed`, which stays the format for
    ticking ``HH:MM:SS`` timers."""
    if seconds is None or seconds < 0:
        return "—"
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h"


def format_age(iso_str: str | None, *, now: datetime | None = None) -> str:
    """Age of an `iso_now()`-style timestamp as a compact duration. Timestamps
    in this app are naive UTC (see `iso_now`), so an aware input is demoted to
    naive rather than converted. Missing/unparseable → ``"—"``."""
    if not iso_str:
        return "—"
    try:
        then = datetime.fromisoformat(iso_str)
    except (ValueError, TypeError):
        return "—"
    if then.tzinfo is not None:
        then = then.replace(tzinfo=None)
    reference = now if now is not None else utc_now()
    return format_duration((reference - then).total_seconds())


def derive_short_title(text: str, limit: int = 80) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    truncated = collapsed[:limit].rsplit(" ", 1)[0].strip() or collapsed[:limit]
    return f"{truncated}…"


def default_task_execution_fields() -> dict:
    return {
        "goal": None,
        "prompt": "",
        "prompt_history": [],
        "prompt_version": 0,
        "notes": "",
        "executor": None,
        "workspace_path": None,
        "pull_request_url": None,
        "pull_request_status": None,
        "progress": 0,
        "progress_mode": "auto",
        "progress_updated_at": None,
        "current_stage": EXECUTION_STAGES[0],
        "started_at": None,
        "finished_at": None,
        "timeline": [],
        "launch_status": "Ready",
        "launch_history": [],
    }


def normalize_task_execution(task: dict) -> dict:
    if not (task.get("goal") or "").strip():
        legacy_title = (task.get("title") or "").strip()
        task["goal"] = legacy_title
        if legacy_title:
            task["title"] = derive_short_title(legacy_title)

    for key, default in default_task_execution_fields().items():
        task.setdefault(key, default)

    if not task.get("workspace_path"):
        task["workspace_path"] = task.get("repository_path")
    if not task.get("executor"):
        task["executor"] = task.get("agent")
    if not task.get("title"):
        task["title"] = derive_short_title(task.get("goal") or "") or "Untitled task"

    return task


def set_current_stage(task: dict, stage: str, *, mode: str = "auto") -> dict:
    """Advance `current_stage`/`progress`. A manual override (`mode="manual"`)
    wins over subsequent automatic-advancement calls until the user resets
    it back to auto via `reset_progress_to_auto`."""
    if stage not in STAGE_PROGRESS:
        raise ValueError(f"Unknown execution stage: {stage!r}")
    if mode == "auto" and task.get("progress_mode") == "manual":
        return task
    task["current_stage"] = stage
    task["progress"] = STAGE_PROGRESS[stage]
    task["progress_mode"] = mode
    task["progress_updated_at"] = iso_now()
    return task


def reset_progress_to_auto(task: dict) -> dict:
    task["progress_mode"] = "auto"
    stage = task.get("current_stage") or EXECUTION_STAGES[0]
    task["progress"] = STAGE_PROGRESS.get(stage, 0)
    task["progress_updated_at"] = iso_now()
    return task


def new_timeline_event(event_type: str, message: str = "") -> dict:
    return {
        "id": new_id(),
        "ts": iso_now(),
        "type": event_type,
        "message": (message or "")[:500],
    }


# Upper bound on a task's UI timeline. `tasks.json` is JSON-parsed on every
# Streamlit rerun (and by five 2-5s fragment pollers); an unbounded timeline
# (measured up to 6,067 events on one task) bloated the file to ~7 MB / ~18 ms
# per parse — 151% of it was timeline. Bounding to the most-recent N events keeps
# the store small and the parse fast. The authoritative, complete run history
# lives in `runtime.db` `run_event`; this only caps the task's own display log.
MAX_TIMELINE_EVENTS = 100


def trim_timeline(task: dict) -> dict:
    """Keep only the most-recent `MAX_TIMELINE_EVENTS` timeline entries. Mutates
    and returns `task`. A no-op when the timeline is already within bound or
    absent."""
    timeline = task.get("timeline")
    if isinstance(timeline, list) and len(timeline) > MAX_TIMELINE_EVENTS:
        task["timeline"] = timeline[-MAX_TIMELINE_EVENTS:]
    return task


def append_timeline_event(task: dict, event_type: str, message: str = "") -> dict:
    task.setdefault("timeline", [])
    task["timeline"].append(new_timeline_event(event_type, message))
    trim_timeline(task)
    return task


def new_launch_attempt(
    *,
    executor: str | None,
    branch: str | None,
    status: str,
    warnings: list[str] | None = None,
    workspace_path: str | None = None,
) -> dict:
    return {
        "id": new_id(),
        "ts": iso_now(),
        "executor": executor,
        "branch": branch,
        # The actual resolved launch path (task workspace_path / project
        # default_workspace_path / repository_path fallback) this attempt
        # ran against — see `command_center.launch.resolve_workspace_path`.
        # `None` for entries recorded before this field existed; no
        # backfill needed, callers just treat a missing key as unknown.
        "workspace_path": workspace_path,
        "status": status,
        "warnings": warnings or [],
    }


def append_launch_attempt(
    task: dict,
    *,
    executor: str | None,
    branch: str | None,
    status: str,
    warnings: list[str] | None = None,
    workspace_path: str | None = None,
) -> dict:
    task.setdefault("launch_history", [])
    task["launch_history"].append(
        new_launch_attempt(
            executor=executor, branch=branch, status=status, warnings=warnings, workspace_path=workspace_path
        )
    )
    task["launch_status"] = status
    return task


def push_prompt_history(task: dict, new_prompt: str) -> dict:
    """Records the outgoing prompt value (if any) before overwriting it. A
    no-op edit — relaunching with the exact same prompt text, which is the
    common case (the launcher pre-fills the previous prompt by default) —
    does not create a duplicate history entry or bump the version; only a
    genuine change is versioned."""
    task.setdefault("prompt_history", [])
    previous = task.get("prompt") or ""
    if previous.strip() and previous != new_prompt:
        task["prompt_version"] = int(task.get("prompt_version") or 0) + 1
        task["prompt_history"].append(
            {"version": task["prompt_version"], "text": previous, "saved_at": iso_now()}
        )
    task["prompt"] = new_prompt
    return task


def unmet_dependencies(task: dict, tasks_by_id: dict[str, dict]) -> list[str]:
    return [
        dep_id
        for dep_id in task.get("depends_on", [])
        if tasks_by_id.get(dep_id, {}).get("status") != "Done"
    ]


def is_blocked(task: dict, tasks_by_id: dict[str, dict]) -> bool:
    return bool(unmet_dependencies(task, tasks_by_id))


def derive_dependency_edges(task: dict, all_tasks: list[dict]) -> dict:
    """Read-time-only reverse edges: `blocks` (tasks that depend on this one)
    and `children` (tasks whose parent_task_id is this one). Never persisted
    on the task dict, to avoid dual-write drift with `depends_on`/
    `parent_task_id`, which remain the sole source of truth."""
    task_id = task.get("id")
    blocks = [t["id"] for t in all_tasks if task_id in (t.get("depends_on") or []) and t.get("id") != task_id]
    children = [t["id"] for t in all_tasks if t.get("parent_task_id") == task_id and t.get("id") != task_id]
    return {"blocks": blocks, "children": children}


# --------------------------------------------------------------------------
# Project chat
# --------------------------------------------------------------------------


def new_conversation(project: str, title: str, task_id: str | None = None) -> dict:
    now = iso_now()
    return {
        "id": new_id(),
        "project": project,
        "title": title or "Новый разговор",
        "task_id": task_id,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }


def new_message(role: str, content: str, provider: str | None = None) -> dict:
    return {
        "id": new_id(),
        "role": role,
        "content": content,
        "provider": provider,
        "created_at": iso_now(),
    }


# --------------------------------------------------------------------------
# Runs
# --------------------------------------------------------------------------


def new_run_record(
    *,
    project: str,
    task_id: str | None,
    agent: str,
    task_type: str,
    repository_path: str,
    prompt: str,
    timeout_seconds: int,
) -> dict:
    now = iso_now()
    return {
        "id": new_id(),
        "project": project,
        "task_id": task_id,
        "agent": agent,
        "task_type": task_type,
        "repository_path": repository_path,
        "prompt": prompt,
        "timeout_seconds": timeout_seconds,
        "status": "queued",
        "pre_run": {"branch": None, "head": None, "status_summary": None, "is_git_repo": None},
        "post_run": {"branch": None, "head": None, "status_summary": None},
        "started_at": None,
        "completed_at": None,
        "duration_seconds": None,
        "exit_code": None,
        "stdout": "",
        "stderr": "",
        "report_path": None,
        "parsed": None,
        "next_task_id": None,
        "created_at": now,
        "updated_at": now,
    }


# --------------------------------------------------------------------------
# Activity log
# --------------------------------------------------------------------------

ACTIVITY_EVENT_TYPES: list[str] = [
    "conversation_created",
    "message_added",
    "task_created_from_message",
    "run_queued",
    "run_started",
    "run_completed",
    "run_failed",
    "report_saved",
    "verdict_extracted",
    "task_moved_to_remediation",
    "next_task_created",
    "manual_field_correction",
]


def new_activity_event(
    event_type: str,
    *,
    project: str | None = None,
    task_id: str | None = None,
    run_id: str | None = None,
    conversation_id: str | None = None,
    message: str = "",
) -> dict:
    return {
        "id": new_id(),
        "ts": iso_now(),
        "type": event_type,
        "project": project,
        "task_id": task_id,
        "run_id": run_id,
        "conversation_id": conversation_id,
        # Truncated defensively: activity events are a log, not a place to mirror
        # full report/document content (which may be sensitive for BANK/LEGAL).
        "message": (message or "")[:500],
    }
