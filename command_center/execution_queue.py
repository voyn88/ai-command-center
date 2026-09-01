"""Execution Queue: an explicit, persisted "what should launch next" list —
distinct from both the Kanban planning lane (`task["status"]`) and the real
execution state (`runtime.db`, per ADR 0003).

Design intent, per the founder decision this module implements:

- **No hidden scheduler.** Nothing in this module launches anything on its
  own initiative. `evaluate_readiness`/`reevaluate_and_persist` only ever
  *recompute a label* (waiting vs. ready) from the existing dependency graph
  — deterministic, side-effect-free besides persisting that label. Only
  `launch_ready`, called from an explicit button click in the UI layer,
  starts a process, and only for entries already in the `READY` state.
- **Deterministic readiness, not a background poll.** A queue entry becomes
  `READY` the moment its evaluation is *run* after its dependencies are
  satisfied — not automatically the instant that happens. The caller is
  responsible for calling `reevaluate_and_persist` at the checkpoints that
  matter (app load, manual refresh, right after a task-state change, right
  after a run reaches a terminal state) — see `command_center.ui.queue_panel`
  for where those checkpoints are wired into the Streamlit app today.
- **A future background worker can reuse this unmodified.** Every function
  here is plain Python over plain dicts and the existing `ExecutionCenterAPI`
  / `launch` / `launch_service` primitives — no Streamlit import, no
  in-process state. A future Supervisor-driven poller can call
  `reevaluate_and_persist` and `launch_ready` on a timer against the same
  `data/execution_queue.json` this module already writes, without this
  module (or its persisted shape) changing at all. That poller does not
  exist yet and is explicitly out of scope here — see the module's ADR.

Storage: `data/execution_queue.json`, one JSON list of queue-entry dicts,
using the same atomic whole-file read/write convention as `tasks.json` (see
`command_center.storage`). Deliberately *not* a new `runtime.db` table: the
queue is a planning-level "what do I want to launch, in what order" list,
upstream of `runtime.db`'s job (ADR 0003) of being the sole source of truth
for "is this actually running" — putting it in the same store as run/session
state would blur that boundary for no benefit; a JSON list is exactly as
reusable by a future worker and carries zero migration risk to the frozen
Supervisor schema.

Cross-process consistency: `storage.atomic_write_json`'s temp-file +
`os.replace` makes any *single* queue write atomic, but does nothing to stop a
lost update across a read-modify-write *cycle* — two concurrent callers (two
Streamlit sessions, a session plus the future Supervisor-driven poller the
module docstring above anticipates) can both `load_queue`, each recompute from
that same pre-write snapshot, and the second `save_queue` silently discards the
first's change. Every read-modify-write cycle this module owns therefore runs
inside `queue_lock` (a thin wrapper over `command_center.storage.file_lock`, the
same OS advisory-lock primitive `portfolio_launch` and `task_import` already use
for their JSON stores): `reevaluate_and_persist`, the `*_and_persist` helpers,
and `launch_ready`'s state-commit all hold it across their whole load→save span.
`load_queue`/`save_queue`/`enqueue`/`dequeue`/`evaluate_readiness` keep their
existing signatures unchanged — a caller that already serializes its own access
(or genuinely wants a lock-free read) can still use them directly; the locked
`*_and_persist` variants are the safe default for a bare read-modify-write.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

from command_center import (
    agent_runner,
    prompts,
    launch,
    launch_service,
    models,
    pipeline_settings,
    project_config,
    storage,
    workspace_provisioning,
)
from command_center.runtime import db as runtime_db
from command_center.runtime import supervisor as runtime_supervisor

logger = logging.getLogger(__name__)

# Sentinel `entry_id` for "the mirror could not be read at all" — see
# `queue_divergence`, which must never report an absent mirror as agreement.
MIRROR_UNAVAILABLE = "__mirror_unavailable__"

QUEUE_FILE_NAME = "execution_queue.json"

STATE_WAITING = "waiting"
STATE_READY = "ready"
STATE_LAUNCHED = "launched"
STATE_CANCELLED = "cancelled"

# Non-terminal — still tracked, still re-evaluated on every checkpoint.
OPEN_STATES: frozenset[str] = frozenset({STATE_WAITING, STATE_READY})


QUEUE_LOCK_FILE_NAME = "execution_queue.lock"
QUEUE_LOCK_TIMEOUT_SECONDS = 30.0
_QUEUE_LOCK_POLL_SECONDS = 0.05


def queue_file_path(root: Path) -> Path:
    return storage.resolve_data_dir(root) / QUEUE_FILE_NAME


def queue_lock_path(root: Path) -> Path:
    """The dedicated lock file guarding `execution_queue.json`'s
    read-modify-write cycle — a sibling of the queue file, never the queue file
    itself, so a lock holder never blocks a plain unlocked `load_queue` read."""
    return storage.resolve_data_dir(root) / QUEUE_LOCK_FILE_NAME


def load_queue(root: Path) -> list[dict]:
    return storage.read_json(queue_file_path(root), [])


def save_queue(root: Path, entries: list[dict]) -> None:
    """Persist the queue. JSON is authoritative; the SQLite mirror is written
    alongside it (ADR 0007 step 1).

    Order matters: JSON first, then the mirror. If the mirror write fails the
    authoritative store has already committed, so the queue is correct and only
    the mirror is stale — the divergence check will see it, which is exactly
    what that check is for. Writing the mirror first would allow the opposite:
    a mirror ahead of the real queue, which no check would flag as wrong.

    A mirror failure is swallowed for the same reason: during dual-write the
    mirror is not load-bearing, and letting it break a queue write would mean a
    migration step could take down the queue it is migrating."""
    storage.atomic_write_json(queue_file_path(root), entries)
    _mirror_to_runtime_db(root, entries)


def _mirror_to_runtime_db(root: Path, entries: list[dict]) -> None:
    """Best-effort write of the queue into `runtime.db` (ADR 0007 dual-write).

    Deliberately silent on failure — see `save_queue`. The mirror's health is
    reported by `queue_divergence`, not by exceptions raised at write time."""
    try:
        runtime_db.replace_queue_entries(runtime_db.resolve_db_path(root), entries)
    except Exception:  # noqa: BLE001 — the mirror must never break the real write
        logger.warning("Could not mirror execution queue into runtime.db", exc_info=True)


def backfill_mirror(root: Path) -> int:
    """One-shot import of the existing JSON queue into the SQLite mirror
    (ADR 0007 step 2). Returns the number of entries written.

    Idempotent, because `replace_queue_entries` replaces the whole list rather
    than appending: running it twice leaves the same rows. That matters — the
    backfill is expected to be run more than once (once per operator, once
    after a rollback and re-advance), and a backfill that duplicated on the
    second run would manufacture exactly the divergence it exists to remove.

    Deliberately not called automatically from a read path. A migration step
    that runs itself as a side effect of someone looking at the queue is a
    migration nobody decided to perform."""
    entries = load_queue(root)
    runtime_db.replace_queue_entries(runtime_db.resolve_db_path(root), entries)
    return len(entries)


def queue_divergence(root: Path) -> list[dict]:
    """Entries where the JSON store and the SQLite mirror disagree.

    Returns one record per differing entry: `{entry_id, fields, json, mirror}`,
    or `[]` when they match.

    An *unreadable* mirror reports a single `MIRROR_UNAVAILABLE` record rather
    than an empty list. Returning `[]` there would be the dangerous answer: ADR
    0007 gates "stop writing JSON" on a session with no divergence, and a
    missing mirror would satisfy that gate by having nothing to disagree with —
    the migration would advance on the strength of a store that was never
    written.

    Read-only, and never raises: this runs on a read path during the dual-write
    phases, and a check that can break the thing it checks is worse than no
    check."""
    try:
        json_entries = load_queue(root)
        mirror_entries = runtime_db.list_queue_entries(runtime_db.resolve_db_path(root))
    except Exception as exc:  # noqa: BLE001
        return [
            {
                "entry_id": MIRROR_UNAVAILABLE,
                "fields": ["*"],
                "json": None,
                "mirror": None,
                "detail": f"{type(exc).__name__}: {exc}",
            }
        ]

    mirror_by_id = {entry.get("id"): entry for entry in mirror_entries}
    divergences: list[dict] = []
    for entry in json_entries:
        entry_id = entry.get("id")
        mirrored = mirror_by_id.pop(entry_id, None)
        if mirrored is None:
            divergences.append(
                {"entry_id": entry_id, "fields": ["*"], "json": entry, "mirror": None}
            )
            continue
        differing = [
            field
            for field in runtime_db._QUEUE_ENTRY_COLUMNS
            if entry.get(field) != mirrored.get(field)
        ]
        if differing:
            divergences.append(
                {"entry_id": entry_id, "fields": differing, "json": entry, "mirror": mirrored}
            )
    # Anything left in the mirror exists there but not in the authoritative
    # store — a stale row the mirror failed to delete.
    for entry_id, mirrored in mirror_by_id.items():
        divergences.append(
            {"entry_id": entry_id, "fields": ["*"], "json": None, "mirror": mirrored}
        )
    return divergences


@contextlib.contextmanager
def queue_lock(root: Path, *, timeout: float = QUEUE_LOCK_TIMEOUT_SECONDS):
    """Cross-process, cross-thread mutual exclusion for the *entire*
    read-modify-write cycle on `data/execution_queue.json` — acquire this
    before the `load_queue` and hold it through the `save_queue`, never just
    around the write, or two callers can still both read the same pre-write
    snapshot and each write back a version missing the other's change (the
    lost update this lock exists to prevent).

    Thin wrapper over `command_center.storage.file_lock` (see that function's
    docstring for the underlying `fcntl`/`msvcrt` OS advisory-lock mechanism,
    including that a crashed holder never wedges future callers). A
    `storage.LockTimeoutError` propagates unchanged — a bounded, descriptive
    failure instead of an indefinite hang."""
    with storage.file_lock(queue_lock_path(root), timeout=timeout, poll_seconds=_QUEUE_LOCK_POLL_SECONDS):
        yield


def _mutate_queue(root: Path, transform, *, timeout: float = QUEUE_LOCK_TIMEOUT_SECONDS) -> list[dict]:
    """Run one `load_queue -> transform -> save_queue` cycle entirely inside
    `queue_lock`, so the read `transform` is computed from and the write that
    commits it are adjacent and no concurrent writer can interleave between
    them. `transform` takes the freshly-loaded entries and returns the entries
    to persist."""
    with queue_lock(root, timeout=timeout):
        updated = transform(load_queue(root))
        save_queue(root, updated)
        return updated


def _new_entry(task: dict) -> dict:
    return {
        "id": models.new_id(),
        "task_id": task.get("id"),
        "project": task.get("project"),
        "state": STATE_WAITING,
        "reason": None,
        "run_id": None,
        "added_at": models.iso_now(),
        "evaluated_at": None,
        "launched_at": None,
    }


def enqueue(entries: list[dict], task: dict, tasks_by_id: dict[str, dict]) -> list[dict]:
    """Adds `task` to the queue unless it already has an open (waiting/ready)
    entry — re-enqueuing an already-queued task is a no-op, not a duplicate.
    A task already `Done` is never enqueued. Returns the new entry list;
    readiness is evaluated immediately so the caller never has to render a
    freshly-added entry with a stale state."""
    task_id = task.get("id")
    if task_id is None:
        return entries
    if task.get("status") == "Done":
        return entries
    if any(e.get("task_id") == task_id and e.get("state") in OPEN_STATES for e in entries):
        return entries
    updated = [*entries, _new_entry(task)]
    return evaluate_readiness(updated, tasks_by_id)


def dequeue(entries: list[dict], entry_id: str) -> list[dict]:
    """Removes an entry outright (used for waiting/ready entries the user no
    longer wants queued). Launched/cancelled entries are left alone by every
    other function in this module and are expected to age out via whatever
    retention policy the caller chooses — this module keeps no opinion on
    that beyond "never delete a launched entry silently"."""
    return [e for e in entries if e.get("id") != entry_id]


def evaluate_readiness(entries: list[dict], tasks_by_id: dict[str, dict]) -> list[dict]:
    """Pure recomputation of every open entry's `state`/`reason` from the
    *current* dependency graph and task status — never launches anything,
    never touches the filesystem. Safe to call on every Streamlit rerun.

    - A task that no longer exists, or has reached `Done` outside the queue
      (e.g. a manual Kanban move), is cancelled — its dependents were the
      point of tracking it, and a finished task has nothing left to wait on.
    - Otherwise: `READY` if `models.is_blocked` says no, else `WAITING` with
      a human-readable reason naming the unmet dependencies.
    """
    updated: list[dict] = []
    for entry in entries:
        if entry.get("state") not in OPEN_STATES:
            updated.append(entry)
            continue

        task = tasks_by_id.get(entry.get("task_id"))
        entry = dict(entry)
        entry["evaluated_at"] = models.iso_now()

        if task is None:
            entry["state"] = STATE_CANCELLED
            entry["reason"] = "задача больше не существует"
        elif task.get("status") == "Done":
            entry["state"] = STATE_CANCELLED
            entry["reason"] = "задача уже завершена"
        else:
            unmet = models.unmet_dependencies(task, tasks_by_id)
            if unmet:
                names = ", ".join((tasks_by_id.get(dep_id, {}).get("title") or dep_id) for dep_id in unmet)
                entry["state"] = STATE_WAITING
                entry["reason"] = f"ожидает: {names}"
            else:
                entry["state"] = STATE_READY
                entry["reason"] = None

        updated.append(entry)
    return updated


def reevaluate_and_persist(root: Path, tasks_by_id: dict[str, dict]) -> list[dict]:
    """The single call the UI layer makes at each of the four checkpoints
    (app load, manual refresh, after a task-state change, after a run
    reaches a terminal state) — loads, re-evaluates, saves, returns, with the
    whole load→save cycle held under `queue_lock` so a concurrent writer's
    update is never lost. Never launches anything; see `launch_ready` for the
    only function in this module that does."""
    return _mutate_queue(root, lambda entries: evaluate_readiness(entries, tasks_by_id))


def enqueue_and_persist(root: Path, task: dict, tasks_by_id: dict[str, dict]) -> list[dict]:
    """Lost-update-safe `load_queue -> enqueue -> save_queue`: the whole cycle
    runs under `queue_lock`, so adding a task to the queue never clobbers a
    concurrent re-evaluation or another enqueue in a different process. Prefer
    this over hand-rolling the `load_queue`/`enqueue`/`save_queue` triple."""
    return _mutate_queue(root, lambda entries: enqueue(entries, task, tasks_by_id))


def dequeue_and_persist(root: Path, entry_id: str) -> list[dict]:
    """Lost-update-safe `load_queue -> dequeue -> save_queue`: removing an
    entry re-reads the current on-disk queue under `queue_lock` before writing,
    so it never silently reverts a concurrent writer's change back to the stale
    snapshot the caller happened to be holding."""
    return _mutate_queue(root, lambda entries: dequeue(entries, entry_id))


def ready_entries(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("state") == STATE_READY]


def waiting_entries(entries: list[dict]) -> list[dict]:
    return [e for e in entries if e.get("state") == STATE_WAITING]


# Machine-readable outcome codes for one `launch_ready` attempt. `message` is
# a human-facing (Russian) sentence and is deliberately never parsed; anything
# that needs to *branch* on why a launch did not happen — the autopilot's audit
# trail, the desktop wave view's remediation hints, tests — reads `reason_code`
# instead. Every `LaunchAttemptResult` carries exactly one.
LAUNCH_OK = "launched"
LAUNCH_SKIP_TASK_DONE = "task_already_done"
LAUNCH_SKIP_TASK_NOT_FOUND = "task_not_found"
LAUNCH_SKIP_WORKSPACE_NOT_CONFIGURED = "workspace_not_configured"
LAUNCH_SKIP_BLOCKED = "launch_blocked"
LAUNCH_SKIP_NEEDS_CONFIRMATION = "needs_confirmation"
LAUNCH_SKIP_DUPLICATE_ACTIVE = "duplicate_active_launch"
LAUNCH_SKIP_WORKSPACE_VERIFICATION = "workspace_verification_failed"
LAUNCH_SKIP_GLOBAL_AT_CAPACITY = "global_at_capacity"
LAUNCH_SKIP_WORKSPACE_LOCKED = "workspace_locked"
LAUNCH_SKIP_LAUNCH_ERROR = "launch_error"
LAUNCH_SKIP_NO_AVAILABLE_EXECUTOR = "no_available_executor"


def select_available_executor(
    task: dict,
    cfg: dict,
    *,
    preferred_executor: str | None = None,
    load_snapshot=None,
) -> str | None:
    """Pick the best *available* executor for this task.

    When ``load_snapshot`` is a ``scheduler.LoadSnapshot`` (built once per
    ``launch_ready`` batch from ``runtime.db``), the selection is
    **load-aware**: among all viable candidates the executor with the most
    spare concurrency capacity is chosen, so idle agents are preferred over
    ones already running tasks. The explicitly configured executor (the
    task's own ``executor`` or the caller's ``preferred_executor``) is still
    tried first — but only if it has capacity; if it is at its concurrency
    limit and another executor has spare slots, the other one wins.

    Capability check: ``ollama`` (and any future capability-restricted
    executor) is only eligible for task types it can actually run —
    read-only types (review, audit, …). An implementation task silently
    skips ``ollama`` rather than dispatching to a provider that will refuse
    it at launch time.

    A provider whose ``availability()`` probe returns ``available=False``
    (binary missing, daemon unreachable, …) is never selected. A provider
    already in ``task["failed_executors"]`` (set by ``task_sync`` when a run
    died at startup) is also skipped, triggering automatic fallover to the
    next eligible agent.

    Returns ``None`` when every allowed executor is ineligible — the caller
    emits ``LAUNCH_SKIP_NO_AVAILABLE_EXECUTOR`` and the task stays queued.
    """
    from command_center.runtime import providers as runtime_providers
    from command_center.runtime import scheduler as runtime_scheduler

    allowed = list(cfg.get("allowed_agents") or [])
    explicit_executor = preferred_executor or task.get("executor")
    task_type = task.get("task_type") or "implementation"
    required_caps = runtime_scheduler.capabilities_for_task_type(task_type)
    failed = set(task.get("failed_executors") or [])

    running_by_executor: dict[str, int] = {}
    if load_snapshot is not None:
        running_by_executor = dict(load_snapshot.running_by_agent)

    def _viable(executor_id: str) -> bool:
        if executor_id in failed:
            return False
        try:
            provider = runtime_providers.get_provider(executor_id)
        except ValueError:
            return False
        if not provider.availability().available:
            return False
        # Capability gate: ollama only matches read-only task types.
        executor_caps = runtime_scheduler.capabilities_for_executor(executor_id)
        if runtime_scheduler.CAP_ANY not in executor_caps:
            if not (required_caps <= executor_caps):
                return False
        return True

    # Build a deduped candidate list: explicit executor first (if any),
    # then the rest of allowed_agents in configured order. When neither
    # an explicit executor nor allowed_agents is configured, fall back to
    # "claude_code" (the system default) so unconfigured tasks still launch.
    all_ids: list[str] = []
    seen: set[str] = set()
    if explicit_executor:
        all_ids.append(explicit_executor)
        seen.add(explicit_executor)
    for eid in allowed:
        if eid not in seen:
            all_ids.append(eid)
            seen.add(eid)
    if not all_ids:
        all_ids = ["claude_code"]

    DEFAULT_MAX_CONCURRENCY = 2
    candidates: list[tuple[str, int, bool]] = []  # (executor_id, spare, is_explicit)
    for executor_id in all_ids:
        if not _viable(executor_id):
            continue
        spare = DEFAULT_MAX_CONCURRENCY - running_by_executor.get(executor_id, 0)
        candidates.append((executor_id, spare, executor_id == explicit_executor))

    if not candidates:
        return None

    # Sort: most spare capacity first; explicit executor wins on equal capacity.
    # Python's sort is stable, so configured list order is the tiebreak when
    # spare capacity and explicit-flag are both equal.
    candidates.sort(key=lambda c: (-c[1], not c[2]))
    return candidates[0][0]


def select_remediation_executor(task: dict, cfg: dict) -> str | None:
    """Pick the executor to use for a «Исправить» (fix) re-launch.

    Simpler than ``select_available_executor``:
    - Does NOT probe ``provider.availability()`` — transient probe failures
      must not block an operator-initiated retry (see comment in app.py).
    - Does NOT apply load-awareness — this is a one-off human action.
    - Skips only executors already in ``task["failed_executors"]`` (those
      recorded as startup failures, not permission-denied blocks mid-run).

    Returns the first viable candidate in configured order, or ``None`` when
    every candidate is in ``failed_executors``.
    """
    configured = task.get("executor") or "claude_code"
    failed = set(task.get("failed_executors") or [])
    allowed = list(cfg.get("allowed_agents") or [])

    candidates: list[str] = []
    seen: set[str] = set()
    for eid in [configured, *allowed]:
        if eid not in seen:
            candidates.append(eid)
            seen.add(eid)
    if not candidates:
        candidates = ["claude_code"]

    for eid in candidates:
        if eid not in failed:
            return eid
    return None


@dataclass
class LaunchAttemptResult:
    entry_id: str
    task_id: str | None
    launched: bool
    run_id: str | None = None
    message: str = ""
    # One of the `LAUNCH_*` constants above — the machine-readable counterpart
    # to `message`. Defaults to the generic error code so an unannotated
    # construction is never mistaken for a success.
    reason_code: str = LAUNCH_SKIP_LAUNCH_ERROR
    # Populated only for the "blocked by validation warnings" case (dirty
    # tree, detached HEAD, branch mismatch) — the exact strings from
    # `launch.LaunchValidation.warnings`, so the UI layer can render each as
    # its own bullet instead of collapsing them into `message`'s generic
    # summary. Empty for every other skip/failure reason (missing workspace,
    # task not found, launch exception).
    warnings: list[str] = field(default_factory=list)
    # Full `launch.validate_launch` result for the same case, for an
    # optional "Details" expander in the UI — never used to gate anything,
    # purely informational.
    validation_report: dict | None = None


def _validation_report(validation: launch.LaunchValidation, *, expected_branch: str | None) -> dict:
    """The complete pre-flight picture for one skipped launch — everything
    `validate_launch` observed, not just the warning strings — for the UI's
    "Details" expander. Read-only summary; never used for control flow."""
    git_status = validation.git_status or {}
    return {
        "workspace_path": validation.workspace_path,
        "expected_branch": expected_branch,
        "actual_branch": git_status.get("branch"),
        "errors": list(validation.errors),
        "warnings": list(validation.warnings),
        "git_status": dict(git_status),
    }


def launch_ready(
    root: Path,
    entries: list[dict],
    tasks: list[dict],
    tasks_by_id: dict[str, dict],
    project_configs: dict[str, dict],
    execution_center_api,
    *,
    entry_ids: list[str] | None = None,
    executor_by_entry: dict[str, str] | None = None,
    timeout_seconds: int | None = None,
) -> tuple[list[dict], list[LaunchAttemptResult]]:
    """Launches every `READY` entry (or, if `entry_ids` is given, just those
    — the "launch next ready task" action passes a single id). Never called
    from a plain render path — only from an explicit button-click handler in
    the UI layer (see `command_center.ui.queue_panel`), and never invoked
    implicitly by `evaluate_readiness`/`reevaluate_and_persist`.

    An entry whose validation carries *warnings* (dirty tree, detached HEAD,
    branch mismatch) is deliberately **not** auto-launched — those normally
    require an explicit human checkbox acknowledgement in
    `render_agent_launcher`, and a batch/queue action has no per-task human
    in the loop to give it. Such an entry is left `READY` (still queued,
    still visible) with a message pointing at the task's own launcher for a
    manual, confirmed launch. An entry with a blocking *error* (missing/
    invalid workspace) is treated the same way — left in place, reported,
    never silently dropped.

    Persistence is cross-process safe: the launched-state transitions are
    committed under `queue_lock`, re-reading the current on-disk queue and
    merging onto it (see `_commit_launch_results`), so a concurrent
    `reevaluate_and_persist`/enqueue in another process is preserved rather
    than clobbered by this call's snapshot. The actual process launches happen
    *outside* the lock — it is held only for the final merge-and-save, never
    across a subprocess spawn. This function still saves the queue itself; the
    returned entry list is the merged result and must be treated as a snapshot,
    not saved again outside the lock. Callers must separately persist `tasks` via
    `tasks_repository.save_tasks` — this function mutates `task` dicts in place
    exactly like `launch_service.execute_agent_launch_v2` already does. One
    `LaunchAttemptResult` per entry considered is returned, in order, for the
    caller to render a per-entry outcome."""
    targets = ready_entries(entries)
    if entry_ids is not None:
        wanted = set(entry_ids)
        targets = [e for e in targets if e.get("id") in wanted]

    results: list[LaunchAttemptResult] = []
    # entry_id -> the field patch to apply on a successful launch. Collected
    # here rather than mutated straight into a working copy so the state commit
    # (and its `queue_lock`) can happen once, after every launch, outside the
    # lock — never holding the queue lock across an `execute_agent_launch_v2`
    # subprocess spawn.
    launched_patches: dict[str, dict] = {}

    # Build a load snapshot once for the whole batch so executor selection is
    # capacity-aware. A single read avoids repeated DB round-trips and ensures
    # all entries in this batch see the same baseline load (intra-batch launches
    # are reflected by the global concurrency counter, not this snapshot).
    try:
        from command_center.runtime import scheduler as _sched
        _load_snapshot = _sched.build_load_snapshot(runtime_db.resolve_db_path(root))
    except Exception:  # noqa: BLE001
        _load_snapshot = None

    # Global concurrency cap (audit H3). launch_ready is a direct entry point
    # (the queue's "launch all READY" button, portfolio launches) that bypasses
    # the scheduler's cap, so a batch could spawn one agent per READY entry
    # regardless of the limit. The cap is ultimately enforced atomically in
    # db.create_run; passing it here just lets a batch stop cleanly at the limit
    # with a clear per-entry reason instead of each spawn being rejected.
    max_global = pipeline_settings.load_settings(root).max_global_concurrency

    for entry in targets:
        task_id = entry.get("task_id")
        task = tasks_by_id.get(task_id)
        if task is None:
            results.append(
                LaunchAttemptResult(
                    entry["id"],
                    task_id,
                    False,
                    message="задача не найдена",
                    reason_code=LAUNCH_SKIP_TASK_NOT_FOUND,
                )
            )
            continue
        if task.get("status") == "Done":
            results.append(
                LaunchAttemptResult(
                    entry["id"],
                    task_id,
                    False,
                    message="задача уже в статусе Done — запуск отклонён",
                    reason_code=LAUNCH_SKIP_TASK_DONE,
                )
            )
            continue

        # `project_configs` is keyed by canonical id, but a task may store a
        # display name / alias. Resolve it before preparing the launch so the
        # queue receives the correct repository and workspace configuration.
        canonical_project = project_config.canonical_project_id(task.get("project"))
        cfg = project_configs.get(canonical_project, {})

        # Shared classification — same service the Kanban launcher uses. A
        # missing but provisionable workspace is routed through the isolated
        # worktree provisioning and fail-closed verification path.
        prep = launch_service.prepare_task_launch(task=task, project_config=cfg)
        if not prep.selection.path:
            results.append(
                LaunchAttemptResult(
                    entry["id"],
                    task_id,
                    False,
                    message="workspace не настроен для задачи",
                    reason_code=LAUNCH_SKIP_WORKSPACE_NOT_CONFIGURED,
                )
            )
            continue

        expected_branch = prep.expected_branch
        base_branch = prep.base_branch
        source_repository_path = prep.source_repository_path
        validation = prep.validation

        if prep.decision == launch_service.LAUNCH_DECISION_BLOCKED:
            results.append(
                LaunchAttemptResult(
                    entry["id"],
                    task_id,
                    False,
                    message="; ".join(prep.fatal_messages) or "запуск заблокирован",
                    reason_code=LAUNCH_SKIP_BLOCKED,
                )
            )
            continue
        if prep.decision == launch_service.LAUNCH_DECISION_NEEDS_CONFIRMATION:
            results.append(
                LaunchAttemptResult(
                    entry["id"],
                    task_id,
                    False,
                    message="требует подтверждения предупреждений — запустите вручную из карточки задачи",
                    reason_code=LAUNCH_SKIP_NEEDS_CONFIRMATION,
                    warnings=list(validation.warnings),
                    validation_report=_validation_report(validation, expected_branch=expected_branch),
                )
            )
            continue

        # READY or PROVISIONABLE — proceed through the shared launcher.
        resolved_workspace = Path(prep.resolved_workspace)

        # --- Executor fallback (AICC-DESKTOP-017) ---------------------------
        # The task may name an executor whose binary is unavailable or that
        # already died on startup (e.g. an expired OAuth token that the
        # `--version` probe cannot detect). Try the configured executor first,
        # then the rest of the project's `allowed_agents` in order, skipping
        # any in `task["failed_executors"]`. When the selected executor
        # differs from the task's configured one, record the switch so the
        # board shows which agent actually ran.
        cfg = project_configs.get(task.get("project")) or {}
        planned_executor = (executor_by_entry or {}).get(entry.get("id"))
        selected_executor = select_available_executor(
            task,
            cfg,
            preferred_executor=planned_executor,
            load_snapshot=_load_snapshot,
        )
        if selected_executor is None:
            results.append(
                LaunchAttemptResult(
                    entry["id"],
                    task_id,
                    False,
                    message=(
                        "ни один из разрешённых исполнителей не доступен — "
                        "проверьте установку/авторизацию агентов"
                    ),
                    reason_code=LAUNCH_SKIP_NO_AVAILABLE_EXECUTOR,
                )
            )
            continue
        if selected_executor != (task.get("executor") or "claude_code"):
            original = task.get("executor") or "claude_code"
            task.setdefault("timeline", []).append(
                {
                    "ts": models.iso_now(),
                    "type": (
                        "executor_assignment"
                        if planned_executor == selected_executor
                        else "executor_fallback"
                    ),
                    "from": original,
                    "to": selected_executor,
                    "reason": (
                        "selected by load-aware scheduler"
                        if planned_executor == selected_executor
                        else "configured executor unavailable or failed"
                    ),
                }
            )

        try:
            run = launch_service.execute_agent_launch_v2(
                project=task.get("project"),
                task_type=task.get("task_type") or "implementation",
                # A task without its own prompt used to reach the agent as
                # nothing but its title. `prompts.build_prompt` composes the
                # instruction for its task type instead, and returns a
                # deliberately-written prompt untouched when there is one.
                prompt=prompts.build_prompt(task),
                # An explicit caller timeout wins; otherwise the timeout is
                # individualized to 200 % of the task's estimate (see
                # `agent_runner.timeout_for_task`) rather than one fixed default.
                timeout_seconds=(
                    timeout_seconds
                    if timeout_seconds is not None
                    else agent_runner.timeout_for_task(task)
                ),
                repository_path=resolved_workspace,
                execution_center_api=execution_center_api,
                confirmed=True,
                task=task,
                executor_id=selected_executor,
                validation=validation,
                expected_branch=expected_branch,
                base_branch=base_branch,
                source_repository_path=source_repository_path,
                max_global_concurrency=max_global,
            )
        except launch_service.DuplicateActiveLaunchError as exc:
            results.append(
                LaunchAttemptResult(
                    entry["id"],
                    task_id,
                    False,
                    message=str(exc),
                    reason_code=LAUNCH_SKIP_DUPLICATE_ACTIVE,
                )
            )
            continue
        except (
            workspace_provisioning.WorkspaceVerificationError,
            runtime_supervisor.WorkspaceVerificationFailed,
        ) as exc:
            # Provisioning or the fail-closed isolation gate rejected this
            # workspace — the agent was never started. Record an explicit,
            # structured Requires-Attention reason; never continue as launched.
            structured = (
                exc.structured
                if isinstance(exc, runtime_supervisor.WorkspaceVerificationFailed)
                else exc.as_dict()
            )
            reason = structured.get("detail") or structured.get("remediation") or "проверка изоляции не пройдена"
            results.append(
                LaunchAttemptResult(
                    entry["id"],
                    task_id,
                    False,
                    message=f"workspace не прошёл проверку изоляции ({structured['failed_step']}): {reason}",
                    reason_code=LAUNCH_SKIP_WORKSPACE_VERIFICATION,
                    validation_report=structured,
                )
            )
            continue
        except runtime_db.GlobalConcurrencyLimitError as exc:
            # Atomic global cap hit (audit H3). The rest of the batch will hit it
            # too, but keep reporting per-entry rather than aborting so the
            # operator sees exactly which tasks were held back.
            results.append(
                LaunchAttemptResult(
                    entry["id"],
                    task_id,
                    False,
                    message=f"глобальный лимит достигнут ({exc.active_count}/{exc.limit} активны)",
                    reason_code=LAUNCH_SKIP_GLOBAL_AT_CAPACITY,
                )
            )
            continue
        except runtime_supervisor.WorkspaceLockedError as exc:
            # Another run claimed this exact workspace between our snapshot and
            # the atomic create_run insert (audit H4). Report it as the specific
            # "workspace busy" outcome, not a generic launch error, so the loser
            # of the race is described accurately.
            results.append(
                LaunchAttemptResult(
                    entry["id"],
                    task_id,
                    False,
                    message=str(exc),
                    reason_code=LAUNCH_SKIP_WORKSPACE_LOCKED,
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001 — one bad entry must not abort the batch
            results.append(
                LaunchAttemptResult(
                    entry["id"],
                    task_id,
                    False,
                    message=str(exc),
                    reason_code=LAUNCH_SKIP_LAUNCH_ERROR,
                )
            )
            continue

        launched_patches[entry["id"]] = {
            "state": STATE_LAUNCHED,
            "run_id": run["id"],
            "launched_at": models.iso_now(),
            "reason": None,
        }
        results.append(
            LaunchAttemptResult(entry["id"], task_id, True, run_id=run["id"], reason_code=LAUNCH_OK)
        )

    updated_entries = _commit_launch_results(root, entries, launched_patches)
    return updated_entries, results


def _commit_launch_results(
    root: Path, base_entries: list[dict], patches: dict[str, dict]
) -> list[dict]:
    """Persist the launched-state `patches` (keyed by queue entry id) atomically
    under `queue_lock`, merged onto whatever is *currently* on disk rather than
    blindly overwriting it.

    `base_entries` is the caller's in-memory view (what it passed to
    `launch_ready`). Once a queue file exists, its current contents are the
    merge base: concurrent enqueue, reevaluation, ordering, and deletion all
    win over the stale caller snapshot. The only absent entry restored from
    `base_entries` is one with a successful launch patch, because that process
    has actually started and the queue must retain its `LAUNCHED` record.

    When no queue file has ever been persisted, `base_entries` seeds it for
    compatibility with in-memory callers and tests."""
    with queue_lock(root):
        persisted = queue_file_path(root).exists()
        disk = load_queue(root)
        merged = [dict(entry) for entry in (disk if persisted else base_entries)]
        by_id = {e.get("id"): e for e in merged}
        base_by_id = {e.get("id"): e for e in base_entries}
        for entry_id, patch in patches.items():
            target = by_id.get(entry_id)
            if target is None:
                base = base_by_id.get(entry_id)
                if base is None:
                    continue
                target = dict(base)
                merged.append(target)
                by_id[entry_id] = target
            target.update(patch)

        save_queue(root, merged)
    return merged


def reconcile_missing_run_links(root: Path, entries: list[dict]) -> list[dict]:
    """Backfill `run_id` on LAUNCHED entries that lost their run link.

    A queue entry transitions to STATE_LAUNCHED and receives a `run_id` in
    `_commit_launch_results`. If that commit was interrupted (crash, SIGKILL,
    lost write) the entry stays LAUNCHED but with `run_id=None`, making it
    invisible to live-board and attention tracking.

    For each such orphaned entry, look up the latest run for that `task_id` in
    runtime.db and fill in its id. Returns the (possibly modified) list; the
    caller decides whether to persist it.
    """
    db_path = runtime_db.resolve_db_path(root)
    repaired: list[dict] = []
    changed = False
    for entry in entries:
        if entry.get("state") == STATE_LAUNCHED and not entry.get("run_id"):
            task_id = entry.get("task_id")
            run = runtime_db.get_latest_run_for_task(db_path, task_id) if task_id else None
            if run and run.get("id"):
                entry = dict(entry)
                entry["run_id"] = run["id"]
                logger.info(
                    "reconcile_missing_run_links: backfilled run_id=%s for queue entry %s (task %s)",
                    run["id"],
                    entry.get("id"),
                    task_id,
                )
                changed = True
        repaired.append(entry)
    return repaired if changed else entries
