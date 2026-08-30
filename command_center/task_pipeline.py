"""Desktop autopilot: the one bounded, idempotent tick that composes the
already-existing state machines into a working pipeline.

This module is deliberately an **orchestrator, not an engine**. Every decision
it acts on is produced by a component that already existed and is already
tested; nothing here re-implements ranking, dependency readiness, capacity
planning, launching, validation/PR/merge, or the Kanban projection:

    persisted opt-in                 `command_center.pipeline_settings`
    reconcile + completion advance   `runtime.api.ExecutionCenterAPI`
                                     `runtime.completion_service`
    Kanban projection                `runtime.task_sync`
    dependency readiness             `execution_queue.reevaluate_and_persist`
    capacity/workspace planning      `runtime.scheduler.plan`
    the actual launch                `execution_queue.launch_ready`
    persistent task writes           `tasks_repository`

The missing piece this module supplies is the *wiring*: `runtime.scheduler` is
a pure planner whose decisions are keyed by `task_id` and are never mapped back
onto the queue entries that produced them, and `execution_queue.launch_ready`
has only ever been reachable from a button click. `tick()` closes both gaps in
one pass — reconcile -> advance completions -> sync Kanban -> project verified
merges to `Done` -> refresh queue readiness -> plan -> launch the `ASSIGN`
decisions -> re-plan — and returns a fully serializable `PipelineTickResult` in
which every entry, launched or not, carries a machine-readable reason.

Invariants this module is responsible for holding:

- **Explicit persisted opt-in.** Nothing automatic happens unless
  `pipeline_settings` says so, and every switch there is off by default and
  fail-closed on a malformed file. The tick branches on the combined
  `auto_launch_active` / `auto_merge_active` properties, never on a raw
  boolean, so the master switch genuinely gates everything.
- **No second gate, and no bypassed gate.** Admission consults exactly the
  shared `launch_service.prepare_task_launch` classification the queue's own
  launcher uses, read-only, purely so a permanently dirty/blocked workspace
  does not consume a capacity slot every tick and starve the wave.
  `execution_queue.launch_ready` still re-runs it authoritatively, and the
  sensitive-content confirmation (`context_service.require_launch_confirmation`)
  and fail-closed workspace verification at `Supervisor.start_raw` are reached
  unchanged.
- **Bounded, and never blocking.** One pass, no loops, no sleeps, and a hard
  ceiling on how long the caller's thread can be held. Streamlit calls this from
  its existing refresh tick (see `app.py`), so any step that can take minutes —
  running a project's validation commands, `git fetch`, a `gh` round trip — is
  performed off the render thread by `_AdvanceWorker` and its results are folded
  into whichever tick they are ready for. The tick either does its one pass or
  reports why it did not; it never freezes the dashboard operators use to see
  and cancel real running processes.
- **At most one active attempt per task and per workspace.** Three independent
  layers already enforce this and are all retained: the same-host advisory
  `pipeline_lock` serializes ticks; `scheduler.plan` emits at most one `ASSIGN`
  per `task_id` and never two against one workspace (seeded from the *live*
  `LoadSnapshot`, so a run started by a previous tick still counts);
  `launch_service.find_active_run_conflict` raises before any subprocess.
- **A failed item never aborts the wave.** `launch_ready` already isolates a
  per-entry failure; this module additionally isolates the whole launch batch,
  each completion advance, the merge-policy pass and the Kanban projection, so
  a transient GitHub/git fault degrades the tick instead of killing it.

No new task store, no new queue, no second completion state machine.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
import dataclasses
from pathlib import Path

from command_center import (
    activity_log,
    execution_queue,
    git_info,
    launch_service,
    models,
    report_parser,
    pipeline_settings,
    project_config,
    storage,
    tasks_repository,
    workspace_provisioning,
)
from command_center.pipeline_settings import PipelineSettings
from command_center.runtime import completion as completion_domain
from command_center.runtime import completion_service
from command_center.runtime import db as runtime_db
from command_center.runtime import reports
from command_center.runtime import scheduler, task_sync
from command_center.runtime.completion import CompletionPolicy

_LOG = logging.getLogger(__name__)

PIPELINE_LOCK_FILE_NAME = "task_pipeline.lock"

# Deliberately short. This lock exists to make a *concurrent* tick a no-op, not
# to queue ticks up behind one another: a Streamlit refresh that finds the
# pipeline busy should return immediately and report `pipeline_busy`, because
# the tick already running is doing the identical work. A longer wait would
# stall a render for no additional effect.
PIPELINE_LOCK_TIMEOUT_SECONDS = 0.5
_PIPELINE_LOCK_POLL_SECONDS = 0.05

# How many completion rows one tick will advance/repolicy, and how much run
# history one work-item adaptation reads. All three bound the tick's cost
# against a large store.
COMPLETION_ADVANCE_LIMIT = 50
COMPLETION_SCAN_LIMIT = 500
RUN_HISTORY_LIMIT = 200

# How long a tick will wait for the off-thread completion advance before moving
# on. Sized so the common case (nothing due, or one cheap step) is reported by
# the tick that started it, while a validation run or a `gh` round trip is left
# to finish in the background and be reported by a later tick. This is the hard
# ceiling on how long a Streamlit render can be held by the advance step.
ADVANCE_WAIT_SECONDS = 1.0


# --------------------------------------------------------------------------
# Vocabulary: actions, reason codes, remediation
# --------------------------------------------------------------------------

# `scheduler.ACTION_ASSIGN` / `ACTION_DEFER` / `ACTION_BLOCKED` are re-used
# verbatim. `ACTION_SKIPPED` is this module's own fourth action, for a queue
# entry that never became a `WorkItem` at all (so the planner never saw it).
ACTION_SKIPPED = "SKIPPED"

# Admission reasons. The four conditions that also exist at launch time reuse
# `execution_queue`'s own `LAUNCH_SKIP_*` codes verbatim rather than defining a
# parallel spelling: an entry skipped at admission and the same entry refused by
# `launch_ready` are the *same* fact observed at two moments, so they must carry
# the same machine-readable code and the same remediation. (Two of these were
# briefly duplicated as separate constants with identical string values, which
# silently collided as duplicate keys in `REMEDIATION_BY_REASON` — aliasing makes
# the shared vocabulary explicit instead of accidental.)
REASON_TASK_MISSING = execution_queue.LAUNCH_SKIP_TASK_NOT_FOUND
REASON_WORKSPACE_UNCONFIGURED = execution_queue.LAUNCH_SKIP_WORKSPACE_NOT_CONFIGURED
REASON_LAUNCH_BLOCKED = execution_queue.LAUNCH_SKIP_BLOCKED
REASON_NEEDS_CONFIRMATION = execution_queue.LAUNCH_SKIP_NEEDS_CONFIRMATION
# These admission-only reasons have no launch-time counterpart.
REASON_DUPLICATE_QUEUE_ENTRY = "duplicate_queue_entry"
REASON_COMPLETION_IN_PROGRESS = "completion_in_progress"
REASON_ADAPTATION_FAILED = "adaptation_failed"

# A READY queue row can outlive execution and overlap the completion pipeline.
# These states mean the engineering work is already being validated, reviewed,
# published, merged, or verified, so launching another agent would duplicate
# work. Failure states are deliberately excluded: the bounded rework loop may
# legitimately enqueue a new attempt after validation or CI fails.
_COMPLETION_LAUNCH_BLOCKING_STATES: frozenset[str] = frozenset(
    {
        completion_domain.CompletionState.EXECUTION_FINISHED,
        completion_domain.CompletionState.VALIDATING_RESULT,
        completion_domain.CompletionState.RESULT_VALID,
        completion_domain.CompletionState.AWAITING_REVIEW,
        completion_domain.CompletionState.PREPARING_PULL_REQUEST,
        completion_domain.CompletionState.PULL_REQUEST_OPEN,
        completion_domain.CompletionState.AWAITING_MERGE,
        completion_domain.CompletionState.MERGED,
        completion_domain.CompletionState.VERIFYING_TARGET_BRANCH,
        completion_domain.CompletionState.RECOVERY_PENDING,
    }
)

# Why a whole tick did not run (or did not launch).
TICK_DISABLED = "autopilot_disabled"
TICK_BUSY = "pipeline_busy"
TICK_RAN = "ran"
LAUNCH_DISABLED = "auto_launch_disabled"
LAUNCH_BUDGET_EXHAUSTED = "daily_spend_budget_exhausted"
# Distinct from LAUNCH_BUDGET_EXHAUSTED on purpose: "exhausted" asserts a
# *known* spend at/over the ceiling. When `daily_spend_usd` cannot be trusted
# (a corrupt cost event, an overflowed total, an unreadable DB) the honest
# verdict is "unknown", not "exhausted" — conflating the two previously made a
# corrupt event masquerade as a real budget cap (VOYN-W0-AICC-REPORT-319).
LAUNCH_SPEND_UNKNOWN = "daily_spend_unknown"
LAUNCH_BATCH_FAILED = "launch_batch_failed"

# Completion audit event appended when this module reconciles a row's merge
# policy with the current host-level opt-in. This pair lives in the
# *completion-event* namespace, not the decision-reason one — it never appears
# on an `EntryDecision`, so it is deliberately not named `REASON_*`.
EV_MERGE_POLICY_APPLIED = "MERGE_POLICY_APPLIED"
MERGE_POLICY_EVENT_REASON = "merge_policy_opt_in"

# Activity-log event types (durable audit; see AICC-DESKTOP-017).
EV_PIPELINE_LAUNCHED = "pipeline_launched"
EV_PIPELINE_SKIPPED = "pipeline_skipped"
EV_PIPELINE_COMPLETED = "pipeline_task_completed"
EV_PIPELINE_REWORK = "pipeline_rework"
EV_PIPELINE_REMEDIATED = "pipeline_workspace_remediated"
EV_PIPELINE_REVIEW = "pipeline_review"

# Operator remediation per machine-readable reason code — the "and what do I do
# about it?" half of every DEFER/BLOCKED/SKIPPED decision. Kept as data here
# (not as UI strings) so the desktop panel, a CLI, and a test all read the
# identical advice. Covers every `scheduler.REASON_*`, every `REASON_*` above,
# and every `execution_queue.LAUNCH_SKIP_*` launch outcome.
REMEDIATION_BY_REASON: dict[str, str] = {
    scheduler.REASON_ASSIGNED: "Задача назначена агенту — запуск выполняется.",
    scheduler.REASON_WAITING_DEPENDENCY: (
        "Завершите зависимости задачи (перевод в Done после подтверждённого merge)."
    ),
    scheduler.REASON_BACKOFF: (
        "Предыдущая попытка завершилась ошибкой; повтор станет возможен после окончания backoff."
    ),
    scheduler.REASON_RETRY_TIMING_UNKNOWN: (
        "У предыдущего прогона нет времени завершения — проверьте запись прогона "
        "или перезапустите задачу вручную."
    ),
    scheduler.REASON_DUPLICATE_TASK: "У задачи уже есть активная попытка — дождитесь её завершения.",
    scheduler.REASON_WORKSPACE_BUSY: "Workspace занят другим активным прогоном — дождитесь его завершения.",
    scheduler.REASON_GLOBAL_AT_CAPACITY: (
        "Достигнут общий лимит параллельности — увеличьте его в настройках автопилота."
    ),
    scheduler.REASON_AGENT_AT_CAPACITY: (
        "Агент загружен — увеличьте лимит на агента или дождитесь освобождения."
    ),
    scheduler.REASON_AGENT_UNAVAILABLE: "Все подходящие агенты недоступны — проверьте конфигурацию исполнителей.",
    scheduler.REASON_MALFORMED: "Запись очереди повреждена — удалите её из очереди и добавьте задачу заново.",
    scheduler.REASON_INVALID_ATTEMPT_STATE: (
        "История прогонов задачи повреждена — проверьте прогоны в Live Execution Center."
    ),
    scheduler.REASON_NO_CAPABLE_AGENT: "Нет агента с нужными возможностями — измените исполнителя задачи.",
    scheduler.REASON_TERMINAL_FAILURE: (
        "Повтор не поможет: устраните причину отказа и создайте новую попытку вручную."
    ),
    scheduler.REASON_RETRY_EXHAUSTED: "Исчерпан бюджет повторов — разберите причину сбоя и перезапустите вручную.",
    REASON_DUPLICATE_QUEUE_ENTRY: "В очереди несколько записей для одной задачи — лишние игнорируются.",
    REASON_COMPLETION_IN_PROGRESS: (
        "Задача уже проходит проверку, публикацию PR или merge — дождитесь завершения "
        "completion pipeline либо выполните доступное ручное действие."
    ),
    REASON_ADAPTATION_FAILED: "Не удалось подготовить задачу к планированию — см. текст ошибки.",
    execution_queue.LAUNCH_OK: "Запуск начат.",
    execution_queue.LAUNCH_SKIP_TASK_DONE: (
        "Задача уже в статусе Done — запись очереди устарела, удалите её."
    ),
    execution_queue.LAUNCH_SKIP_TASK_NOT_FOUND: "Задача очереди больше не существует — очистите запись очереди.",
    execution_queue.LAUNCH_SKIP_WORKSPACE_NOT_CONFIGURED: (
        "Укажите workspace_path задачи или repository_path проекта."
    ),
    execution_queue.LAUNCH_SKIP_BLOCKED: (
        "Устраните ошибки workspace (нет каталога, не git-репозиторий) и повторите."
    ),
    execution_queue.LAUNCH_SKIP_NEEDS_CONFIRMATION: (
        "Есть предупреждения (грязное дерево, detached HEAD, несовпадение ветки) — "
        "запустите вручную из карточки задачи с подтверждением."
    ),
    execution_queue.LAUNCH_SKIP_DUPLICATE_ACTIVE: (
        "Для задачи или workspace уже есть активный прогон — дождитесь его завершения."
    ),
    execution_queue.LAUNCH_SKIP_WORKSPACE_VERIFICATION: (
        "Workspace не принадлежит репозиторию проекта или не на ожидаемой ветке — "
        "проверьте изоляцию worktree."
    ),
    # `LAUNCH_SKIP_GLOBAL_AT_CAPACITY` aliases `scheduler.REASON_GLOBAL_AT_CAPACITY`
    # (same string) and is already covered by its entry above — the launch-time
    # and admission-time views of one fact. `LAUNCH_SKIP_WORKSPACE_LOCKED` is the
    # launch-time race where another run claimed the workspace between the queue
    # snapshot and the atomic create_run insert.
    execution_queue.LAUNCH_SKIP_WORKSPACE_LOCKED: (
        "Другой прогон занял этот workspace в момент запуска (гонка) — "
        "дождитесь его завершения и повторите."
    ),
    execution_queue.LAUNCH_SKIP_LAUNCH_ERROR: "Запуск завершился ошибкой — см. сообщение.",
    execution_queue.LAUNCH_SKIP_NO_AVAILABLE_EXECUTOR: (
        "Ни один из разрешённых исполнителей не доступен — "
        "проверьте установку и авторизацию агентов (claude, copilot, ollama)."
    ),
    # Provider-reported causes (see `providers.*.classify_failure`). These
    # arrive as a run's `failure_reason`, and the wave shows them verbatim, so
    # each needs the one action that actually resolves it.
    "session_expired": "Сессия Claude истекла — выполните `claude` в терминале и войдите заново.",
    "authentication_failed": "Не пройдена аутентификация провайдера — проверьте вход и ключи.",
    "quota_limit": "Исчерпан лимит/квота провайдера — дождитесь сброса или смените план.",
    "provider_api_error": "Временная ошибка API провайдера — задача будет повторена.",
    "provider_exit_nonzero": "Процесс завершился с ненулевым кодом — см. отчёт прогона.",
    "daemon_unreachable": "Локальный сервер Ollama недоступен — запустите `ollama serve`.",
    "model_missing": "Модель не скачана — выполните `ollama pull <модель>`.",
    "insufficient_memory": "Недостаточно памяти для модели — выберите модель меньше.",
    completion_domain.ReasonCode.VALIDATION_FAILED: (
        "Локальная проверка не прошла — автодоработка исправит ошибку и повторит проверки."
    ),
    completion_domain.ReasonCode.CHECKS_FAILING: (
        "CI не прошёл — автодоработка разберёт упавшие проверки и отправит исправление."
    ),
    completion_domain.ReasonCode.REVIEW_REJECTED: (
        "Независимое ревью отклонило изменение — автодоработка исправит замечания."
    ),
}


def remediation_for(reason_code: str | None) -> str:
    return REMEDIATION_BY_REASON.get(reason_code or "", "")


# --------------------------------------------------------------------------
# AICC-DESKTOP-005 — the same-host advisory pipeline lock
# --------------------------------------------------------------------------


def pipeline_lock_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / PIPELINE_LOCK_FILE_NAME


@contextlib.contextmanager
def pipeline_lock(root: Path, *, timeout: float = PIPELINE_LOCK_TIMEOUT_SECONDS):
    """Serialize plan-and-dispatch across every process on this host.

    Thin wrapper over `storage.file_lock` (the same `fcntl`/`msvcrt` advisory
    primitive `tasks_repository`, `execution_queue`, `pipeline_settings` and
    `portfolio_launch` already use — released by the kernel even if the holder
    crashes, so a dead tick can never wedge future ones). This is the
    *outermost* lock a tick takes; the finer-grained `tasks_lock`/`queue_lock`/
    `settings_lock` are acquired and released inside it by the services that own
    those files, and never in the reverse order, so the set cannot deadlock."""
    with storage.file_lock(pipeline_lock_path(root), timeout=timeout, poll_seconds=_PIPELINE_LOCK_POLL_SECONDS):
        yield


# --------------------------------------------------------------------------
# Completion advancing, off the caller's thread
# --------------------------------------------------------------------------


class _AdvanceWorker:
    """Runs `advance_completions` off the caller's thread, one advance at a
    time, and hands results back to whichever tick is ready to report them.

    This exists because a completion advance is the one genuinely slow step in
    the pipeline and the caller is usually a Streamlit render: `_step_validation`
    shells out to the project's configured validation commands with a 900-second
    timeout, and the PR/verify steps do `git fetch` and `gh` round trips. Running
    any of that inline would freeze the Live Execution Center — the very screen
    an operator needs in order to *see and cancel* a runaway run — for as long as
    it takes. (The pre-existing `Supervisor.start_completion_autopilot` kept the
    same work on a background daemon thread for exactly this reason.)

    The design is deliberately not a long-lived poller:

    - **At most one advance in flight per database.** A second `start()` while
      one is running is a no-op — one advance already processes every due row,
      so a second would only contend for the same CAS-protected rows.
    - **Nothing outlives the work.** There is no loop and no interval; the thread
      exists only for one advance and then dies. Disabling autopilot therefore
      stops all advancing as soon as the in-flight one finishes — no thread to
      remember to stop, and no conflict with the independent
      `AICC_COMPLETION_AUTOPILOT` opt-in, which owns its own poller.
    - **Results are never dropped.** An advance that outruns the tick that
      started it leaves its results (or its error) in `_pending`, and the next
      tick drains them, so a slow advance is reported late rather than lost.

    Advancing runs outside the `pipeline_lock`, which is correct: it never
    launches a run or touches the queue, and `runtime.db`'s compare-and-set plus
    `advance_pending`'s own `LostUpdateError` handling already make concurrent
    advancers safe.
    """

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._pending: list[dict] = []
        self._error: str | None = None
        self._lock = threading.Lock()

    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self, api, *, github, limit: int) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return

            def _run() -> None:
                try:
                    results = api.advance_completions(limit=limit, github=github)
                    records = [
                        {
                            "run_id": result.run_id,
                            "from_state": result.from_state,
                            "to_state": result.to_state,
                            "reason_code": result.reason_code,
                            "changed": result.changed,
                        }
                        for result in results
                    ]
                except Exception as exc:  # noqa: BLE001 — reported, never raised on a daemon thread
                    with self._lock:
                        self._error = str(exc)
                    return
                with self._lock:
                    self._pending.extend(records)

            thread = threading.Thread(target=_run, name="task-pipeline-advance", daemon=True)
            self._thread = thread
            thread.start()

    def join(self, timeout: float) -> None:
        """Give the in-flight advance up to `timeout` seconds to finish. A fast
        advance (the common case — nothing due, or one cheap step) completes
        well inside it, so its results are reported by the tick that started it;
        a slow one simply keeps running."""
        thread = self._thread
        if thread is not None and timeout > 0:
            thread.join(timeout)

    def drain(self) -> tuple[list[dict], str | None]:
        with self._lock:
            results, self._pending = self._pending, []
            error, self._error = self._error, None
        return results, error


_advance_workers: dict[str, _AdvanceWorker] = {}
_advance_workers_lock = threading.Lock()


def _advance_worker(db_path: Path) -> _AdvanceWorker:
    """One worker per database, so two `ExecutionCenterAPI` instances pointed at
    the same store still serialize their advances."""
    key = str(db_path)
    with _advance_workers_lock:
        worker = _advance_workers.get(key)
        if worker is None:
            worker = _AdvanceWorker()
            _advance_workers[key] = worker
        return worker


# --------------------------------------------------------------------------
# AICC-DESKTOP-003 — the auditable result types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryDecision:
    """One queue entry's fate this tick — a `scheduler.SchedulingDecision`
    re-keyed onto the queue entry that produced it, plus what actually happened
    when the decision was acted on.

    Every instance carries a `reason_code` from a closed vocabulary
    (`scheduler.REASON_*` or this module's `REASON_*`) and a human explanation.
    A decision with `action == ACTION_ASSIGN` and `launched is False` is a
    launch that was planned and then refused downstream; `launch_reason_code`
    (one of `execution_queue.LAUNCH_*`) says why in machine-readable form, and
    `launch_message`/`warnings` carry the human detail."""

    entry_id: str | None
    task_id: str | None
    action: str
    reason_code: str
    explanation: str
    title: str | None = None
    project: str | None = None
    priority: str | None = None
    workspace: str | None = None
    agent_id: str | None = None
    executor_id: str | None = None
    attempt: int | None = None
    next_eligible_at: str | None = None
    launched: bool = False
    run_id: str | None = None
    launch_reason_code: str | None = None
    launch_message: str = ""
    warnings: tuple[str, ...] = ()

    @property
    def remediation(self) -> str:
        """Operator advice for whichever code is the operative one: a refused
        launch explains itself through `launch_reason_code`, everything else
        through the scheduling `reason_code`."""
        if self.launch_reason_code and not self.launched:
            return remediation_for(self.launch_reason_code)
        return remediation_for(self.reason_code)

    def as_dict(self) -> dict:
        return {
            "entry_id": self.entry_id,
            "task_id": self.task_id,
            "action": self.action,
            "reason_code": self.reason_code,
            "explanation": self.explanation,
            "remediation": self.remediation,
            "title": self.title,
            "project": self.project,
            "priority": self.priority,
            "workspace": self.workspace,
            "agent_id": self.agent_id,
            "executor_id": self.executor_id,
            "attempt": self.attempt,
            "next_eligible_at": self.next_eligible_at,
            "launched": self.launched,
            "run_id": self.run_id,
            "launch_reason_code": self.launch_reason_code,
            "launch_message": self.launch_message,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PipelineTickResult:
    """The complete, serializable record of one tick — the audit trail and the
    UI's render model in one object.

    `ran` is False exactly when the tick did no work: autopilot disabled, or
    another tick on this host held the lock. `decisions` is the wave as planned
    *before* dispatch (in the planner's own deterministic order); `next_wave` is
    the wave re-planned immediately afterwards, which is what the operator
    should look at to see what comes next."""

    started_at: str
    finished_at: str
    settings: PipelineSettings
    ran: bool
    status: str
    decisions: tuple[EntryDecision, ...] = ()
    next_wave: tuple[EntryDecision, ...] = ()
    launch_status: str = ""
    completion_advances: tuple[dict, ...] = ()
    merge_policy_updates: tuple[dict, ...] = ()
    reworks: tuple[dict, ...] = ()
    reviews: tuple[dict, ...] = ()
    queue_divergence: tuple[dict, ...] = ()
    remediations: tuple[dict, ...] = ()
    stuck: tuple[StuckTask, ...] = ()
    completed_task_ids: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def assignments(self) -> list[EntryDecision]:
        return [d for d in self.decisions if d.action == scheduler.ACTION_ASSIGN]

    def launched(self) -> list[EntryDecision]:
        return [d for d in self.decisions if d.launched]

    def skipped(self) -> list[EntryDecision]:
        """Everything that did *not* start: a planned assignment refused at
        launch time, plus every DEFER/BLOCKED/SKIPPED decision."""
        return [d for d in self.decisions if not d.launched]

    def as_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "settings": self.settings.as_dict(),
            "ran": self.ran,
            "status": self.status,
            "launch_status": self.launch_status,
            "decisions": [d.as_dict() for d in self.decisions],
            "next_wave": [d.as_dict() for d in self.next_wave],
            "completion_advances": [dict(a) for a in self.completion_advances],
            "merge_policy_updates": [dict(u) for u in self.merge_policy_updates],
            "reworks": [dict(r) for r in self.reworks],
            "reviews": [dict(r) for r in self.reviews],
            "queue_divergence": [dict(d) for d in self.queue_divergence],
            "remediations": [dict(r) for r in self.remediations],
            "stuck": [s.as_dict() for s in self.stuck],
            "completed_task_ids": list(self.completed_task_ids),
            "errors": list(self.errors),
        }


# --------------------------------------------------------------------------
# AICC-DESKTOP-002 — READY queue entries -> scheduler.WorkItem
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AdaptedWave:
    """The result of adapting the current READY queue into planner input:
    the `WorkItem`s themselves, the `task_id -> entry_id` map that lets a
    `SchedulingDecision` be re-keyed onto its queue entry (AICC-DESKTOP-003),
    and the entries that never became work items at all."""

    work_items: tuple[scheduler.WorkItem, ...] = ()
    entry_by_task: dict[str, str] = field(default_factory=dict)
    workspace_by_task: dict[str, str] = field(default_factory=dict)
    skipped: tuple[EntryDecision, ...] = ()


def _run_history(db_path: Path, task_id: str) -> tuple[int, str | None, str | None, str | None]:
    """`(attempts_made, last_state, last_failure_reason, last_completed_at)` for
    `task_id`, read from the run table — the same rows `Supervisor` writes and
    `reconcile()` repairs, never a task-level mirror of them.

    `attempts_made` counts only *terminal* runs (an attempt that has finished,
    however it finished). A currently-active run is deliberately not counted as
    an attempt: it is already represented in `LoadSnapshot.active_task_ids`, so
    the planner defers the task as a duplicate rather than mistaking the live
    attempt for a completed one and charging it against the retry budget."""
    runs = runtime_db.list_runs(db_path, task_id=task_id, limit=RUN_HISTORY_LIMIT)
    terminal = [r for r in runs if r.get("state") in runtime_db.TERMINAL_STATES]
    if not terminal:
        return 0, None, None, None
    terminal.sort(
        key=lambda r: (r.get("completed_at") or "", r.get("created_at") or "", r.get("sequence") or 0),
        reverse=True,
    )
    latest = terminal[0]
    return (
        len(terminal),
        latest.get("state"),
        latest.get("failure_reason"),
        latest.get("completed_at"),
    )


def _skip(entry: dict, task: dict | None, reason_code: str, explanation: str) -> EntryDecision:
    task = task or {}
    return EntryDecision(
        entry_id=entry.get("id"),
        task_id=entry.get("task_id"),
        action=ACTION_SKIPPED,
        reason_code=reason_code,
        explanation=explanation,
        title=task.get("title"),
        project=task.get("project") or entry.get("project"),
        priority=task.get("priority"),
    )



def _agent_constraints(
    task: dict, project_id: str | None
) -> tuple[frozenset[str], str | None]:
    """Return ``(allowed_agents, hard_pin)`` for one schedulable task.

    Project ``allowed_agents`` is the authorization boundary. The task's
    ordinary ``executor`` is only its default/preference: it must not pin all
    work to Claude Code while Codex is idle. A task may opt into a true hard
    pin with ``executor_pinned=True``; a task naming a provider forbidden by
    project policy also remains pinned to that forbidden id so the scheduler
    reports a visible ``no_capable_agent`` refusal instead of silently
    redirecting it.

    Providers that already failed to start for this task are removed from the
    eligible set. This makes failover and normal load distribution use the same
    planner rather than two conflicting selection rules."""
    try:
        allowed = tuple(project_config.allowed_execution_providers(project_id))
    except Exception:  # noqa: BLE001 — malformed policy is reported by the launcher
        requested = task.get("executor")
        return frozenset(), requested or None

    requested = task.get("executor")
    if requested and requested not in allowed:
        return frozenset(allowed), requested

    failed = frozenset(task.get("failed_executors") or ())
    eligible = frozenset(agent for agent in allowed if agent not in failed)
    hard_pin = requested if requested and task.get("executor_pinned") else None
    return eligible, hard_pin


def adapt_ready_entries(
    entries: list[dict],
    tasks_by_id: dict[str, dict],
    project_configs: dict[str, dict],
    *,
    db_path: Path,
) -> AdaptedWave:
    """Turn the current READY queue into `scheduler.WorkItem`s.

    Entries are processed in a deterministic order (`added_at`, then entry id)
    so two ticks over identical state produce identical input — the planner's
    own determinism guarantee is worthless if its input order is not stable.

    An entry is *skipped* (never handed to the planner) when it cannot produce a
    launchable work item: its task is gone, it duplicates a task another open
    entry already represents, it has no configured workspace, or the shared
    `launch_service.prepare_task_launch` classification says the launch is
    blocked or needs a human confirmation. That last case is the one judgement
    call here, and it is a liveness fix, not a second gate: `launch_ready` would
    refuse such an entry anyway, so admitting it would burn one capacity slot
    per tick forever and starve the rest of the wave. The identical
    classification is re-run authoritatively at launch time."""
    adapted: list[scheduler.WorkItem] = []
    entry_by_task: dict[str, str] = {}
    workspace_by_task: dict[str, str] = {}
    skipped: list[EntryDecision] = []

    ordered = sorted(entries, key=lambda e: (e.get("added_at") or "", e.get("id") or ""))
    for entry in ordered:
        task_id = entry.get("task_id")
        task = tasks_by_id.get(task_id)
        if task_id is None or task is None:
            skipped.append(
                _skip(entry, task, REASON_TASK_MISSING, "queue entry references a task that no longer exists")
            )
            continue
        if task_id in entry_by_task:
            skipped.append(
                _skip(
                    entry,
                    task,
                    REASON_DUPLICATE_QUEUE_ENTRY,
                    f"another open queue entry ({entry_by_task[task_id]}) already represents task {task_id!r}",
                )
            )
            continue

        try:
            completion = runtime_db.get_completion_by_task(db_path, task_id)
            if completion is not None and completion.get(
                "completion_state"
            ) in _COMPLETION_LAUNCH_BLOCKING_STATES:
                skipped.append(
                    _skip(
                        entry,
                        task,
                        REASON_COMPLETION_IN_PROGRESS,
                        (
                            "latest completion "
                            f"{completion.get('run_id')!r} is "
                            f"{completion.get('completion_state')!r}"
                        ),
                    )
                )
                continue
            canonical = project_config.canonical_project_id(task.get("project"))
            cfg = project_configs.get(canonical, {})
            prep = launch_service.prepare_task_launch(task=task, project_config=cfg)
        except Exception as exc:  # noqa: BLE001 — one unusable entry must not abort adaptation
            skipped.append(_skip(entry, task, REASON_ADAPTATION_FAILED, str(exc)))
            continue

        if not prep.selection.path:
            skipped.append(
                _skip(entry, task, REASON_WORKSPACE_UNCONFIGURED, "no workspace is configured for this task")
            )
            continue
        if prep.decision == launch_service.LAUNCH_DECISION_BLOCKED:
            skipped.append(
                _skip(
                    entry,
                    task,
                    REASON_LAUNCH_BLOCKED,
                    "; ".join(prep.fatal_messages) or "workspace validation failed",
                )
            )
            continue
        if prep.decision == launch_service.LAUNCH_DECISION_NEEDS_CONFIRMATION:
            skipped.append(
                replace(
                    _skip(
                        entry,
                        task,
                        REASON_NEEDS_CONFIRMATION,
                        "; ".join(prep.validation.warnings) or "launch requires explicit confirmation",
                    ),
                    warnings=tuple(prep.validation.warnings),
                )
            )
            continue

        # The fully-resolved form — the exact string `execute_agent_launch_v2`
        # persists as `run.repository_path`, so the planner's workspace
        # exclusivity compares the same spelling the DB workspace lock does.
        workspace = prep.resolved_workspace or launch_service.resolved_workspace_path(prep.selection.path)
        attempts_made, last_state, last_failure_reason, last_completed_at = _run_history(db_path, task_id)
        allowed_agents, preferred_agent = _agent_constraints(task, canonical)
        adapted.append(
            scheduler.WorkItem(
                task_id=task_id,
                workspace=workspace,
                required_capabilities=scheduler.capabilities_for_task_type(
                    task.get("task_type") or "implementation"
                ),
                priority=task.get("priority") or "Medium",
                # Authorization is a set, not a pin. The scheduler can spread
                # work across every permitted compatible provider while a
                # forbidden provider can never be proposed.
                allowed_agents=allowed_agents,
                preferred_agent=preferred_agent,
                # A READY entry is by definition dependency-satisfied, but the
                # queue file is persisted state that could be stale relative to
                # `tasks.json`. Recomputing from the live graph is pure and
                # cheap, and means a stale READY label can never produce an
                # ASSIGN for a task whose dependencies have regressed.
                dependencies_met=not models.is_blocked(task, tasks_by_id),
                attempts_made=attempts_made,
                last_state=last_state,
                last_failure_reason=last_failure_reason,
                last_completed_at=last_completed_at,
                enqueued_at=entry.get("added_at"),
            )
        )
        entry_by_task[task_id] = entry.get("id")
        workspace_by_task[task_id] = workspace

    return AdaptedWave(
        work_items=tuple(adapted),
        entry_by_task=entry_by_task,
        workspace_by_task=workspace_by_task,
        skipped=tuple(skipped),
    )


def map_decisions(
    plan: scheduler.SchedulingPlan, wave: AdaptedWave, tasks_by_id: dict[str, dict]
) -> tuple[EntryDecision, ...]:
    """Re-key every `SchedulingDecision` onto the queue entry that produced it,
    preserving the planner's deterministic ordering, and append the entries that
    never reached the planner. This is the mapping the scheduler deliberately
    does not do itself (it is a pure function of `WorkItem`s and knows nothing
    about the queue)."""
    decisions: list[EntryDecision] = []
    for decision in plan.decisions:
        task = tasks_by_id.get(decision.task_id) or {}
        decisions.append(
            EntryDecision(
                entry_id=wave.entry_by_task.get(decision.task_id),
                task_id=decision.task_id,
                action=decision.action,
                reason_code=decision.reason_code,
                explanation=decision.explanation,
                title=task.get("title"),
                project=task.get("project"),
                priority=decision.priority,
                workspace=wave.workspace_by_task.get(decision.task_id),
                agent_id=decision.agent_id,
                executor_id=decision.executor_id,
                attempt=decision.attempt,
                next_eligible_at=decision.next_eligible_at,
            )
        )
    decisions.extend(wave.skipped)
    return tuple(decisions)


# --------------------------------------------------------------------------
# AICC-DESKTOP-010 — the auto-merge opt-in, applied to completion policy only
# --------------------------------------------------------------------------


def merge_policy_overrides(settings: PipelineSettings) -> dict:
    """The operator layer handed to `CompletionPolicy.resolve(overrides=...)`
    when a completion row is *seeded* (see `task_sync.sync_tasks`). Empty
    unless something is actively opted in, so the default remains whatever
    task/project configuration says — `manual` merge, no review gate."""
    overrides: dict = {}
    if settings.auto_merge_active:
        overrides["merge_mode"] = completion_domain.MERGE_AUTO_AFTER_CHECKS
    # The review flag is set explicitly both ways while autopilot is enabled: on
    # when the operator opts in, and OFF when they opt out, so a deliberate
    # "review off" genuinely forces the gate off instead of leaving a config
    # default (auto_after_checks_and_review) to re-require it. That default-driven
    # gate is what stalled runs at AWAITING_REVIEW -> auto REVIEW_REJECTED even
    # with the operator's review switch off.
    if settings.enabled:
        overrides["requires_independent_review"] = settings.require_independent_review
    return overrides


# Rows whose merge policy may still be reconciled with the current opt-in.
#
# `begin_completion` resolves policy once, at seeding, precisely so a row does
# not have the rules changed underneath it mid-pipeline. That is right for a row
# that has already *acted* on its policy — once a merge has been performed there
# is nothing left to re-decide, and re-judging it would be revisionist. It is
# wrong for a row that has not: with autopilot off by default, the operator
# almost always enables auto-merge while pull requests are already open, and a
# seed-time-only override would silently never reach them. So reconciliation
# applies to exactly the pre-merge active states, and `MERGED`/
# `VERIFYING_TARGET_BRANCH` are excluded.
_REPOLICIABLE_STATES: frozenset[str] = completion_domain.ACTIVE_STATES - {
    completion_domain.CompletionState.MERGED,
    completion_domain.CompletionState.VERIFYING_TARGET_BRANCH,
}


def _desired_merge_mode(
    row: dict, tasks_by_id: dict[str, dict], project_configs: dict[str, dict], *, opt_in: bool
) -> str:
    """The merge mode this completion row *should* carry right now: whatever
    task/project configuration resolves to, upgraded from `manual` to
    `auto_after_checks` only while the host-level opt-in is on.

    Deriving the value fresh every tick (rather than remembering "the pipeline
    upgraded this row once") is what makes the opt-in genuinely reversible: turn
    it off and the next tick writes the configured mode back, so a row can never
    keep auto-merging on the strength of a setting that is no longer enabled. A
    task or project that explicitly configures `auto_after_checks_and_review`
    keeps it — the opt-in only ever raises `manual`, never lowers or overrides an
    explicit stronger choice."""
    task = tasks_by_id.get(row.get("task_id")) or {}
    cfg = project_configs.get(project_config.canonical_project_id(row.get("project"))) or {}
    configured = CompletionPolicy.resolve(task=task, project_cfg=cfg).merge_mode
    if opt_in and configured == completion_domain.MERGE_MANUAL:
        return completion_domain.MERGE_AUTO_AFTER_CHECKS
    return configured


def apply_merge_policy(
    db_path: Path,
    tasks_by_id: dict[str, dict],
    project_configs: dict[str, dict],
    *,
    opt_in: bool,
) -> list[dict]:
    """Reconcile every pre-merge completion row's persisted merge *policy* with
    the current opt-in, and return one audit record per row actually changed.

    This function decides nothing about whether a PR may merge. It writes the
    policy only, except that a row upgraded from manual to automatic is made
    immediately due by clearing the manual wait's retry timer and counter.
    Every real gate — checks passing, a required review, mergeability,
    conflicts, the closed-unmerged recovery rules — stays where it already is,
    in `CompletionEvaluator`. A row whose policy already matches is left
    completely untouched (no write, no version bump, no audit event), which is
    what makes calling this twice per tick free."""
    updates: list[dict] = []
    rows = runtime_db.list_completions(
        db_path, states=sorted(_REPOLICIABLE_STATES), limit=COMPLETION_SCAN_LIMIT
    )
    for row in rows:
        desired = _desired_merge_mode(row, tasks_by_id, project_configs, opt_in=opt_in)
        policy = CompletionPolicy.from_json(row.get("policy_json"))
        if policy.merge_mode == desired and row.get("merge_mode") == desired:
            continue
        updated_policy = CompletionPolicy.from_dict({**policy.to_dict(), "merge_mode": desired})
        fields: dict[str, object] = {
            "merge_mode": desired,
            "policy_json": updated_policy.to_json(),
        }
        if (
            policy.merge_mode == completion_domain.MERGE_MANUAL
            and desired != completion_domain.MERGE_MANUAL
        ):
            # Manual waiting deliberately backs off for an hour. Retaining that
            # timer after an explicit auto-merge opt-in makes the switch appear
            # broken and also carries a large manual-poll retry count into the
            # automatic policy. The next completion advance must evaluate the
            # PR now, with a fresh retry budget.
            fields.update({"next_retry_at": None, "retry_count": 0})
        try:
            runtime_db.update_completion(
                db_path,
                row["run_id"],
                expected_version=row["version"],
                fields=fields,
            )
        except (runtime_db.LostUpdateError, KeyError):
            # A concurrent advancer moved (or removed) the row between the list
            # and the compare-and-set. Its progress is authoritative; the policy
            # is re-derived from scratch on the next tick anyway.
            continue
        runtime_db.append_completion_event(
            db_path,
            row["run_id"],
            EV_MERGE_POLICY_APPLIED,
            reason_code=MERGE_POLICY_EVENT_REASON,
            message=f"merge_mode {policy.merge_mode!r} -> {desired!r} (auto_merge opt-in={opt_in})",
            metadata={"from": policy.merge_mode, "to": desired, "opt_in": opt_in},
        )
        updates.append(
            {
                "run_id": row["run_id"],
                "task_id": row.get("task_id"),
                "from": policy.merge_mode,
                "to": desired,
            }
        )
    return updates


# --------------------------------------------------------------------------
# Rework: a failed validation becomes another attempt, not a dead end
# --------------------------------------------------------------------------

# Fields the pipeline owns on a task record, for a rework loop that is
# restartable and idempotent across ticks.
#
# `REWORK_BASE_PROMPT_FIELD` snapshots the task's original instruction the first
# time a rework rewrites `prompt`, and every later rework recomposes from that
# snapshot — so failure context never compounds across attempts (attempt 3 does
# not carry attempt 1's *and* attempt 2's output) and the operator's original
# wording is never lost.
#
# `REWORK_LAST_RUN_FIELD` records which failed completion row has already been
# turned into a rework. It is what makes the loop idempotent: the same
# `VALIDATION_FAILED` row is seen on every tick until a new run replaces it, and
# without this the budget would be consumed several times over for one failure.
REWORK_BASE_PROMPT_FIELD = "autopilot_prompt_base"
REWORK_LAST_RUN_FIELD = "autopilot_rework_run_id"
REWORK_COUNT_FIELD = "autopilot_rework_count"

# Rework outcome codes, reported per task in `PipelineTickResult.reworks`.
REWORK_REQUEUED = "requeued"
REWORK_BUDGET_EXHAUSTED = "rework_budget_exhausted"
REWORK_DISABLED = "auto_rework_disabled"

# Completion states that unconditionally mean "the work is not acceptable and
# a new agent attempt is the remedy".
REWORK_TRIGGER_STATES: frozenset[str] = frozenset(
    {
        completion_domain.CompletionState.VALIDATION_FAILED,
        # A rejected independent review is the same shape of problem as a
        # failed test: the work is not acceptable and another agent attempt is
        # the remedy. The reviewer's reasoning rides into the rework prompt via
        # `recommended_action`, so the next attempt knows what to fix.
        completion_domain.CompletionState.REVIEW_REJECTED,
    }
)


def _is_rework_trigger(row: dict) -> bool:
    state = row.get("completion_state")
    if state in REWORK_TRIGGER_STATES:
        return True
    # A red remote CI run is discovered after the PR exists, so it rests in
    # MERGE_BLOCKED rather than the terminal local VALIDATION_FAILED state.
    return (
        state == completion_domain.CompletionState.MERGE_BLOCKED
        and row.get("last_reason_code") == completion_domain.ReasonCode.CHECKS_FAILING
    )


def _rework_prompt(task: dict, row: dict) -> str:
    """The instruction for a rework attempt: the task's original objective, plus
    exactly what failed, in plain text.

    Composed from the *snapshot* of the original prompt, never from the previous
    rework's prompt, so the agent sees one clear objective and one current
    failure rather than an ever-growing transcript."""
    base = (task.get(REWORK_BASE_PROMPT_FIELD) or "").strip()
    summary = (row.get("validation_summary") or "").strip()
    recommended = (row.get("recommended_action") or "").strip()
    lines = [base, "", "## Доработка", ""]
    lines.append(
        "Это ПРОДОЛЖЕНИЕ, а не новый старт: изменения предыдущей попытки уже лежат в "
        "рабочем дереве. Сначала посмотри `git status` и уже созданные/изменённые файлы, "
        "прими сделанное как есть и продолжай с этого места — не переписывай с нуля. "
        "Предыдущая попытка не дошла до конца/не прошла проверку. Исправь причину и "
        "доведи задачу до состояния, в котором проверка проходит."
    )
    if summary:
        lines += ["", "### Что упало", "", summary]
    if recommended:
        lines += ["", "### Рекомендация", "", recommended]
    return "\n".join(lines).strip()


def plan_rework(
    root: Path,
    tasks_by_id: dict[str, dict],
    *,
    db_path: Path,
    settings: PipelineSettings,
) -> list[dict]:
    """Turn every task whose latest completion failed validation into another
    attempt, and return one audit record per task considered.

    The mechanism is deliberately *not* a new state machine. A rework is simply
    the same task, re-enqueued, with its prompt rewritten to carry the failure —
    so it flows through the identical readiness → plan → launch → validate path
    as the first attempt, is bounded by the same capacity and workspace
    exclusivity, and produces its own run and its own completion row. The
    history of what was tried is the run history, which already exists.

    Three independent guards stop it from looping:

    - the **opt-in** (`auto_rework_active`, which also requires auto-launch —
      a rework is a launch);
    - the **budget** (`max_rework_attempts`), counted per task and persisted;
    - **idempotency** — a given failed completion row is reworked at most once
      (`REWORK_LAST_RUN_FIELD`), so the several ticks that observe the same
      failure before the new run starts do not each spend a budget unit.

    A task that exhausts its budget is left exactly where it is: `Requires
    Attention`, with its failure recorded, for a human."""
    records: list[dict] = []
    if not settings.auto_rework_active or settings.max_rework_attempts <= 0:
        return records

    candidates: list[tuple[str, dict]] = []
    for task_id, task in tasks_by_id.items():
        if task.get("status") == "Done":
            continue
        row = runtime_db.get_completion_by_task(db_path, task_id)
        if row is None or not _is_rework_trigger(row):
            continue
        if task.get(REWORK_LAST_RUN_FIELD) == row.get("run_id"):
            continue  # already reworked this exact failure
        candidates.append((task_id, row))

    if not candidates:
        return records

    rows_by_task = dict(candidates)

    def _mutator(tasks: list[dict]) -> list[dict]:
        touched: list[dict] = []
        for task in tasks:
            row = rows_by_task.get(task.get("id"))
            if row is None:
                continue
            attempts = int(task.get(REWORK_COUNT_FIELD) or 0)
            if attempts >= settings.max_rework_attempts:
                records.append(
                    {
                        "task_id": task["id"],
                        "run_id": row.get("run_id"),
                        "outcome": REWORK_BUDGET_EXHAUSTED,
                        "attempts": attempts,
                    }
                )
                # Mark it handled so the budget message is emitted once, not on
                # every tick for the rest of the task's life.
                task[REWORK_LAST_RUN_FIELD] = row.get("run_id")
                touched.append(task)
                continue

            if not task.get(REWORK_BASE_PROMPT_FIELD):
                task[REWORK_BASE_PROMPT_FIELD] = (
                    task.get("prompt") or task.get("goal") or task.get("title") or ""
                )
            task["prompt"] = _rework_prompt(task, row)
            task[REWORK_COUNT_FIELD] = attempts + 1
            task[REWORK_LAST_RUN_FIELD] = row.get("run_id")
            task["updated_at"] = models.iso_now()
            models.append_timeline_event(
                task,
                "launch_requires_attention",
                f"Автопилот: проверка не прошла, назначена доработка (попытка {attempts + 1}).",
            )
            records.append(
                {
                    "task_id": task["id"],
                    "run_id": row.get("run_id"),
                    "outcome": REWORK_REQUEUED,
                    "attempts": attempts + 1,
                }
            )
            touched.append(task)
        return touched

    touched = tasks_repository.mutate_tasks(root, _mutator, persist_if=lambda t: bool(t))

    # Re-enqueue outside the task lock. `enqueue_and_persist` is idempotent for a
    # task that already has an open entry, so a task still sitting in the queue
    # is not duplicated.
    fresh = {t["id"]: t for t in tasks_repository.load_tasks(root) if t.get("id")}
    for task in touched:
        record = next((r for r in records if r["task_id"] == task["id"]), None)
        if record is None or record["outcome"] != REWORK_REQUEUED:
            continue
        for activity_task_id in (task["id"],):
            activity_log.log_event(
                EV_PIPELINE_REWORK,
                project=task.get("project"),
                task_id=activity_task_id,
                message=f"Автопилот назначил доработку (попытка {record['attempts']}).",
            )
        execution_queue.enqueue_and_persist(root, fresh.get(task["id"], task), fresh)
    return records


# --------------------------------------------------------------------------
# The blocking independent review gate
# --------------------------------------------------------------------------

REVIEW_REQUESTED = "review_requested"
REVIEW_RECORDED = "review_recorded"
REVIEW_FAILED = "review_failed"

# The reviewer runs as a `review` task type, which `agent_runner.
# profile_for_task_type` resolves to the read-only execution profile: the tool
# set handed to the process contains no `Bash`, `Edit`, `Write` or
# `NotebookEdit` at all. The reviewer therefore *cannot* modify the change it is
# judging — independence is enforced by the process contract, not by asking
# nicely in the prompt.
REVIEW_TASK_TYPE = "review"


def _review_instruction(task: dict, row: dict) -> str:
    branch = row.get("branch") or "—"
    base = row.get("base_branch") or "main"
    title = task.get("title") or row.get("task_id")
    return "\n".join(
        [
            f"Ты — независимый ревьюер. Проверь изменение в ветке `{branch}` относительно `{base}`.",
            "",
            f"## Задача: {title}",
            (task.get("goal") or task.get("prompt") or "").strip(),
            "",
            "## Что сделать",
            "",
            f"1. Изучи изменения: `git diff {base}...{branch}`.",
            "2. Оцени: решает ли изменение поставленную задачу; нет ли регрессий,",
            "   дыр в безопасности, потери данных; адекватны ли тесты.",
            "3. Ты работаешь в режиме только для чтения — ничего не правь.",
            "",
            "## Формат ответа",
            "",
            f"Последней строкой выведи ровно один вердикт: `{models.VERDICT_APPROVED_FOR_COMMIT}`",
            f"если изменение можно вливать, либо `{models.VERDICT_NOT_APPROVED_FOR_COMMIT}` если нет.",
            "Перед вердиктом кратко перечисли найденные проблемы.",
        ]
    ).strip()


def advance_reviews(api, tasks_by_id: dict[str, dict], *, settings: PipelineSettings) -> list[dict]:
    """Drive the blocking review gate: start a reviewer for every completion
    waiting on one, and record the verdict of every reviewer that has finished.

    This is the half of the gate the completion orchestrator deliberately cannot
    do. The orchestrator is the privileged actor that pushes and merges, so it
    never launches agents; it only parks a row in `AWAITING_REVIEW` and reads the
    verdict. Launching the reviewer and writing that verdict back is this
    function's job, which keeps "may start a process" and "may push to a remote"
    in separate modules.

    The reviewer is a normal run against the same workspace with task type
    `review` — the read-only execution profile. It is deliberately not attached
    to the task's `current_run_id`, so the Kanban projection keeps describing the
    *implementation* run and the reviewer never seeds a completion row of its own.

    Idempotent: a row already carrying a `review_run_id` is never given a second
    reviewer, and recording a verdict flips the row out of `AWAITING_REVIEW` on
    the next advance, so it is never recorded twice."""
    records: list[dict] = []
    if not settings.enabled:
        return records

    rows = runtime_db.list_completions(
        api.db_path,
        states=[completion_domain.CompletionState.AWAITING_REVIEW],
        limit=COMPLETION_SCAN_LIMIT,
    )
    for row in rows:
        task = tasks_by_id.get(row.get("task_id")) or {}
        review_run_id = row.get("review_run_id")

        # 1. A reviewer already exists — record its verdict once it is terminal.
        if review_run_id:
            review_run = api.get_run(review_run_id)
            if review_run is None or review_run.get("state") not in runtime_db.TERMINAL_STATES:
                continue  # still running; the gate stays closed
            records.append(_record_review_verdict(api, row, review_run))
            continue

        # 2. No reviewer yet — start one. Only on the auto-launch opt-in: a
        #    review is a process launch like any other.
        if not settings.auto_launch_active:
            continue
        try:
            run = api.start_run(
                project=row.get("project"),
                repository_path=row["repository_path"],
                task_type=REVIEW_TASK_TYPE,
                instruction=_review_instruction(task, row),
                confirmed=True,
                task_id=row.get("task_id"),
                title=f"Независимая проверка: {task.get('title') or row.get('task_id')}",
                launch_source="autopilot_review",
                repository_already_validated=True,
                max_global_concurrency=settings.max_global_concurrency,
            )
        except Exception as exc:  # noqa: BLE001 — one failed reviewer must not stop the rest
            records.append(
                {"task_id": row.get("task_id"), "outcome": REVIEW_FAILED, "detail": str(exc)}
            )
            continue

        try:
            runtime_db.update_completion(
                api.db_path,
                row["run_id"],
                expected_version=row["version"],
                fields={"review_run_id": run["id"]},
            )
        except (runtime_db.LostUpdateError, KeyError):
            continue
        records.append(
            {"task_id": row.get("task_id"), "outcome": REVIEW_REQUESTED, "review_run_id": run["id"]}
        )
        activity_log.log_event(
            EV_PIPELINE_REVIEW,
            project=row.get("project"),
            task_id=row.get("task_id"),
            run_id=run["id"],
            message="Автопилот запустил независимую проверку перед созданием PR.",
        )
    return records


def _record_review_verdict(api, row: dict, review_run: dict) -> dict:
    """Parse a finished reviewer's report and persist its verdict.

    The verdict is read with the same deterministic `report_parser.parse_report`
    the rest of the app uses and judged with `models.is_passing_verdict` — one
    definition of "good enough", never a second opinion embedded here.

    A reviewer that did not finish cleanly, or produced no recognizable verdict,
    is treated as **rejection**, never approval. That is the only safe default
    for a blocking gate: an unreadable review must not open a pull request."""
    events = runtime_db.list_run_events(api.db_path, review_run["id"], after_seq=0, limit=1_000_000)
    parsed = (
        report_parser.parse_report(reports.result_text(events))
        if events
        else report_parser.empty_parsed_result()
    )
    verdict = parsed.get("verdict")
    approved = review_run.get("state") == "COMPLETED" and models.is_passing_verdict(verdict)
    summary = (parsed.get("summary") or "").strip() or f"вердикт: {verdict or '—'}"
    try:
        runtime_db.update_completion(
            api.db_path,
            row["run_id"],
            expected_version=row["version"],
            fields={
                "review_verdict": (
                    completion_service.REVIEW_VERDICT_APPROVED
                    if approved
                    else completion_service.REVIEW_VERDICT_REJECTED
                ),
                "review_summary": summary[:2000],
            },
        )
    except (runtime_db.LostUpdateError, KeyError):
        return {"task_id": row.get("task_id"), "outcome": REVIEW_FAILED, "detail": "lost update"}
    return {
        "task_id": row.get("task_id"),
        "outcome": REVIEW_RECORDED,
        "approved": approved,
        "detail": summary[:400],
    }


# --------------------------------------------------------------------------
# Workspace remediation: make a task that would never start, start
# --------------------------------------------------------------------------

REMEDIATION_STASHED = "workspace_stashed"
REMEDIATION_REPOINTED = "workspace_repointed"
REMEDIATION_NOT_OWNED = "workspace_not_owned"
REMEDIATION_FAILED = "remediation_failed"

# Marker prefix on every stash this module creates, so an operator can find and
# restore them: `git stash list | grep autopilot`.
STASH_MESSAGE_PREFIX = "autopilot"

# Where a derived worktree path is placed when a feature task has been left
# pointing at its project's primary working tree. A sibling `<repo>-worktrees/`
# directory matches the layout this machine already uses
# (`ai-command-center-worktrees`, `portfolio-worktrees`) and is deliberately
# *outside* the repository, so a worktree can never appear as untracked noise
# inside the repo it belongs to.
WORKTREE_PARENT_SUFFIX = "-worktrees"


def derive_worktree_path(repository_path: str, branch: str) -> Path:
    """The dedicated worktree path for `branch` of `repository_path`.

    Deterministic, so the same task resolves to the same directory on every
    tick rather than accumulating one per run. `/` in a branch name becomes `-`
    (`task/a` -> `task-a`) because a nested directory per branch segment would
    make `feature/x` and `feature/x/y` collide as file-vs-directory."""
    repo = Path(repository_path).expanduser().resolve()
    slug = branch.strip().strip("/").replace("/", "-") or "work"
    return repo.parent / f"{repo.name}{WORKTREE_PARENT_SUFFIX}" / slug



def _worktree_holding_branch(repository_path: str, branch: str) -> str | None:
    """The resolved path of the worktree that currently has `branch` checked
    out, or `None`. Read-only; a git failure resolves to `None` so a missing or
    unreadable repository degrades to "derive a fresh path" rather than raising
    inside planning."""
    try:
        for entry in git_info.get_worktrees(Path(repository_path)):
            if entry.get("branch") != branch:
                continue
            path = entry.get("path")
            if path:
                return str(Path(path).expanduser().resolve())
    except Exception:  # noqa: BLE001 — planning must not fail on a git hiccup
        return None
    return None


def _repoint_to_own_worktree(
    task: dict, prep, *, repository_path: str | None
) -> tuple[str, str] | None:
    """`(new_workspace_path, detail)` when this task is a feature task that has
    been left resolving to its project's *primary* working tree, else `None`.

    This is the second half of "a task must not sit refused forever". The
    isolation gate rejects a feature-branch launch in the primary tree — rightly,
    since two agents in one tree corrupt each other — and with no
    `workspace_path` set the resolution order (`launch.resolve_workspace_path`)
    lands there by default. The repair is purely *additive*: the task is given
    its own worktree path, which the existing launch path then provisions and
    verifies. Nothing existing is moved, rewritten or deleted, and the primary
    tree is untouched.

    Refuses whenever the answer is not obvious: no repository configured, no
    feature branch, a workspace that is not actually the primary tree, or a
    derived path that already exists (adopting an existing directory would mean
    guessing that its contents are ours)."""
    if not repository_path:
        return None
    branch = prep.expected_branch
    base = prep.base_branch
    if not branch or not workspace_provisioning.is_feature_task(branch, base):
        return None

    workspace = prep.resolved_workspace or prep.selection.path
    if not workspace:
        return None
    try:
        current = Path(workspace).expanduser().resolve()
        current_str = str(current)
        repo = Path(repository_path).expanduser().resolve()
    except OSError:
        return None
    # A branch already checked out somewhere is the strongest constraint git
    # imposes, and it applies wherever the task currently points — not only
    # when it points at the primary tree. Checked before the "points elsewhere"
    # guard below, which would otherwise return early and leave the task
    # permanently refused by `no_conflicting_worktree`.
    existing = _worktree_holding_branch(repository_path, branch)
    if existing is not None and existing != current_str:
        return existing, (
            f"ветка «{branch}» уже выгружена в {existing}; задача направлена туда, "
            "второй worktree на ту же ветку git не допускает"
        )

    if current != repo:
        # Pointing somewhere else entirely, and its branch is free — that is a
        # configuration question this function must not answer by guessing.
        return None

    target = derive_worktree_path(repository_path, branch)
    if target.exists():
        return None
    return str(target), (
        f"задача с веткой «{branch}» указывала на основное дерево проекта; "
        f"назначен собственный worktree {target}"
    )


def remediate_workspaces(
    root: Path,
    tasks_by_id: dict[str, dict],
    project_configs: dict[str, dict],
    *,
    settings: PipelineSettings,
) -> list[dict]:
    """Tidy the workspaces of tasks that would otherwise be refused forever, and
    return one audit record per workspace touched.

    **What this does and, more importantly, what it refuses to do.**

    A dirty working tree makes `launch_ready` refuse a task every single tick —
    correctly, because auto-launching an agent on top of uncommitted changes can
    entangle them with the agent's own work. But when the workspace is a *linked
    worktree the pipeline itself provisioned for this task's feature branch*,
    those uncommitted changes are almost always leftovers from a previous
    attempt of the same task, and the refusal is a permanent dead end rather
    than a safety win. That narrow case is what this function repairs.

    The repair is `git stash push --include-untracked`. It is **recoverable** —
    the work is in `git stash list`, tagged with this task's id, and can be
    restored with `git stash pop`. This module never runs `git reset --hard`,
    never runs `git clean`, and never discards anything: an automatic action
    that can destroy work a person has not committed is not an acceptable
    trade for convenience, at any setting.

    The scope guard is `workspace_provisioning.is_pipeline_owned_worktree`,
    which fails closed. A primary working tree — the repository a human actually
    works in — is never touched, nor is any repository that is not the task's
    own project repository, nor a path whose ownership cannot be proven. Those
    are reported by `find_stuck_tasks` for a person to resolve, which is the
    right outcome: the pipeline does not get to guess about someone else's
    uncommitted work.
    """
    records: list[dict] = []
    if not settings.auto_remediate_workspace_active:
        return records

    repoints: dict[str, tuple[str, str]] = {}

    for task_id, task in sorted(tasks_by_id.items()):
        if task.get("status") == "Done":
            continue
        cfg = project_configs.get(project_config.canonical_project_id(task.get("project"))) or {}
        repository_path = cfg.get("repository_path")
        try:
            prep = launch_service.prepare_task_launch(task=task, project_config=cfg)
        except Exception:  # noqa: BLE001 — an unusable task is reported elsewhere
            continue

        # A feature task resolving to the project's primary working tree can
        # never pass the isolation gate. Checked before the dirty-tree case and
        # independently of the validation decision, because whether the primary
        # tree happens to be clean or on the right branch is irrelevant — the
        # launch is refused either way.
        repoint = _repoint_to_own_worktree(task, prep, repository_path=repository_path)
        if repoint is not None:
            repoints[task_id] = repoint
            continue

        if prep.decision != launch_service.LAUNCH_DECISION_NEEDS_CONFIRMATION:
            continue

        workspace = prep.resolved_workspace or prep.selection.path
        if not workspace:
            continue
        status = git_info.get_status(Path(workspace))
        if not status.get("dirty"):
            # A warning that is not dirtiness (detached HEAD, branch mismatch)
            # is a configuration question, not leftovers. Never auto-"fixed".
            continue

        if not workspace_provisioning.is_pipeline_owned_worktree(workspace, repository_path):
            records.append(
                {
                    "task_id": task_id,
                    "workspace": workspace,
                    "outcome": REMEDIATION_NOT_OWNED,
                    "detail": (
                        "рабочее дерево не является изолированным worktree проекта — "
                        "несохранённые изменения не трогаем"
                    ),
                }
            )
            continue

        message = f"{STASH_MESSAGE_PREFIX}: {task_id} {models.iso_now()}"
        result = git_info.run_git_command(
            Path(workspace), ["stash", "push", "--include-untracked", "-m", message]
        )
        if result is None or result.returncode != 0:
            records.append(
                {
                    "task_id": task_id,
                    "workspace": workspace,
                    "outcome": REMEDIATION_FAILED,
                    "detail": (getattr(result, "stderr", "") or "git stash не выполнен").strip()[:400],
                }
            )
            continue

        records.append(
            {
                "task_id": task_id,
                "workspace": workspace,
                "outcome": REMEDIATION_STASHED,
                "detail": message,
            }
        )
        activity_log.log_event(
            EV_PIPELINE_REMEDIATED,
            project=task.get("project"),
            task_id=task_id,
            message=f"Автопилот убрал остатки в worktree в stash ({message}); восстановить: git stash pop.",
        )

    if repoints:
        def _mutator(tasks: list[dict]) -> list[str]:
            changed: list[str] = []
            for task in tasks:
                repoint = repoints.get(task.get("id"))
                if repoint is None:
                    continue
                new_path, detail = repoint
                task["workspace_path"] = new_path
                task["updated_at"] = models.iso_now()
                models.append_timeline_event(task, "workspace_verified", f"Автопилот: {detail}")
                changed.append(task["id"])
            return changed

        for task_id in tasks_repository.mutate_tasks(root, _mutator, persist_if=lambda c: bool(c)):
            new_path, detail = repoints[task_id]
            records.append(
                {
                    "task_id": task_id,
                    "workspace": new_path,
                    "outcome": REMEDIATION_REPOINTED,
                    "detail": detail,
                }
            )
            activity_log.log_event(
                EV_PIPELINE_REMEDIATED,
                project=(tasks_by_id.get(task_id) or {}).get("project"),
                task_id=task_id,
                message=f"Автопилот назначил задаче собственный worktree: {new_path}",
            )
    return records


# --------------------------------------------------------------------------
# Nothing silently stuck: every task is either progressing or reported
# --------------------------------------------------------------------------

# Completion states the pipeline will not advance out of on its own *and* that
# are not success. A row here has stopped for good until a person or a rework
# moves it — which is precisely the condition that must never go unnoticed.
STUCK_COMPLETION_STATES: frozenset[str] = completion_domain.TERMINAL_STATES - {
    completion_domain.CompletionState.COMPLETED
}

# Kanban launch statuses that mean the last thing this task did was fail.
STUCK_LAUNCH_STATUSES: frozenset[str] = frozenset({"Failed", "Blocked", "Requires Attention"})

STUCK_KIND_COMPLETION = "completion_stopped"
STUCK_KIND_LAUNCH = "launch_failed"
STUCK_KIND_NOT_STARTING = "never_starts"
STUCK_KIND_REWORK_EXHAUSTED = "rework_exhausted"


@dataclass(frozen=True)
class StuckTask:
    """A task that is neither `Done` nor moving, with the reason it stopped.

    The point of this type is that "unfinished" and "errored" must be a
    *computed, reported* set rather than something an operator has to notice by
    scrolling. A task can stop in four distinguishable ways, and they need
    different answers:

    - `completion_stopped` — the engineering lifecycle hit a terminal non-success
      state (validation failed, needs attention, recovery failed);
    - `launch_failed` — the last run ended badly and nothing is running now;
    - `never_starts` — it is in the queue and eligible, but every tick refuses
      to launch it for the same structural reason (a dirty worktree that is
      never cleaned, a workspace that does not belong to its project). This one
      is the genuinely invisible case: without this report the task just sits in
      the queue looking fine while nothing ever happens to it;
    - `rework_exhausted` — automatic rework spent its budget and stopped.
    """

    task_id: str
    title: str | None
    project: str | None
    kind: str
    reason_code: str
    detail: str = ""

    @property
    def remediation(self) -> str:
        return remediation_for(self.reason_code)

    def as_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "project": self.project,
            "kind": self.kind,
            "reason_code": self.reason_code,
            "detail": self.detail,
            "remediation": self.remediation,
        }


def find_stuck_tasks(
    tasks: list[dict],
    decisions: tuple[EntryDecision, ...],
    *,
    db_path: Path,
    settings: PipelineSettings,
    active_task_ids: frozenset[str],
) -> tuple[StuckTask, ...]:
    """Every task that has stopped without reaching `Done`, deduplicated and
    ordered deterministically by task id.

    Read-only: this reports, it never repairs. A task with a run in flight is
    never reported — it is moving, by definition — which is why the live
    `active_task_ids` set is required rather than inferred from task fields that
    may lag behind the run table."""
    stuck: dict[str, StuckTask] = {}

    def _add(task: dict, kind: str, reason_code: str, detail: str = "") -> None:
        task_id = task.get("id")
        if not task_id or task_id in stuck:
            return
        stuck[task_id] = StuckTask(
            task_id=task_id,
            title=task.get("title"),
            project=task.get("project"),
            kind=kind,
            reason_code=reason_code,
            detail=detail,
        )

    by_id = {t["id"]: t for t in tasks if t.get("id")}

    # 1. A decision that refused to launch for a structural reason. A transient
    #    DEFER (capacity, workspace busy, backoff) is explicitly *not* stuck —
    #    it clears on its own, and reporting it would drown the real cases.
    for decision in decisions:
        if decision.launched or not decision.task_id:
            continue
        task = by_id.get(decision.task_id)
        if task is None or task.get("status") == "Done":
            continue
        blocked_structurally = decision.action in (scheduler.ACTION_BLOCKED, ACTION_SKIPPED)
        refused_at_launch = bool(decision.launch_reason_code) and not decision.launched
        if blocked_structurally or refused_at_launch:
            _add(
                task,
                STUCK_KIND_NOT_STARTING,
                decision.launch_reason_code or decision.reason_code,
                decision.launch_message or decision.explanation,
            )

    # 2. Completion stopped, or rework gave up.
    for task in tasks:
        task_id = task.get("id")
        if not task_id or task.get("status") == "Done" or task_id in active_task_ids:
            continue
        row = runtime_db.get_completion_by_task(db_path, task_id)
        if row is not None and row.get("completion_state") in STUCK_COMPLETION_STATES:
            exhausted = (
                row.get("completion_state") in REWORK_TRIGGER_STATES
                and settings.auto_rework_active
                and int(task.get(REWORK_COUNT_FIELD) or 0) >= settings.max_rework_attempts
            )
            _add(
                task,
                STUCK_KIND_REWORK_EXHAUSTED if exhausted else STUCK_KIND_COMPLETION,
                row.get("last_reason_code") or row.get("completion_state") or "",
                row.get("recommended_action") or "",
            )
            continue
        # 3. No completion row, but the last run ended badly.
        if task.get("launch_status") in STUCK_LAUNCH_STATUSES:
            _add(task, STUCK_KIND_LAUNCH, str(task.get("launch_status")), "")

    return tuple(stuck[task_id] for task_id in sorted(stuck))


# --------------------------------------------------------------------------
# AICC-DESKTOP-011 — a verified merge is what makes a task Done
# --------------------------------------------------------------------------


def project_verified_completions(root: Path, *, db_path: Path) -> list[str]:
    """Move every task whose completion pipeline reached the terminal
    `COMPLETED` state — i.e. the merge is *verified present in the target
    branch*, not merely reported by GitHub — into the Kanban `Done` status, and
    return the task ids moved.

    This is the step that closes the loop: Kanban `status` is what
    `models.unmet_dependencies` reads, so until it flips to `Done` a merged task
    never unblocks its dependents and the next wave never appears. The rule
    itself lives in `task_sync.project_completion_to_kanban` (one definition of
    "Done means verified"); this function only supplies it with the fresh task
    list and the locked write, via `tasks_repository.mutate_tasks` — never a
    direct JSON replacement. Idempotent: a task already `Done` is skipped, so
    repeated ticks neither rewrite it nor re-emit its timeline event."""

    def _mutator(tasks: list[dict]) -> tuple[list[str], bool]:
        moved: list[str] = []
        changed = False
        for task in tasks:
            task_id = task.get("id")
            if not task_id:
                continue
            row = runtime_db.get_completion_by_task(db_path, task_id)
            if row is None:
                continue
            # The completion row can belong to an earlier successful run while
            # a later verification/rework attempt is the task's current run.
            # Project the full completion read-model before the Kanban lane so
            # `Done` can never coexist with stale "Incomplete", 40%, or
            # "Implementation" fields in the live UI.
            execution_projection_changed = task_sync.sync_task_from_completion(task, row)
            kanban_changed = task_sync.project_completion_to_kanban(task, row)
            if kanban_changed:
                moved.append(task_id)
            if execution_projection_changed or kanban_changed:
                changed = True
        return moved, changed

    result = tasks_repository.mutate_tasks(
        root, _mutator, persist_if=lambda value: value[1]
    )
    return result[0]


def sync_on_refresh(root: Path, api) -> tuple[list[dict], list[str]]:
    """The data work of one dashboard refresh tick, independent of Streamlit so
    it can be tested and reused.

    Two steps, in order: (1) reconcile live runs and re-project execution state
    onto every task (`task_sync.reconcile_and_sync`), then (2) project every
    *verified* completion onto the Kanban `Done` lane
    (`project_verified_completions`).

    Step 2 previously ran only inside the desktop-autopilot tick, so a merge that
    was verified present in the target branch never reached `Done` unless
    autopilot (default-off) was enabled — leaving a genuinely merged task stuck
    in Backlog with its dependents blocked (audit DATA-D2). Both steps are
    idempotent and only persist on change, so the always-on refresh can call this
    on every tick regardless of autopilot.

    Returns the freshly loaded task list and the ids moved to `Done` this tick."""

    def _sync_mutator(fresh_tasks: list[dict]) -> tuple[list[dict], list[dict]]:
        return fresh_tasks, task_sync.reconcile_and_sync(api, fresh_tasks)

    tasks, _mutated = tasks_repository.mutate_tasks(
        root, _sync_mutator, persist_if=lambda result: bool(result[1])
    )
    moved = project_verified_completions(root, db_path=api.db_path)
    if moved:
        tasks = tasks_repository.load_tasks(root)
    return tasks, moved


def unverified_done_ids(root: Path, *, db_path: Path) -> list[str]:
    """Ids of tasks the board shows as `Done` that are NOT backed by a verified
    (engine-`COMPLETED`) completion.

    A task reaches `Done` three ways the board cannot tell apart (audit DATA-D1):
    the gated projection of a completion whose merge is verified present in the
    target branch, an operator's manual lane change, and a verbatim import. This
    returns exactly the `Done` tasks whose completion has not reached
    `COMPLETED` — a manual/administrative close, or a merge only reported but
    never verified — so the UI can mark them 'not verified' rather than render an
    unverified close as an indistinguishable real merged result."""
    unverified: list[str] = []
    for task in tasks_repository.load_tasks(root):
        if task.get("status") != "Done":
            continue
        task_id = task.get("id")
        if not task_id:
            continue
        row = runtime_db.get_completion_by_task(db_path, task_id)
        if (
            row is None
            or row.get("completion_state") != completion_domain.CompletionState.COMPLETED
        ):
            unverified.append(task_id)
    return unverified


def mutate_moved(root: Path, mutator) -> list[str]:
    """`tasks_repository.mutate_tasks` with a persist-only-if-something-moved
    guard, so an idle tick costs an (uncontended) lock acquisition but no disk
    write."""
    return tasks_repository.mutate_tasks(root, mutator, persist_if=lambda moved: bool(moved))


# --------------------------------------------------------------------------
# AICC-DESKTOP-001 / 006 / 007 / 008 / 009 / 012 — the tick
# --------------------------------------------------------------------------


def _plan_wave(
    root: Path,
    api,
    tasks: list[dict],
    project_configs: dict[str, dict],
    settings: PipelineSettings,
    *,
    now: str,
) -> tuple[tuple[EntryDecision, ...], AdaptedWave, dict[str, dict]]:
    """Refresh queue readiness, adapt the READY entries, and plan — read-only
    from the launcher's point of view (nothing here starts a process). Returns
    the mapped decisions, the adaptation (for the entry-id mapping) and the
    task index the caller should keep using."""
    tasks_by_id = {t["id"]: t for t in tasks if t.get("id")}
    entries = execution_queue.reevaluate_and_persist(root, tasks_by_id)
    ready = execution_queue.ready_entries(entries)
    wave = adapt_ready_entries(ready, tasks_by_id, project_configs, db_path=api.db_path)
    plan = api.plan_schedule(
        list(wave.work_items),
        registry=scheduler.default_registry(max_concurrency=settings.max_agent_concurrency),
        config=scheduler.SchedulerConfig(max_global_concurrency=settings.max_global_concurrency),
        # Without this the planner used its own default budget and the operator
        # had no way to grant a further attempt to a task whose earlier ones
        # were consumed by an external fault.
        policy=scheduler.RetryPolicy(max_attempts=settings.max_run_attempts),
        now=now,
    )
    return map_decisions(plan, wave, tasks_by_id), wave, tasks_by_id


def _dispatch(
    root: Path,
    api,
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
    project_configs: dict[str, dict],
    decisions: tuple[EntryDecision, ...],
    settings: PipelineSettings,
) -> tuple[tuple[EntryDecision, ...], str]:
    """Launch exactly the `ASSIGN` decisions, through `execution_queue.
    launch_ready` and nothing else, then fold each per-entry outcome back onto
    its decision.

    `launch_ready` is handed the explicit `entry_ids` of the assignments — never
    called in its "launch everything READY" mode — so a READY entry the planner
    deferred (capacity, workspace busy, backoff) is never started as a side
    effect. It already isolates a single entry's failure and preserves every
    pre-flight gate; the batch as a whole is additionally isolated here so a
    lock timeout or an unexpected fault degrades the tick to "nothing launched,
    reason recorded" instead of aborting it."""
    assigned = [d for d in decisions if d.action == scheduler.ACTION_ASSIGN and d.entry_id]
    if not assigned:
        return decisions, TICK_RAN

    entries = execution_queue.load_queue(root)
    try:
        _, results = execution_queue.launch_ready(
            root,
            entries,
            tasks,
            tasks_by_id,
            project_configs,
            api,
            entry_ids=[d.entry_id for d in assigned],
            executor_by_entry={
                d.entry_id: d.executor_id
                for d in assigned
                if d.entry_id and d.executor_id
            },
            timeout_seconds=settings.run_timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 — a failed batch must not abort the tick
        return decisions, f"{LAUNCH_BATCH_FAILED}: {exc}"

    # `launch_ready` mutates the task dicts in place (prompt history, launch
    # status, current_run_id); persist them through the locked repository, not
    # by writing the caller's snapshot back over the store.
    tasks_repository.upsert_tasks(root, tasks)

    by_entry = {r.entry_id: r for r in results}
    folded: list[EntryDecision] = []
    for decision in decisions:
        result = by_entry.get(decision.entry_id)
        if result is None:
            folded.append(decision)
            continue
        folded.append(
            replace(
                decision,
                launched=result.launched,
                run_id=result.run_id,
                launch_reason_code=result.reason_code,
                launch_message=result.message,
                warnings=tuple(result.warnings),
            )
        )
        if result.launched:
            activity_log.log_event(
                EV_PIPELINE_LAUNCHED,
                project=decision.project,
                task_id=decision.task_id,
                run_id=result.run_id,
                message=f"Автопилот запустил задачу на агенте {decision.agent_id} (попытка {decision.attempt}).",
            )
        else:
            activity_log.log_event(
                EV_PIPELINE_SKIPPED,
                project=decision.project,
                task_id=decision.task_id,
                message=f"Автопилот не запустил задачу ({result.reason_code}): {result.message}",
            )
    return tuple(folded), TICK_RAN


def tick(
    root: Path,
    api,
    project_configs: dict[str, dict],
    *,
    settings: PipelineSettings | None = None,
    now: str | None = None,
    github=None,
    advance_wait_seconds: float = ADVANCE_WAIT_SECONDS,
) -> PipelineTickResult:
    """One bounded, idempotent pass of the desktop pipeline.

    The order is the point, and each step exists because the one before it can
    create work for it:

    1. **reconcile** — repair every persisted `RUNNING` row whose process died,
       once, so everything below reads truthful execution state;
    2. **reconcile merge policy** — bring existing completion rows in line with
       the *current* opt-in before anything advances on a stale policy;
    3. **advance completions** — pre-existing rows first, so anything already
       mid-flight (validating, PR open, merged-not-yet-verified) moves before we
       read state from it;
    4. **sync** — project run state onto tasks and *seed* a completion row for
       every newly-terminal successful run (with the opt-in as its birth policy);
    5. **re-reconcile policy + advance again** — so a row seeded in step 4 can
       still make progress on this same tick rather than idling until the next
       one (AICC-DESKTOP-009);
    6. **sync again** — so the states reached in step 5 (in particular terminal
       `COMPLETED`) are projected onto their tasks now, not next tick;
    7. **project verified completions to `Done`** — the only thing that unblocks
       dependents (`models.unmet_dependencies` reads Kanban status);
    8. **plan** — refresh queue readiness against those newly-Done tasks and ask
       `scheduler.plan` for the capacity- and workspace-safe wave;
    9. **dispatch** — launch only the `ASSIGN` decisions, only when auto-launch
       is actively opted in;
    10. **re-plan** — return the wave as it stands *after* dispatch, which is
        what the operator needs to see.

    Everything from step 1 to step 10 runs inside the advisory `pipeline_lock`,
    so two desktop sessions (or a session and a scheduled tick) can never plan
    against the same free capacity and both dispatch into it. A tick that cannot
    take the lock returns immediately with `status == TICK_BUSY` rather than
    queueing behind the holder — the holder is doing the identical work.

    Steps 2, 3, 5 and 7 are individually isolated: a transient GitHub/git fault
    during a completion advance is recorded in `errors` and the tick proceeds to
    plan and launch unrelated tasks (AICC-DESKTOP-012).

    `github` is forwarded verbatim to `advance_completions` (which constructs
    the orchestrator); leaving it `None` uses the real `GitHubClient`. It exists
    so a test — or a headless host with its own authenticated client — can
    substitute one, never so a caller can weaken a merge gate: the gates live in
    `CompletionEvaluator` and are unaffected by which client is in use."""
    started_at = now or models.iso_now()
    settings = settings if settings is not None else pipeline_settings.load_settings(root)

    if not settings.enabled:
        return PipelineTickResult(
            started_at=started_at,
            finished_at=models.iso_now(),
            settings=settings,
            ran=False,
            status=TICK_DISABLED,
        )

    try:
        with pipeline_lock(root):
            return _locked_tick(
                root,
                api,
                project_configs,
                settings,
                started_at=started_at,
                github=github,
                advance_wait_seconds=advance_wait_seconds,
            )
    except storage.LockTimeoutError:
        return PipelineTickResult(
            started_at=started_at,
            finished_at=models.iso_now(),
            settings=settings,
            ran=False,
            status=TICK_BUSY,
        )


def _locked_tick(
    root: Path,
    api,
    project_configs: dict[str, dict],
    settings: PipelineSettings,
    *,
    started_at: str,
    github=None,
    advance_wait_seconds: float = ADVANCE_WAIT_SECONDS,
) -> PipelineTickResult:
    errors: list[str] = []
    advances: list[dict] = []
    merge_updates: list[dict] = []
    overrides = merge_policy_overrides(settings)
    worker = _advance_worker(api.db_path)

    def _record(exc: Exception, step: str) -> None:
        errors.append(f"{step}: {exc}")

    def _sync() -> list[dict]:
        """Project run/completion state onto a *freshly loaded* task list inside
        the repository's own lock, persisting only when something changed.
        `sync_tasks` (not `reconcile_and_sync`) because this tick reconciles
        once, up front, and must not pay for a second `Supervisor.reconcile()`
        on every sync."""

        def _mutator(fresh_tasks: list[dict]) -> tuple[list[dict], list[dict]]:
            return fresh_tasks, task_sync.sync_tasks(api, fresh_tasks, policy_overrides=overrides)

        tasks, _mutated = tasks_repository.mutate_tasks(
            root, _mutator, persist_if=lambda result: bool(result[1])
        )
        return tasks

    def _advance(step: str) -> None:
        """Kick off (or keep waiting on) the off-thread completion advance, then
        report whatever has finished — including results left behind by an
        advance a *previous* tick started. Never raises: a failure inside the
        worker is drained as an error string, so a broken `gh` or a failing
        validation command degrades this tick instead of aborting it."""
        worker.start(api, github=github, limit=COMPLETION_ADVANCE_LIMIT)
        worker.join(advance_wait_seconds)
        results, error = worker.drain()
        advances.extend(results)
        if error:
            errors.append(f"{step}: {error}")

    def _merge_policy(tasks: list[dict], step: str) -> None:
        try:
            merge_updates.extend(
                apply_merge_policy(
                    api.db_path,
                    {t["id"]: t for t in tasks if t.get("id")},
                    project_configs,
                    opt_in=settings.auto_merge_active,
                )
            )
        except Exception as exc:  # noqa: BLE001
            _record(exc, step)

    # 1. Reconcile once — every later step reads truthful execution state.
    try:
        api.reconcile()
    except Exception as exc:  # noqa: BLE001
        _record(exc, "reconcile")

    # 2-4. Current policy first, then advance, then sync (which seeds new rows).
    tasks = tasks_repository.load_tasks(root)
    _merge_policy(tasks, "merge_policy_pre")
    _advance("advance_pre")
    tasks = _sync()

    # 5-6. A row seeded by the sync above is brand new; make sure its policy
    #      matches this tick's opt-in, give it one advance, and project the
    #      result immediately rather than a tick later.
    _merge_policy(tasks, "merge_policy_post")

    # 5b. The blocking review gate: start reviewers for rows parked in
    #     AWAITING_REVIEW and record the verdict of any that finished. Placed
    #     between the two advances so a verdict recorded here is acted on by the
    #     advance below, on this same tick.
    reviews: list[dict] = []
    try:
        reviews = advance_reviews(
            api, {t["id"]: t for t in tasks if t.get("id")}, settings=settings
        )
    except Exception as exc:  # noqa: BLE001
        _record(exc, "advance_reviews")

    _advance("advance_post")
    tasks = _sync()

    # 6b. Re-enqueue tasks sent back to "Ready" by the executor-fallback
    #     auto-retry (AICC-DESKTOP-017). `sync_task_from_run` records the dead
    #     executor in `failed_executors` and flips `launch_status` to "Ready"
    #     when a run died on startup (no output, e.g. expired OAuth). The queue
    #     entry for that run is still `launched`, so without a re-enqueue the
    #     planner would never see the task again. `enqueue_and_persist` is
    #     idempotent — it only creates a new entry when no open one exists.
    reenqueued: list[str] = []
    try:
        tasks_by_id_sync = {t["id"]: t for t in tasks if t.get("id")}
        for task in tasks:
            if (
                (task.get("failed_executors") or task.get("relaunch_requested"))
                and task.get("launch_status") == "Ready"
                and task.get("status") != "Done"
            ):
                execution_queue.enqueue_and_persist(
                    root, task, tasks_by_id_sync
                )
                if task.pop("relaunch_requested", None):
                    tasks_repository.save_tasks(root, tasks)
                reenqueued.append(task["id"])
    except Exception as exc:  # noqa: BLE001
        _record(exc, "reenqueue_executor_retry")
    if reenqueued:
        tasks = tasks_repository.load_tasks(root)

    # 7. Verified merge -> Kanban Done -> dependents become eligible.
    completed_task_ids: list[str] = []
    try:
        completed_task_ids = project_verified_completions(root, db_path=api.db_path)
    except Exception as exc:  # noqa: BLE001
        _record(exc, "project_completions")
    for task_id in completed_task_ids:
        activity_log.log_event(
            EV_PIPELINE_COMPLETED,
            task_id=task_id,
            message="Автопилот: merge подтверждён в целевой ветке, задача переведена в Done.",
        )
    if completed_task_ids:
        tasks = tasks_repository.load_tasks(root)

    # 7b. A failed validation becomes another attempt rather than a dead end.
    #     Runs before planning so a re-enqueued rework is part of *this* wave.
    reworks: list[dict] = []
    try:
        reworks = plan_rework(
            root,
            {t["id"]: t for t in tasks if t.get("id")},
            db_path=api.db_path,
            settings=settings,
        )
    except Exception as exc:  # noqa: BLE001
        _record(exc, "plan_rework")
    if reworks:
        tasks = tasks_repository.load_tasks(root)

    # 7c. Tidy workspaces the pipeline owns, so a task that would be refused
    #     every tick can actually start. Never touches a human's working tree.
    remediations: list[dict] = []
    try:
        remediations = remediate_workspaces(
            root, {t["id"]: t for t in tasks if t.get("id")}, project_configs, settings=settings
        )
        if remediations:
            tasks = tasks_repository.load_tasks(root)
    except Exception as exc:  # noqa: BLE001
        _record(exc, "remediate_workspaces")

    # 7d. ADR 0007 dual-write health. Read-only and never fatal: JSON is still
    #     authoritative, so a divergence means the mirror is behind, not that
    #     the queue is wrong. Surfaced rather than logged because "stop writing
    #     JSON" is gated on a session with none, and that is a claim the
    #     operator must be able to see.
    divergence: tuple[dict, ...] = ()
    try:
        divergence = tuple(execution_queue.queue_divergence(root))
    except Exception as exc:  # noqa: BLE001
        _record(exc, "queue_divergence")

    # 8. Plan the wave.
    decisions, _wave, tasks_by_id = _plan_wave(
        root, api, tasks, project_configs, settings, now=models.iso_now()
    )

    # 9. Dispatch — only ASSIGN decisions, only on an active explicit opt-in,
    #    and only while the daily spend budget (when set) has headroom. The
    #    budget gates NEW launches exclusively: running work, completions and
    #    merges continue — stopping mid-flight work is the kill switch's job.
    spend_budget_exhausted = False
    # Known/unknown, not folded into one bit: "exhausted" is a verdict about a
    # *known* spend figure, "unknown" is the honest report that the figure
    # itself could not be trusted (`SpendUnknownError`, e.g. a corrupt cost
    # event or an overflowed total) or read at all (any other exception —
    # a DB outage). Both fail closed the same way, but a diagnostic branch
    # keeps them from being reported as the same thing: collapsing them
    # previously made a corrupt cost event masquerade as a real budget cap
    # (VOYN-W0-AICC-REPORT-319).
    spend_status_unknown = False
    if settings.auto_launch_active and settings.max_daily_spend_usd > 0:
        try:
            spend = daily_spend_usd(api.db_path)
        except SpendUnknownError as exc:
            _record(exc, f"daily_spend_budget:{exc.reason}")
            spend_status_unknown = True
        except Exception as exc:  # noqa: BLE001 — DB unreadable: fail closed too
            _record(exc, "daily_spend_budget")
            spend_status_unknown = True
        else:
            spend_budget_exhausted = spend >= settings.max_daily_spend_usd
    if settings.auto_launch_active and not spend_budget_exhausted and not spend_status_unknown:
        decisions, launch_status = _dispatch(
            root, api, tasks, tasks_by_id, project_configs, decisions, settings
        )
    elif spend_status_unknown:
        launch_status = LAUNCH_SPEND_UNKNOWN
    else:
        launch_status = (
            LAUNCH_BUDGET_EXHAUSTED if spend_budget_exhausted else LAUNCH_DISABLED
        )

    # 9b. Nothing silently stuck: compute, from the post-dispatch state, every
    #     task that has stopped without reaching Done. Read-only.
    stuck: tuple[StuckTask, ...] = ()
    try:
        stuck = find_stuck_tasks(
            tasks_repository.load_tasks(root),
            decisions,
            db_path=api.db_path,
            settings=settings,
            active_task_ids=scheduler.build_load_snapshot(api.db_path).active_task_ids,
        )
    except Exception as exc:  # noqa: BLE001
        _record(exc, "find_stuck_tasks")

    # 10. Re-plan so the caller sees the wave that follows this dispatch.
    next_wave, _next_adapted, _next_index = _plan_wave(
        root, api, tasks_repository.load_tasks(root), project_configs, settings, now=models.iso_now()
    )

    return PipelineTickResult(
        started_at=started_at,
        finished_at=models.iso_now(),
        settings=settings,
        ran=True,
        status=TICK_RAN,
        decisions=decisions,
        next_wave=next_wave,
        launch_status=launch_status,
        completion_advances=tuple(advances),
        merge_policy_updates=tuple(merge_updates),
        reworks=tuple(reworks),
        reviews=tuple(reviews),
        queue_divergence=divergence,
        remediations=tuple(remediations),
        stuck=stuck,
        completed_task_ids=tuple(completed_task_ids),
        errors=tuple(errors),
    )


# ---------------------------------------------------------------------------
# Background sync driver (audit MAJOR-8) — opt-in, off by default.
#
# The run->task Kanban projection is normally driven by an open Live Execution
# Center page's refresh (see `app.py`'s `_maybe_run_autopilot_tick`), so with no
# page open the projection freezes until someone looks again. That is fine for an
# interactive operator — the board reconciles the instant it is opened — but
# leaves a headless / unattended host (the same audience as
# `AICC_COMPLETION_AUTOPILOT`) with stale task state. This optional daemon runs
# one `tick` every `interval_seconds` so a backend keeps task state truthful with
# nobody watching. The interactive app never starts it; a host opts in via
# `AICC_BACKGROUND_SYNC` (see `app.py`'s `get_execution_center_api`).
# ---------------------------------------------------------------------------

_BACKGROUND_SYNC_INTERVAL_SECONDS = 15.0
_background_sync_thread: threading.Thread | None = None
_background_sync_stop: threading.Event | None = None


def start_background_sync(
    root: Path,
    api,
    project_configs_provider: Callable[[], dict[str, dict]],
    *,
    interval_seconds: float = _BACKGROUND_SYNC_INTERVAL_SECONDS,
) -> None:
    """Start a bounded, daemon background poller that runs one `tick` every
    `interval_seconds`, keeping the run->task projection current even when no
    page is open to drive it (audit MAJOR-8).

    Idempotent: a second call while one is already running is a no-op. Reuses
    `tick`'s host-wide `pipeline_lock`, so this poller and a live page's tick can
    never interleave — whichever takes the lock does the work, the other returns
    `TICK_BUSY` and skips. `project_configs_provider` is called fresh on every
    tick (not captured once), so a project-config edit is picked up without a
    restart. One bad tick is swallowed so a single fault never kills the poller —
    the same contract as `Supervisor.start_completion_autopilot`."""
    global _background_sync_thread, _background_sync_stop
    if _background_sync_thread is not None and _background_sync_thread.is_alive():
        return
    stop = threading.Event()
    _background_sync_stop = stop

    def _loop() -> None:
        while not stop.wait(interval_seconds):
            try:
                tick(root, api, project_configs_provider())
            except Exception:  # noqa: BLE001 - one bad tick must never kill the poller
                continue

    thread = threading.Thread(target=_loop, name="aicc-background-sync", daemon=True)
    _background_sync_thread = thread
    thread.start()


def stop_background_sync() -> None:
    """Signal the background sync poller (if running) to stop after its current
    wait. The thread is `daemon=True` and also dies with the process; this is the
    graceful path used by tests and an orderly shutdown."""
    if _background_sync_stop is not None:
        _background_sync_stop.set()


def kill_switch(root: Path, api, *, confirmed: bool) -> dict:
    """One-action emergency stop for the desktop autopilot
    (NIGHT-W7-AICC-AUTONOMY).

    Two effects, in fail-closed order:

    1. **Persist the master switch off** (`pipeline_settings.enabled=False`)
       *first*, so every subsequent tick — in this process or any other —
       refuses to launch even if step 2 is interrupted mid-way.
    2. **Cancel every actively supervised run** in this process instance
       (SIGTERM, then SIGKILL after the grace period — the supervisor's
       ordinary confirmed-cancellation path; working trees are left intact).

    Runs supervised by a *different* process instance cannot be signalled
    from here (no Popen handle — see `Supervisor.cancel`); they are reported
    under ``not_cancellable`` rather than silently ignored, and the disabled
    master switch guarantees they are never followed by new launches.

    Returns a truthful report: ``{"disabled": bool, "cancelled": [run_id],
    "cancel_errors": {run_id: str}, "not_cancellable": [run_id]}``.
    """
    from command_center.runtime import context_service

    context_service.require_launch_confirmation(confirmed, what="Kill switch")

    settings = pipeline_settings.load_settings(root)
    if settings.enabled:
        pipeline_settings.save_settings(
            root, dataclasses.replace(settings, enabled=False)
        )

    cancelled: list[str] = []
    cancel_errors: dict[str, str] = {}
    not_cancellable: list[str] = []
    active = [
        run
        for run in api.list_runs()
        if run.get("state") in runtime_db.EXECUTION_CENTER_ACTIVE_STATES
    ]
    for run in active:
        try:
            api.request_cancel(run["id"], confirmed=True)
            cancelled.append(run["id"])
        except Exception as exc:  # noqa: BLE001 — every failure is reported, none hides
            if "not an actively supervised run" in str(exc):
                not_cancellable.append(run["id"])
            else:
                cancel_errors[run["id"]] = str(exc)
    return {
        "disabled": True,
        "cancelled": cancelled,
        "cancel_errors": cancel_errors,
        "not_cancellable": not_cancellable,
    }


class SpendUnknownError(Exception):
    """Raised by `daily_spend_usd` when the trailing-24h spend cannot be
    trusted as a real number.

    This is the function's failure mode for "known garbage", as opposed to a
    `runtime_db`/SQL exception propagating for "unreadable". Either way, a
    caller must fail closed exactly like it would for a DB outage — but must
    never spell the result `LAUNCH_BUDGET_EXHAUSTED`/`DEFER_DAILY_BUDGET`,
    which asserts a *known* spend at/over the ceiling. This is `daily_spend_usd`
    refusing to guess, not a verdict that the budget was hit.
    """

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


# `SpendUnknownError.reason` values.
CORRUPT_COST_EVENT = "corrupt_cost_event"
SPEND_TOTAL_OVERFLOW = "spend_total_overflow"


def daily_spend_usd(db_path: Path, *, now: str | None = None) -> float:
    """Sum of the providers' own reported `total_cost_usd` over the trailing
    24 hours (runs whose `completed_at` falls in the window, plus still-running
    work started in it). Reads only the final `result` stream events, which are
    the single truthful cost source — nothing is estimated or fabricated; a
    run with no matching event at all (no result reported yet) contributes 0.

    Trustworthy or raises: every row this reads was already selected because
    its JSON text contains `total_cost_usd`, so a row whose decoded
    `total_cost_usd` is not a finite, non-negative number — a string, `null`,
    a bool, `NaN`/`Infinity` (valid Python floats `json.loads` will happily
    hand back), or a negative number — raises `SpendUnknownError
    (CORRUPT_COST_EVENT)` rather than being silently skipped or coerced. A
    skip-and-continue here would let one corrupt or adversarial event zero out
    real spend and make a caller believe the budget has headroom it does not
    have. The running `total` is checked the same way after every addition,
    so individually finite costs that overflow the accumulator (e.g. two
    `1e308` values) raise `SpendUnknownError(SPEND_TOTAL_OVERFLOW)` instead of
    silently becoming `inf` — a total no comparison against a ceiling can use.

    `payload` may already be a `dict` rather than JSON text — a `jsonb`-backed
    read (the PostgreSQL mirror this table has, VOYN-W0-AICC-SRV-01B) hands
    back a decoded object, not a string, and `json.loads` on a `dict` raises
    `TypeError`. A prior version caught `TypeError` alongside `ValueError` and
    silently `continue`d past every row, which zeroes the whole sum with no
    error and no log line — a spend cap that reads 0 stops gating without
    ever saying so. Only malformed JSON *text* is tolerated (and logged); a
    row of an unexpected shape is now visible instead of silently dropped.
    """
    import json as _json
    import math as _math
    from datetime import datetime as _dt, timedelta as _td

    anchor = _dt.fromisoformat(now) if now else _dt.now()
    cutoff = (anchor - _td(hours=24)).isoformat(timespec="seconds")
    total = 0.0
    with runtime_db.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT run_event.payload_json AS payload FROM run_event
              JOIN run ON run.id = run_event.run_id
               AND run_event.payload_json LIKE '%total_cost_usd%'
               AND (run.completed_at >= ? OR (run.completed_at IS NULL AND run.created_at >= ?))
            """,
            (cutoff, cutoff),
        ).fetchall()
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, (str, bytes, bytearray)):
            try:
                payload = _json.loads(payload)
            except ValueError:
                _LOG.warning(
                    "daily_spend_usd: skipping run_event with unparseable payload_json: %r",
                    payload[:200] if isinstance(payload, str) else payload,
                )
                continue
        if not isinstance(payload, dict):
            _LOG.warning(
                "daily_spend_usd: skipping run_event whose payload is a %s, not an object",
                type(payload).__name__,
            )
            continue
        cost = payload.get("total_cost_usd")
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not _math.isfinite(cost)
            or cost < 0
        ):
            raise SpendUnknownError(
                CORRUPT_COST_EVENT,
                f"total_cost_usd={cost!r} is not a finite, non-negative number",
            )
        total += float(cost)
        if not _math.isfinite(total):
            raise SpendUnknownError(
                SPEND_TOTAL_OVERFLOW,
                f"trailing-24h total overflowed to {total!r} after adding {cost!r}",
            )
    return total
