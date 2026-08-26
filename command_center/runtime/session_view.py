"""Live Execution Center v2 — pure projection layer.

Turns one v2 `run` row (plus optional linked Kanban task, project config, and
latest event) into the canonical execution-session view model the dashboard
renders. No Streamlit import, no database write, no subprocess call beyond
one read-only `git status` against the run's own workspace — safe to unit
test without a live Streamlit script context or a real `runtime.db`.

`derive_status` is a *display*-only mapping. `run["state"]`
(`command_center.runtime.db.RUN_STATES`) remains the sole persisted
execution-state enum — nothing in this module is ever written back to
`runtime.db` or to a Kanban task record. No new persisted status value is
introduced anywhere in this project by this module.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from command_center import capabilities, git_info

# --------------------------------------------------------------------------
# Status vocabulary (display-only — see module docstring)
# --------------------------------------------------------------------------

STATUS_LAUNCHING = "Launching"
STATUS_STARTING = "Starting"
STATUS_RUNNING = "Running"
STATUS_STALE = "Stale"
STATUS_WAITING = "Waiting"
STATUS_REQUIRES_ATTENTION = "Requires Attention"
STATUS_BLOCKED = "Blocked"
STATUS_INCOMPLETE = "Incomplete"
STATUS_CAPABILITY_MISMATCH = "Capability Mismatch"
STATUS_COMPLETED = "Completed"
STATUS_FAILED = "Failed"
STATUS_CANCELLED = "Cancelled"

ACTIVE_DISPLAY_STATUSES: frozenset[str] = frozenset(
    {STATUS_LAUNCHING, STATUS_STARTING, STATUS_RUNNING, STATUS_STALE, STATUS_WAITING}
)

# A live OS process either exists or provably existed a moment ago for every
# one of these — the run is spawned and not terminal. `STATUS_STARTING` (PID
# valid, no output yet) and `STATUS_STALE` (PID valid, liveness probe momentarily
# old) are *warnings about a running process*, never failures: the UI treats
# all three exactly like `STATUS_RUNNING` for cancel/heartbeat affordances, and
# `task_sync` keeps their task non-terminal. This is the concrete guard behind
# "a successfully spawned process must never be recorded Failed just because
# early output / a fresh probe has not arrived yet."
LIVE_PROCESS_DISPLAY_STATUSES: frozenset[str] = frozenset(
    {STATUS_STARTING, STATUS_RUNNING, STATUS_STALE}
)
# `STATUS_BLOCKED`/`STATUS_INCOMPLETE` are terminal too — the process has
# already exited (see `runtime.outcome`'s `EvaluatingResult` stage, which is
# what produces these out of a `FAILED`-state run's `failure_reason`), it
# just did not reach a genuine `COMPLETED` outcome. Included here so
# `task_sync._apply_terminal_fields` still runs for them (report path,
# verdict, etc.) exactly as it does for `FAILED`/`CANCELLED`.
TERMINAL_DISPLAY_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELLED,
        STATUS_BLOCKED,
        STATUS_INCOMPLETE,
        STATUS_CAPABILITY_MISMATCH,
    }
)

_BLOCKED_REASON_PREFIX = "blocked:"
_INCOMPLETE_REASON_PREFIX = "incomplete:"
_CAPABILITY_MISMATCH_REASON_PREFIX = capabilities.FAILURE_REASON_PREFIX

# Default staleness threshold for the heartbeat liveness probe (see
# `get_heartbeat_age`/`is_heartbeat_stale` below).
DEFAULT_HEARTBEAT_STALE_SECONDS = 90.0


def is_awaiting_handshake(run: dict) -> bool:
    """`True` for a run whose process is spawned and RUNNING but has not yet
    produced any output (`first_output_at` is unset) — the "started but early
    output not yet received" window. Used to distinguish `STATUS_STARTING`
    from `STATUS_RUNNING`. Never `True` for a terminal run (that a run
    completed/failed/was cancelled without ever emitting output is decided on
    exit facts, not on this flag)."""
    return run.get("state") == "RUNNING" and not run.get("first_output_at")


def derive_status(run: dict, *, awaiting_handshake: bool = False, heartbeat_stale: bool = False) -> str:
    """Maps `run["state"]` (+ `cancel_requested`/`failure_reason`) to the
    mission's display vocabulary. `INTERRUPTED`/`UNKNOWN`/any unrecognized
    state is conservatively mapped to `Requires Attention` — this module
    never guesses a terminal-ok outcome for an ambiguous run, mirroring
    `Supervisor.reconcile()`'s own conservatism.

    A `FAILED` run whose `failure_reason` carries the `"blocked:"`/
    `"incomplete:"` prefix `runtime.outcome.classify_process_result`
    produces (via `Supervisor._supervise`) is displayed as `Blocked`/
    `Incomplete` rather than the generic `Failed` — both are still process
    exits that did not deliver the requested work, but the reason differs
    (a denied tool call / explicit blocker language, vs. a clean exit that
    simply didn't produce the required repository changes) and the UI must
    be able to tell them apart (Required fix 7).

    `awaiting_handshake`/`heartbeat_stale` are **opt-in** refinements of the
    `RUNNING` case only, and both default to `False` so every existing
    single-argument caller keeps its exact prior behavior (a plain RUNNING run
    is still `Running`). When the live projection layer passes them:

    - `heartbeat_stale=True` -> `STATUS_STALE`: the process is still recorded
      RUNNING but the UI's liveness probe is momentarily old. A *warning about
      a running process*, not a failure.
    - `awaiting_handshake=True` -> `STATUS_STARTING`: a valid PID exists but no
      output has arrived yet ("agent started but early output was not
      received"). Also a warning, never a failure — this is exactly the state
      a healthy-but-slow-to-first-token run occupies, and it must never be
      shown as `Failed`.

    Neither ever applies to a non-RUNNING state, and an explicit
    `cancel_requested` still wins over both (a cancel in flight is `Waiting`,
    regardless of handshake/probe)."""
    state = run.get("state", "UNKNOWN")
    if state in ("PREPARED", "QUEUED"):
        return STATUS_LAUNCHING
    if state == "RUNNING":
        if run.get("cancel_requested"):
            return STATUS_WAITING
        if heartbeat_stale:
            return STATUS_STALE
        if awaiting_handshake:
            return STATUS_STARTING
        return STATUS_RUNNING
    if state == "COMPLETED":
        return STATUS_COMPLETED
    if state == "FAILED":
        reason = run.get("failure_reason") or ""
        # A pre-spawn executor-capability mismatch is a distinct, blocking
        # preflight outcome — never conflated with a user denial or a Claude
        # runtime failure (Required fix 8).
        if reason.startswith(_CAPABILITY_MISMATCH_REASON_PREFIX):
            return STATUS_CAPABILITY_MISMATCH
        if reason.startswith(_BLOCKED_REASON_PREFIX):
            return STATUS_BLOCKED
        if reason.startswith(_INCOMPLETE_REASON_PREFIX):
            return STATUS_INCOMPLETE
        return STATUS_FAILED
    if state == "CANCELLED":
        return STATUS_CANCELLED
    return STATUS_REQUIRES_ATTENTION


def _split_caps(value: str | None) -> list[str]:
    """A persisted comma-joined capability list back into a Python list
    (empty list for NULL/empty — a legacy run row that predates the capability
    columns)."""
    if not value:
        return []
    return [item for item in value.split(",") if item]


def capability_mismatch_detail(run: dict) -> str:
    """The full, human-facing capability-mismatch sentence for a run, rebuilt
    from its persisted `required_capabilities`/`granted_capabilities` columns
    (never the raw machine `failure_reason` code). Falls back to the raw
    `failure_reason` for a legacy row that recorded the code but not the sets."""
    required = frozenset(_split_caps(run.get("required_capabilities")))
    granted = frozenset(_split_caps(run.get("granted_capabilities")))
    if required and granted is not None:
        missing = required - granted
        if missing:
            return capabilities.build_mismatch_reason(missing, granted)
    return run.get("failure_reason") or "Executor capability mismatch."


def operator_display_status(
    status: str, *, progress: int | float | None, task_type: str | None
) -> str:
    """Apply the final operator-facing completion guard to a derived status.

    A write-capable run that exited cleanly is not delivered until its task
    reaches 100 percent (verified merge); read-only reviews/audits genuinely
    finish at process completion. Keeping this pure rule here lets every
    counter and every card use the same definition instead of duplicating it
    in individual Streamlit renderers.
    """
    if status == STATUS_COMPLETED and progress is not None and progress < 100:
        if not is_read_only_task_type(task_type):
            return STATUS_REQUIRES_ATTENTION
    return status


def format_elapsed(seconds: float | None) -> str:
    """`HH:MM:SS`, or `"—"` for `None`/negative (never happens in practice,
    guarded defensively since `now` can theoretically race a just-recorded
    `started_at` by a few milliseconds)."""
    if seconds is None or seconds < 0:
        return "—"
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def elapsed_seconds(started_at: str | None, finished_at: str | None, now: datetime) -> float | None:
    started = _parse_iso(started_at)
    if started is None:
        return None
    # _parse_iso always produces UTC-aware datetimes; normalise `now` to match
    # so the subtraction never raises TypeError when the caller passes naive now.
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    end = _parse_iso(finished_at) or now
    return max(0.0, (end - started).total_seconds())


def median_completed_run_seconds(runs: list[dict], *, task_type: str | None = None) -> float | None:
    """The median wall-clock duration of *actually completed* runs — the real,
    historical "how long a run takes" the operator cares about, so the progress
    bar and the "осталось" estimate track typical execution instead of the run's
    timeout budget (which a run almost never spends in full).

    Only `COMPLETED` runs with both `started_at` and `completed_at` count — a
    failed/interrupted/timed-out run is not a fair sample of a normal duration.
    Optionally narrowed to one `task_type` (a review is quick, an implementation
    is not). Returns `None` when there is no history yet, in which case the
    caller falls back to the timeout budget or to elapsed-only display.
    """
    durations: list[float] = []
    for run in runs:
        if run.get("state") != "COMPLETED":
            continue
        if task_type is not None and (run.get("task_type") or None) != task_type:
            continue
        started = _parse_iso(run.get("started_at"))
        ended = _parse_iso(run.get("completed_at"))
        if started is None or ended is None:
            continue
        secs = (ended - started).total_seconds()
        if secs > 0:
            durations.append(secs)
    if not durations:
        return None
    durations.sort()
    mid = len(durations) // 2
    if len(durations) % 2:
        return durations[mid]
    return (durations[mid - 1] + durations[mid]) / 2


# Per-render cache: many runs share the same workspace, so `get_status` only
# needs to run once per unique path per page load. Cleared by
# `clear_git_status_cache()` at the start of each render cycle.
_git_status_cache: dict[str, dict[str, Any] | None] = {}


def clear_git_status_cache() -> None:
    """Clear the per-render git-status cache. Call once at the start of each
    dashboard render so stale results from the previous render are discarded."""
    _git_status_cache.clear()


def live_git_status(workspace_path: str | None) -> dict[str, Any] | None:
    """Read-only, best-effort: never switches branches, never mutates the
    workspace. Returns `None` if the path is missing, not a directory, or not
    a git repo — callers render "—" rather than crashing. Results are cached
    per `workspace_path` within a single render cycle for deduplication."""
    if not workspace_path:
        return None
    if workspace_path in _git_status_cache:
        return _git_status_cache[workspace_path]
    path = Path(workspace_path).expanduser()
    if not path.is_dir():
        _git_status_cache[workspace_path] = None
        return None
    status = git_info.get_status(path)
    result = status if status.get("is_repo") else None
    _git_status_cache[workspace_path] = result
    return result


def _summarize_event(event: dict | None) -> dict | None:
    if not event:
        return None
    event_type = event.get("event_type", "?")
    payload = event.get("payload") or {}
    if event_type in ("stdout_line", "stderr_line"):
        summary = (payload.get("line") or "").strip()
    elif event_type == "lifecycle":
        summary = payload.get("lifecycle") or event_type
    elif event_type == "result":
        summary = (payload.get("result") or "").strip()
    elif event_type == "assistant_message":
        summary = "assistant message"
    else:
        summary = event_type
    return {"summary": summary[:200] if summary else event_type, "at": event.get("created_at")}


# Completion-pipeline state -> compact, human display label for the Execution
# Center card. This is a pure projection: the UI reads `session["completion"]`
# and renders it; it never derives completion decisions itself.
_COMPLETION_DISPLAY_BY_STATE: dict[str, str] = {
    "EXECUTION_FINISHED": "Process finished",
    "VALIDATING_RESULT": "Verifying",
    "RESULT_VALID": "Verifying",
    "PREPARING_PULL_REQUEST": "Preparing PR",
    "PULL_REQUEST_OPEN": "In Review",
    "AWAITING_MERGE": "Awaiting merge",
    "MERGE_BLOCKED": "Merge blocked",
    "MERGED": "Finalizing",
    "VERIFYING_TARGET_BRANCH": "Finalizing",
    "COMPLETED": "Done",
    "VALIDATION_FAILED": "Requires Attention",
    "PR_CLOSED_UNMERGED": "Requires Attention",
    "REQUIRES_ATTENTION": "Requires Attention",
    "RECOVERY_PENDING": "Recovering",
    "RECOVERY_FAILED": "Requires Attention",
}


def completion_display_status(state: str | None) -> str:
    """Compact human label for a completion state (e.g. `AWAITING_MERGE` ->
    "Awaiting merge"). Distinguishes "Process finished" from "Done" — the whole
    point of the pipeline."""
    if not state:
        return "—"
    return _COMPLETION_DISPLAY_BY_STATE.get(state, state)


def completion_view(completion: dict | None) -> dict | None:
    """Flatten a `completion` DB row into the fields the Execution Center card
    renders. Returns None when the run has no completion row yet. Renders safely
    with any missing optional data (all fields default to None/—)."""
    if not completion:
        return None
    state = completion.get("completion_state")
    return {
        "state": state,
        "display": completion_display_status(state),
        "is_done": state == "COMPLETED",
        "requires_human": bool(completion.get("requires_human")),
        "is_recoverable": bool(completion.get("is_recoverable")),
        "reason_code": completion.get("last_reason_code"),
        "recommended_action": completion.get("recommended_action"),
        "branch": completion.get("branch"),
        "base_branch": completion.get("base_branch"),
        "head_commit": completion.get("head_commit"),
        "pull_request_number": completion.get("pull_request_number"),
        "pull_request_url": completion.get("pull_request_url"),
        "pull_request_state": completion.get("pull_request_state"),
        "replaced_pull_request_number": completion.get("replaced_pull_request_number"),
        "merge_commit": completion.get("merge_commit"),
        "validation_summary": completion.get("validation_summary"),
        "last_checked_at": completion.get("last_checked_at"),
        "next_retry_at": completion.get("next_retry_at"),
        "retry_count": completion.get("retry_count"),
        "merge_mode": completion.get("merge_mode"),
        "manual_merge_available": manual_merge_available(completion),
    }


def manual_merge_available(completion: dict | None) -> bool:
    """Button visibility only; live gates are re-checked on click."""
    if not completion:
        return False
    return (
        completion.get("merge_mode") == "manual"
        and completion.get("completion_state") == "AWAITING_MERGE"
        and completion.get("last_reason_code") == "AWAITING_MANUAL_MERGE"
        and bool(completion.get("pull_request_number"))
        and completion.get("pull_request_state") == "OPEN"
    )


# --------------------------------------------------------------------------
# Live progress — a *display* progress derived from observed facts, never a
# guess about how far into its work the agent is.
#
# The Kanban task carries a `progress` field pinned to a stage (`Workspace
# Verified` == 5). Nothing advances that stage while a run executes, so a
# healthy run's bar sat at 5 % from launch until the merge jumped it to 100 —
# the "always 5 %" the operator saw. This function ignores that stale field and
# reports the run's real position instead: the process phase before the run
# finishes, then the completion pipeline's own state, which is a genuine,
# observed milestone chain (validate -> review -> PR -> merge -> verify).
#
# Within the working phase there is no observable fraction complete. Elapsed
# time is shown separately and must never be converted into delivery progress.
# A running process therefore reports only its latest evidenced milestone.
_RUN_WORKING_FLOOR = 5
_RUN_WORKING_CEILING = 92

# Task types that have no merge lifecycle — a review/audit/gate produces a
# report and is *done* when its process exits cleanly; there is no PR to open
# or branch to merge, so a clean `COMPLETED` is 100 %, not an incomplete run.
_READ_ONLY_TASK_TYPES = frozenset({"review", "final_gate", "architecture_review"})

_RUN_PHASE_PROGRESS: dict[str, tuple[int, str]] = {
    STATUS_LAUNCHING: (2, "Запуск"),
    STATUS_STARTING: (5, "Старт агента"),
    STATUS_WAITING: (50, "Отмена в процессе"),
}

# Completion-pipeline state -> (percent, stage label). The merge pipeline runs
# *after* the process exits, so it occupies the top band (93–100 %) above the
# working phase's 92 % cap — reaching a completion state always moves the bar
# forward, never back. Every entry is a milestone the pipeline actually reached.
_COMPLETION_PROGRESS: dict[str, tuple[int, str]] = {
    "EXECUTION_FINISHED": (93, "Процесс завершён"),
    "VALIDATING_RESULT": (94, "Валидация"),
    "RESULT_VALID": (95, "Валидация пройдена"),
    "AWAITING_REVIEW": (95, "Ожидает независимой проверки"),
    "PREPARING_PULL_REQUEST": (96, "Готовит pull request"),
    "PULL_REQUEST_OPEN": (97, "PR открыт"),
    "AWAITING_MERGE": (98, "Ожидает merge"),
    "MERGED": (99, "Смёржено"),
    "VERIFYING_TARGET_BRANCH": (99, "Проверка целевой ветки"),
    "COMPLETED": (100, "Готово"),
    # Non-progressing outcomes: the bar stops where it is and the card's own
    # error/blocker line explains it. A failure is not "97 % done".
    "VALIDATION_FAILED": (93, "Валидация не пройдена"),
    "PR_CLOSED_UNMERGED": (96, "PR закрыт без merge"),
    "MERGE_BLOCKED": (97, "Merge заблокирован"),
    "REVIEW_REJECTED": (95, "Проверка не одобрила"),
    "REQUIRES_ATTENTION": (93, "Требует внимания"),
    "RECOVERY_PENDING": (60, "Восстановление"),
    "RECOVERY_FAILED": (60, "Восстановление не удалось"),
}


def is_read_only_task_type(task_type: str | None) -> bool:
    """A review/audit/gate task has no merge lifecycle — its clean process exit
    *is* its terminal success."""
    return task_type in _READ_ONLY_TASK_TYPES


def derive_live_progress(
    display_status: str,
    completion: dict | None,
    *,
    task_type: str | None = None,
    elapsed_seconds: float | None = None,
    timeout_seconds: float | None = None,
    stage_progress: int | None = None,
    stage_label: str | None = None,
) -> tuple[int | None, str | None]:
    """A truthful (percent, stage) for the progress bar, from what is actually
    observed about the run — its process phase, then its completion state.

    Returns ``(None, None)`` for a status that has no meaningful progress bar
    (a plain terminal ``Failed``/``Cancelled``/``Requires Attention`` with no
    completion row): the card shows the reason, not a bar. A completion row,
    when present, always wins over the process phase — it is the later, more
    specific fact.

    ``stage_progress``/``stage_label`` carry the Kanban task's milestone-based
    progress (``STAGE_PROGRESS`` for ``current_stage``). For a RUNNING run the
    time-based fraction is a *lower bound*: the bar shows ``max(time_frac,
    stage_progress)`` so it never drops below a milestone the task already
    reached (e.g. once the agent started producing output and the task moved
    to "Implementation" at 40 %, the bar never reads 9 % — it stays at least
    40 % and climbs with time from there).
    """
    # A read-only task that has exited cleanly is *done* — 100 %. This wins over
    # any completion row: the auto-merge pipeline can spuriously seed a
    # completion (and even reach MERGE_BLOCKED) for a review/audit run that has
    # nothing to merge, and reporting "86 % · Merge заблокирован" for a
    # successful analysis is exactly the confusion this guards against.
    if display_status == STATUS_COMPLETED and is_read_only_task_type(task_type):
        return 100, "Готово"

    if completion:
        mapped = _COMPLETION_PROGRESS.get(completion.get("state") or "")
        if mapped:
            return mapped

    if display_status in (STATUS_RUNNING, STATUS_STALE):
        del elapsed_seconds, timeout_seconds
        milestone = int(stage_progress) if stage_progress is not None else 25
        return min(_RUN_WORKING_CEILING, max(_RUN_WORKING_FLOOR, milestone)), stage_label or "Выполняется"

    phase = _RUN_PHASE_PROGRESS.get(display_status)
    if phase:
        return phase

    if display_status == STATUS_COMPLETED:
        # A read-only task (review/audit/gate) is truly done at a clean exit —
        # there is nothing to merge, so it is 100 %. Everything else exited but
        # has not yet merged: 55 %, deliberately not 100 (100 is reserved for a
        # completion verified present in the target branch).
        if is_read_only_task_type(task_type):
            return 100, "Готово"
        return 55, "Процесс завершён"
    return None, None


def build_session_view(
    run: dict,
    *,
    kanban_task: dict | None,
    project_cfg: dict | None,
    latest_event: dict | None,
    report_path: str | None,
    now: datetime,
    heartbeat_stale: bool = False,
    completion: dict | None = None,
    reference_seconds: float | None = None,
) -> dict:
    """The canonical execution-session view model (mission field list). The
    only I/O performed here is one read-only `git status` call against the
    run's own resolved workspace — never a switch/checkout/write.

    `reference_seconds` is the *expected* run duration (the task's estimate, or
    the historical median for its type) — the realistic denominator for the
    progress bar and the "осталось" estimate. It is preferred over the run's raw
    timeout, which is a hard cap (~200 % of the estimate) a run almost never
    spends in full, so basing "remaining" on it read as unreal.

    `heartbeat_stale` is the caller's (UI-owned) liveness-probe verdict for
    this run — kept out of this pure module and passed in, exactly like
    `project_overview` already takes `stale_run_ids`. When it is `True` for a
    still-RUNNING run, `status` becomes `STATUS_STALE` (a warning about a
    running process, never a failure)."""
    awaiting_handshake = is_awaiting_handshake(run)
    status = derive_status(run, awaiting_handshake=awaiting_handshake, heartbeat_stale=heartbeat_stale)
    started_at = run.get("started_at")
    finished_at = run.get("completed_at")
    canonical_provenance = run.get("provenance") or {}
    workspace_path = canonical_provenance.get("worktree") or run.get("repository_path")
    # Only probe the filesystem for live runs — the branch/status of a
    # terminal run was captured at completion time and never changes. This
    # avoids one ``git status`` subprocess (3 git calls) per terminal run,
    # which dominated refresh time at scale (107 runs × ~10 ms = ~1 s).
    run_state = run.get("state") or ""
    if run_state in ("PREPARED", "QUEUED", "RUNNING"):
        git_status = live_git_status(workspace_path)
        actual_branch = (git_status or {}).get("branch")
    else:
        # Terminal runs: branch was captured at completion. Fall back to the
        # expected branch so the UI still shows it without a git subprocess.
        git_status = None
        actual_branch = run.get("expected_branch")

    task_title = (kanban_task or {}).get("title") or (run.get("prompt") or "")[:80] or (run.get("id") or "")[:8]
    executor = run.get("provider_id") or (kanban_task or {}).get("executor") or "claude_code"

    last_error = run.get("failure_reason")
    if not last_error and status == STATUS_FAILED and latest_event and latest_event.get("event_type") == "stderr_line":
        last_error = (latest_event.get("payload") or {}).get("line")

    # Explicit, unambiguous field for `Blocked`/`Incomplete` — distinct from
    # `last_error` (which also covers plain `Failed` stderr output) so the UI
    # can render "why this was blocked" without having to re-derive it from
    # `status` + `last_error` itself (Required fix 7). For a capability
    # mismatch, render the full human sentence rebuilt from the persisted
    # required/granted capability sets, not the machine `failure_reason` code.
    if status == STATUS_CAPABILITY_MISMATCH:
        blocker_reason = capability_mismatch_detail(run)
    elif status in (STATUS_BLOCKED, STATUS_INCOMPLETE):
        blocker_reason = run.get("failure_reason")
    else:
        blocker_reason = None

    return {
        "session_id": run.get("session_id"),
        "run_id": run.get("id"),
        "task_id": run.get("task_id"),
        "task_title": task_title,
        "project_id": run.get("project"),
        "executor": executor,
        "workspace_path": workspace_path,
        "repository_path": canonical_provenance.get("repository")
        or (project_cfg or {}).get("repository_path")
        or workspace_path,
        "expected_branch": canonical_provenance.get("branch") or run.get("expected_branch"),
        "actual_branch": actual_branch,
        "git_status": git_status,
        "launch_source": run.get("launch_source") or "execution_center_adhoc",
        "process_id": run.get("pid"),
        "started_at": started_at,
        "finished_at": finished_at,
        # Spawn confirmation vs. startup/handshake, kept as two distinct,
        # explicit fields so the UI can say "started but early output not yet
        # received" without re-deriving it: `process_id`/`started_at` prove
        # the process was *created*; `first_output_at`/`handshake_received`
        # prove it is *alive and talking*. `awaiting_handshake` is the window
        # between the two.
        "first_output_at": run.get("first_output_at"),
        "handshake_received": bool(run.get("first_output_at")),
        "awaiting_handshake": awaiting_handshake,
        "elapsed_seconds": elapsed_seconds(started_at, finished_at, now),
        # The run's hard timeout cap, and the realistic *expected* duration
        # (estimate/history) the bar and "осталось" actually use. `reference`
        # falls back to the timeout only when there is no estimate or history.
        "timeout_seconds": run.get("timeout_seconds"),
        "reference_seconds": reference_seconds if reference_seconds else run.get("timeout_seconds"),
        "status": status,
        # `current_stage`/`progress` stay the raw Kanban-task fields — the
        # display-status guard (`Completed` implies `progress == 100`) and the
        # existing view contract both depend on them, so they are left exactly
        # as they were.
        "current_stage": (kanban_task or {}).get("current_stage"),
        "progress": (kanban_task or {}).get("progress"),
        # …but the Live Execution Center bar renders these instead: a truthful
        # progress derived from the run's *observed* phase and completion state
        # (see `derive_live_progress`), because the Kanban `progress` above is
        # pinned to a stage nothing advances during a run and therefore sat at
        # 5 % for a healthy run's whole life. `None` here means "no meaningful
        # bar" — the card shows the reason instead.
        **dict(
            zip(
                ("live_progress", "live_stage"),
                derive_live_progress(
                    status,
                    completion_view(completion),
                    task_type=run.get("task_type") or (kanban_task or {}).get("task_type"),
                    elapsed_seconds=elapsed_seconds(started_at, finished_at, now),
                    # Prefer the realistic expected duration (estimate/history);
                    # fall back to the timeout only when neither exists.
                    timeout_seconds=reference_seconds if reference_seconds else run.get("timeout_seconds"),
                    # The task's milestone-based progress (STAGE_PROGRESS for
                    # current_stage) acts as a floor for the time-based bar so
                    # it never drops below a milestone the task already reached.
                    stage_progress=(kanban_task or {}).get("progress"),
                    stage_label=(kanban_task or {}).get("current_stage"),
                ),
            )
        ),
        # The task's stated objective, carried alongside the prompt so a card
        # can answer "what is this agent trying to achieve" without the reader
        # parsing a multi-kilobyte prompt. Task-level, not run-level: the run
        # stores the prompt it was launched with, while the goal belongs to the
        # task the run serves — an ad-hoc run has a prompt and no goal.
        "task_goal": (kanban_task or {}).get("goal"),
        "task_type": run.get("task_type") or (kanban_task or {}).get("task_type"),
        "exit_code": run.get("exit_code"),
        "last_error": last_error,
        "blocker_reason": blocker_reason,
        "capability_profile": run.get("capability_profile") or capabilities.profile_for_task_type(
            run.get("task_type") or ""
        ),
        "capability_override": run.get("capability_override"),
        "required_capabilities": _split_caps(run.get("required_capabilities")),
        "granted_capabilities": _split_caps(run.get("granted_capabilities")),
        "capability_preflight": run.get("capability_preflight"),
        "command_policy": run.get("command_policy"),
        "prompt_version": run.get("prompt_version"),
        "prompt": run.get("prompt"),
        "report_path": report_path,
        "commit_hash": run.get("commit_hash"),
        "pull_request_url": run.get("pull_request_url"),
        "provenance": run.get("provenance"),
        "latest_event": _summarize_event(latest_event),
        # Post-execution completion pipeline (None until the run completes and a
        # completion row is seeded). This is what lets the UI distinguish
        # "process finished" from "task completed and merged".
        "completion": completion_view(completion),
    }


# --------------------------------------------------------------------------
# Heartbeat — a derived liveness *probe*, never an agent-emitted signal.
#
# The runtime has no heartbeat concept: no component here ever pings, and no
# `run`/`session` row carries a "last alive at" column. What this section
# provides instead is honestly a different thing wearing the same word the
# mission's mock uses: the wall-clock time the UI itself last *confirmed*
# (via a cheap, read-only `identity.query_identity(pid).status == LIVE`
# check — the exact primitive `Supervisor.reconcile()` already uses, just
# not mutating anything) that a Running session's PID still exists. It is
# intentionally kept in `st.session_state` by the caller, not in
# `runtime.db` — persisting a real timestamp on every 2-5s refresh tick for
# the life of every run would mean one new row (or write) per tick, an
# unbounded growth this project's log-tailing design elsewhere explicitly
# avoids. Every rendering of this value must be labeled "derived liveness
# probe", never "heartbeat" alone, per the mission's explicit instruction.
# --------------------------------------------------------------------------


def heartbeat_age_seconds(last_probe_at: datetime | None, now: datetime) -> float | None:
    if last_probe_at is None:
        return None
    return max(0.0, (now - last_probe_at).total_seconds())


def is_heartbeat_stale(
    last_probe_at: datetime | None, now: datetime, *, threshold_seconds: float = DEFAULT_HEARTBEAT_STALE_SECONDS
) -> bool:
    """`True` if there is no probe yet, or the last one is older than
    `threshold_seconds` — both cases mean the UI cannot currently vouch for
    this session's liveness."""
    age = heartbeat_age_seconds(last_probe_at, now)
    return age is None or age > threshold_seconds
