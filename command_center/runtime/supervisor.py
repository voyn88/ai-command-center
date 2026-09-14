"""Session Supervisor: owns the Claude Code subprocess lifecycle.

Normative decisions (frozen for Sprint 1 — see the Sprint 1 brief):

- `claude --session-id <uuid>` for a fresh run, exact-id `claude --resume <uuid>`
  for a resumed one. Never `--continue`/`-c`, never `--background`/`--bg`, never
  `claude agents` as a lifecycle registry — this module's own SQLite `run` table
  *is* the lifecycle registry.
- `--output-format stream-json --include-partial-messages --verbose
  --setting-sources ""` on every launch.
- `subprocess.Popen`, `shell=False`, `start_new_session=True` (so the child
  becomes its own process group leader — required for the SIGTERM-to-process-
  group / SIGKILL-after-grace cancellation below), stdin disconnected
  (`subprocess.DEVNULL`).
- The Supervisor owns: process lifecycle, PID/process-start metadata, stdout/
  stderr consumption, incremental `RunEvent` persistence, cancellation,
  timeout enforcement, startup reconciliation, and the post-`COMPLETED`
  auto-commit of work the agent left uncommitted
  (`_auto_commit_completed_work` — the one place this module *writes* git;
  `cancel()` still never does). Claude owns: reasoning,
  conversation, and provider session state — this module never inspects or
  interprets assistant content beyond classifying it for storage
  (`stream_parser`).

No database transaction here ever spans a subprocess call: `db.create_run`/
`db.update_run_state`/`db.update_run_fields`/`db.append_run_event` each open
and close their own short transaction, and `subprocess.Popen(...)` itself is
never called from inside one.

**This module does not assemble context and does not enforce the BANK/LEGAL
sensitive-project boundary.** `start_raw()` executes whatever `prompt` string
it is given, verbatim — it is the internal, low-level process executor, not
an application-facing entry point. The sensitive-project boundary
(`context_service.assemble_context`) is enforced one layer up, in
`ExecutionCenterAPI.start_run`, which is the only route application code
should use to launch a run against a real project. `start_raw()` is public
(tests call it directly, and a future non-project internal caller reasonably
could), but its name is deliberately not `start()` — nothing about its name
suggests it is safe to call with a caller-assembled prompt for a sensitive
project without having gone through `context_service` first.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TextIO

from command_center import (
    agent_runner,
    capabilities,
    project_config,
    provider_route,
    workspace_provisioning,
)
from command_center import run_lineage as provenance
from command_center.models import iso_now
from command_center.runtime import (
    context_service,
    db,
    git_ops,  # noqa: F401 - re-exported: tests patch `supervisor.git_ops.commit_all`
    identity,
    outcome,
    providers,
    reports,
    run_finalizer,
    stream_parser,
    stream_reader,
)

logger = logging.getLogger(__name__)

# Process-group inspection must not inherit test/application monkeypatches of
# ``subprocess.Popen`` that are intentionally scoped to launching Claude.
# Keeping the original constructor also prevents a failed Claude-launch setup
# from making the recovery probe recursively launch another fake Claude.
_SYSTEM_POPEN = subprocess.Popen
_SYSTEM_PROCESS_PID = os.getpid()
_SYSTEM_PROCESS_IDENTITY = identity.capture_identity(_SYSTEM_PROCESS_PID)
# Finalization ownership is process-scoped, matching the liveness proof used
# during recovery.  Multiple Supervisor facades in one backend process are not
# independent crash domains and therefore must not manufacture competing
# ownership tokens for the same OS identity.
_SYSTEM_FINALIZATION_OWNER_TOKEN: str | None = uuid.uuid4().hex
_PROCESS_CONTEXT_GUARD = threading.Lock()
_FINALIZATION_LOCKS_GUARD = threading.Lock()
_FINALIZATION_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_OWNED_RUNS_GUARD = threading.Lock()
_PROCESS_OWNED_RUNS: set[str] = set()


@dataclass
class _WindowsRuntimeLockState:
    mode: str
    handle: TextIO | None = None
    references: int = 0


@dataclass
class _WindowsRuntimeLockHandle:
    key: str
    handle: TextIO
    released: bool = False

    @property
    def closed(self) -> bool:
        return self.released


RuntimeLockHandle = TextIO | _WindowsRuntimeLockHandle


_WINDOWS_RUNTIME_LOCKS_CONDITION = threading.Condition()
_WINDOWS_RUNTIME_LOCKS: dict[str, _WindowsRuntimeLockState] = {}


def offline_cutover_fence_path(db_path: Path) -> Path:
    return db_path.with_name(f"{db_path.name}.offline-cutover")


def _windows_runtime_lock_path(db_path: Path) -> Path:
    """Keep the lifetime lock outside the replaceable SQLite data directory.

    Windows does not permit deleting an open file. A Supervisor deliberately
    holds this lock for its whole lifetime, so placing it beside ``runtime.db``
    makes an otherwise valid data-directory rotation or cleanup fail with
    ``WinError 32``. The normalized absolute DB path remains the lock identity;
    only the physical lock file lives in a stable sibling directory outside
    the replaceable data directory. Deriving that root from the canonical DB
    path (rather than a per-user temp directory) makes different service
    principals contend on the same machine-wide lock whenever they target the
    same database.
    """
    resolved = db_path.expanduser().resolve(strict=False)
    normalized = os.path.normcase(str(resolved))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return resolved.parent.parent / ".aicc-runtime-locks" / f"{digest}.lock"


def _runtime_lock_path(db_path: Path) -> Path:
    if os.name == "nt":
        return _windows_runtime_lock_path(db_path)
    return db_path.with_name(f"{db_path.name}.runtime-lock")


def _acquire_windows_runtime_lock(
    lock_path: Path, *, exclusive: bool
) -> _WindowsRuntimeLockHandle:
    """Make Windows byte-range locks re-entrant for shared in-process users."""
    import msvcrt

    key = os.path.normcase(str(lock_path.resolve(strict=False)))
    requested_mode = "exclusive" if exclusive else "shared"
    with _WINDOWS_RUNTIME_LOCKS_CONDITION:
        while True:
            state = _WINDOWS_RUNTIME_LOCKS.get(key)
            if state is None:
                _WINDOWS_RUNTIME_LOCKS[key] = _WindowsRuntimeLockState(
                    mode=f"acquiring-{requested_mode}"
                )
                break
            if not exclusive and state.mode == "shared":
                if state.handle is None:
                    raise RuntimeError("shared Windows runtime lock has no handle")
                state.references += 1
                return _WindowsRuntimeLockHandle(key=key, handle=state.handle)
            _WINDOWS_RUNTIME_LOCKS_CONDITION.wait()

    handle: TextIO | None = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        if lock_path.stat().st_size == 0:
            handle.write("0")
            handle.flush()
        handle.seek(0)
        mode = msvcrt.LK_LOCK if exclusive else msvcrt.LK_RLCK
        msvcrt.locking(handle.fileno(), mode, 1)
    except BaseException:
        if handle is not None:
            handle.close()
        with _WINDOWS_RUNTIME_LOCKS_CONDITION:
            _WINDOWS_RUNTIME_LOCKS.pop(key, None)
            _WINDOWS_RUNTIME_LOCKS_CONDITION.notify_all()
        raise

    with _WINDOWS_RUNTIME_LOCKS_CONDITION:
        state = _WINDOWS_RUNTIME_LOCKS[key]
        state.mode = requested_mode
        state.handle = handle
        state.references = 1
        _WINDOWS_RUNTIME_LOCKS_CONDITION.notify_all()
    return _WindowsRuntimeLockHandle(key=key, handle=handle)


def _acquire_runtime_lock(
    db_path: Path, *, exclusive: bool
) -> RuntimeLockHandle:
    lock_path = _runtime_lock_path(db_path)
    if os.name == "nt":
        return _acquire_windows_runtime_lock(lock_path, exclusive=exclusive)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+", encoding="utf-8")
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    return handle


def _release_windows_runtime_lock(handle: _WindowsRuntimeLockHandle) -> None:
    import msvcrt

    with _WINDOWS_RUNTIME_LOCKS_CONDITION:
        if handle.released:
            return
        handle.released = True
        state = _WINDOWS_RUNTIME_LOCKS.get(handle.key)
        if state is None or state.handle is not handle.handle:
            raise RuntimeError("Windows runtime lock registry lost its handle")
        state.references -= 1
        if state.references:
            return
        state.mode = "releasing"
        raw = state.handle
    if raw is None:
        raise RuntimeError("Windows runtime lock registry has no raw handle")
    try:
        raw.seek(0)
        msvcrt.locking(raw.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        raw.close()
        with _WINDOWS_RUNTIME_LOCKS_CONDITION:
            _WINDOWS_RUNTIME_LOCKS.pop(handle.key, None)
            _WINDOWS_RUNTIME_LOCKS_CONDITION.notify_all()


def _release_runtime_lock(handle: RuntimeLockHandle) -> None:
    if isinstance(handle, _WindowsRuntimeLockHandle):
        _release_windows_runtime_lock(handle)
        return
    if handle.closed:
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()


def acquire_offline_cutover_fence(
    db_path: Path,
) -> tuple[str, bool, RuntimeLockHandle]:
    """Hold an exclusive runtime lock and create/resume the restart marker."""
    lock_handle = _acquire_runtime_lock(db_path, exclusive=True)
    try:
        marker = offline_cutover_fence_path(db_path)
        marker.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(32)
        try:
            fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            existing = marker.read_text(encoding="utf-8").strip()
            if not existing:
                raise SupervisorError("offline cutover fence exists but has no token")
            return existing, False, lock_handle
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{token}\n")
            handle.flush()
            os.fsync(handle.fileno())
        return token, True, lock_handle
    except BaseException:
        _release_runtime_lock(lock_handle)
        raise


def release_offline_cutover_fence(
    db_path: Path,
    token: str,
    lock_handle: RuntimeLockHandle,
) -> None:
    """Remove only the fence owned by this successful cutover process."""
    marker = offline_cutover_fence_path(db_path)
    current = marker.read_text(encoding="utf-8").strip()
    if not token or current != token:
        raise SupervisorError("offline cutover fence ownership changed")
    marker.unlink()
    _release_runtime_lock(lock_handle)


def _reset_process_finalization_context() -> None:
    """Drop inherited authority after ``fork`` without doing fallible I/O.

    ``after_in_child`` exceptions are ignored by Python.  Identity capture uses
    ``ps`` and can fail, so the hook only publishes unmistakably child-local,
    unauthorised state.  ``_ensure_process_finalization_context`` performs the
    fallible capture lazily and retries on the next Supervisor construction.
    """
    global _SYSTEM_PROCESS_PID, _SYSTEM_PROCESS_IDENTITY
    global _SYSTEM_FINALIZATION_OWNER_TOKEN, _PROCESS_CONTEXT_GUARD
    global _FINALIZATION_LOCKS_GUARD
    global _FINALIZATION_LOCKS, _PROCESS_OWNED_RUNS_GUARD, _PROCESS_OWNED_RUNS
    _SYSTEM_PROCESS_PID = os.getpid()
    _SYSTEM_PROCESS_IDENTITY = None
    _SYSTEM_FINALIZATION_OWNER_TOKEN = None
    _PROCESS_CONTEXT_GUARD = threading.Lock()
    _FINALIZATION_LOCKS_GUARD = threading.Lock()
    _FINALIZATION_LOCKS = {}
    _PROCESS_OWNED_RUNS_GUARD = threading.Lock()
    _PROCESS_OWNED_RUNS = set()


def _ensure_process_finalization_context() -> tuple[int, identity.ProcessIdentity, str]:
    """Return fully initialized authority for this exact OS process."""
    global _SYSTEM_PROCESS_IDENTITY, _SYSTEM_FINALIZATION_OWNER_TOKEN
    if os.getpid() != _SYSTEM_PROCESS_PID:
        _reset_process_finalization_context()
    with _PROCESS_CONTEXT_GUARD:
        if _SYSTEM_PROCESS_IDENTITY is None:
            captured = identity.capture_identity(_SYSTEM_PROCESS_PID)
            if captured is None:
                raise SupervisorError(
                    "Could not establish the supervisor process identity."
                )
            _SYSTEM_PROCESS_IDENTITY = captured
        if _SYSTEM_FINALIZATION_OWNER_TOKEN is None:
            _SYSTEM_FINALIZATION_OWNER_TOKEN = uuid.uuid4().hex
        return (
            _SYSTEM_PROCESS_PID,
            _SYSTEM_PROCESS_IDENTITY,
            _SYSTEM_FINALIZATION_OWNER_TOKEN,
        )


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_process_finalization_context)

# `AICC_CLAUDE_BINARY` lets a test point a genuinely separate OS process
# (e.g. `scripts/execution_center_debug.py`, invoked as a real subprocess, not
# just monkeypatched in-process) at an executable test double instead of the
# real `claude` CLI — the in-process `fake_claude` test fixture cannot do
# this for a *different* process's fresh import of this module. Unset in
# every normal (non-test) invocation, so production behavior is unaffected.
CLAUDE_BINARY = os.environ.get("AICC_CLAUDE_BINARY") or "claude"

# No default timeout is applied *here* — `start_raw`'s `timeout_seconds`
# param means exactly what it says (`None` = no automatic timeout). A
# sensible default policy (e.g. "900s unless the caller opts out") belongs to
# the application layer (`ExecutionCenterAPI.start_run`), not this low-level
# executor.
DEFAULT_CANCEL_GRACE_SECONDS = 10.0
# A leader that exits naturally must not leave inherited-pipe holders behind.
# Give residual group members a short graceful window, then contain them with
# SIGKILL before releasing the pinned leader/pgid.
PROCESS_GROUP_DRAIN_GRACE_SECONDS = 1.0

# `cancel()` records the cancel flag with a compare-and-set on the run row's
# version. A concurrent *benign* field write that leaves the run RUNNING — in
# practice the once-per-run best-effort `first_output_at` handshake write from
# a reader thread (see `_record_handshake`) — bumps the version without
# changing state, and must not defeat a legitimate cancel. `cancel()` re-reads
# and retries the CAS while the run is still RUNNING; this bounds those retries
# so a pathological, unending stream of concurrent writes can never spin
# forever. Only one benign write can actually race a cancel today, so this is a
# generous ceiling, not a tuning knob.
_CANCEL_CAS_MAX_ATTEMPTS = 5

# Final process-state persistence can race the single cancel-request write in
# exactly the opposite direction: `_supervise()` may read RUNNING, then
# `cancel()` bumps the version before the terminal CAS lands. Re-read and
# reclassify so the persisted cancel request wins instead of leaving an exited
# child stuck in RUNNING. The retry is bounded for the same reason as the
# cancel-side CAS above.
# Canonical value lives in `run_finalizer` since the NIGHT-W9 extraction.
_TERMINAL_CAS_MAX_ATTEMPTS = run_finalizer.TERMINAL_CAS_MAX_ATTEMPTS

# How long a run must keep *looking* gone (pid still None, or its pid no longer
# resolves) before reconcile() from a NON-owning process terminalizes it to
# INTERRUPTED. This debounces the two cross-process race windows the audit
# flagged: a peer between `create_run` and `Popen` (pid still None), and a peer
# whose process exited but is still in its finalization window (about to CAS
# COMPLETED). A single-observation terminalization there silently INTERRUPTs a
# succeeding run out from under its owner and frees its workspace lock.
_RECONCILE_ABSENCE_GRACE_SECONDS = 5.0

# Bounds on the diagnostic output a provider may attach to a run, so a
# misbehaving CLI cannot flood the event log.
MAX_PROVIDER_DIAGNOSTIC_EVENTS = 64
MAX_PROVIDER_DIAGNOSTIC_BYTES = 32_768

# Defense in depth: `build_claude_command` never constructs these, but every
# command is checked against this set before it is ever handed to `Popen`.
_FORBIDDEN_FLAGS = frozenset({"--continue", "-c", "--background", "--bg"})


class SupervisorError(Exception):
    """Raised for a launch/cancel request that cannot be carried out."""


class InvalidCapabilityOverrideError(SupervisorError):
    """Raised by `start_raw` — before any subprocess, task, session, or run row
    is created — when a task carries an executor-capability override that is
    not a recognized profile. Fail closed: an invalid override is never
    silently ignored (which would hand the run whatever the default happened to
    be while the operator believed they had constrained it). Wraps
    `capabilities.InvalidCapabilityOverrideError` as a `SupervisorError` so
    existing `except SupervisorError` handlers surface it unchanged."""


class CapabilityMismatchError(SupervisorError):
    """Raised by `start_raw` when the executor-capability preflight fails: the
    capabilities the task requires (from its type and/or prompt intent) are not
    a subset of the capabilities the selected profile would grant. This is a
    *pre-spawn* rejection — the run row is recorded and transitioned straight to
    `FAILED` (with a `capability_mismatch:` `failure_reason` and a structured
    `capability_preflight` event) and **no `subprocess.Popen` is ever called**.
    Carries the persisted `run` row and the `capabilities.CapabilityDecision`
    so the caller can display exactly what was required vs. granted."""

    def __init__(self, run: dict, decision) -> None:
        self.run = run
        self.decision = decision
        super().__init__(decision.reason or "Executor capability mismatch.")


class WorkspaceVerificationFailed(SupervisorError):
    """Fail-closed launch rejection: the target workspace did not pass
    `workspace_provisioning.verify_workspace` (wrong branch, not an isolated
    worktree, belongs to another repository, ...). Subclasses `SupervisorError`
    so every caller that already catches `SupervisorError` (e.g. `app.py`'s
    launch handlers, `execution_queue.launch_ready`) refuses the launch with no
    new except clause, while carrying `.structured` (expected/actual workspace,
    expected/actual branch, failed step, remediation) for a caller that wants
    to render the failure in full. The process is never started."""

    def __init__(self, error: workspace_provisioning.WorkspaceVerificationError) -> None:
        self.structured = error.as_dict()
        self.failed_step = error.failed_step
        super().__init__(str(error))


class WorkspaceLockedError(SupervisorError):
    """Raised by `start_raw` when another run is already active
    (`db.EXECUTION_CENTER_ACTIVE_STATES`) against the same resolved
    workspace — wraps `db.WorkspaceLockedError` (the atomic, race-free check
    performed inside `db.create_run`'s own transaction) so callers that
    already catch `SupervisorError` (e.g. `app.py`'s launch handlers) need no
    new except clause, while a caller that wants the conflicting run
    specifically can catch this subclass and read `.conflicting_run`."""

    def __init__(self, conflicting_run: dict) -> None:
        self.conflicting_run = conflicting_run
        super().__init__(
            f"Workspace {conflicting_run['repository_path']!r} already has an active run "
            f"({conflicting_run['id']!r}, state={conflicting_run['state']!r}). Wait for it to "
            "finish or cancel it before launching again."
        )


class TaskAlreadyActiveError(SupervisorError):
    """Raised by `start_raw` when the launched task already has an active run
    (`db.EXECUTION_CENTER_ACTIVE_STATES`), possibly in a different workspace —
    wraps `db.TaskAlreadyActiveError` (the atomic, race-free per-task check
    inside `db.create_run`'s transaction). A `SupervisorError` subclass so the
    existing launch handlers that catch `SupervisorError` need no new except
    clause; a caller that wants the conflicting run can read `.conflicting_run`."""

    def __init__(self, conflicting_run: dict) -> None:
        self.conflicting_run = conflicting_run
        super().__init__(
            f"Task {conflicting_run['task_id']!r} already has an active run "
            f"({conflicting_run['id']!r}, state={conflicting_run['state']!r}). Wait for it to "
            "finish or cancel it before launching the same task again."
        )


class ProviderUnavailableError(SupervisorError):
    """The selected provider cannot be used safely on this machine."""


def build_claude_command(
    *,
    session_id: str,
    prompt: str,
    task_type: str,
    is_resume: bool,
    model: str | None = None,
    capability_override: str | None = None,
    untrusted: bool = False,
    operator_elevated: bool = False,
    prompt_in_argv: bool = True,
) -> list[str]:
    """Construct the exact `claude` argv for one run.

    Fresh run: `claude --session-id <uuid> -p <prompt> --output-format
    stream-json --include-partial-messages --verbose --setting-sources ""
    --permission-mode <profile mode>`. Resume: identical, except `--resume
    <uuid>` (the *exact* id — never a bare `--resume` picker, never
    `--continue`) in place of `--session-id`.

    `--permission-mode` (via `agent_runner.PERMISSION_MODE_BY_PROFILE`) was a
    genuine gap here until this fix: without it, the CLI's implicit default
    permission mode denies `Write`/`Edit` tool calls outright in headless
    `-p` mode — confirmed empirically against the real `claude` CLI — while
    the process itself still exits 0, so a `trusted_development` run could
    silently fail to make any of the changes it was asked for and still be
    recorded `COMPLETED` (see `agent_runner`'s profile docstring and
    `runtime.outcome` for the terminal-state classifier that also guards
    against exactly this). `agent_runner.build_command` (the v1 synchronous
    executor) already set this; this was the divergence between the two.
    """
    if capability_override is not None:
        profile = (
            agent_runner.PROFILE_READ_ONLY
            if capability_override.upper() == capabilities.PROFILE_READ_ONLY
            else agent_runner.PROFILE_TRUSTED_DEVELOPMENT
        )
    else:
        profile = agent_runner.profile_for_task(
            task_type, untrusted=untrusted, operator_elevated=operator_elevated
        )
    command = [CLAUDE_BINARY]
    if is_resume:
        command += ["--resume", session_id]
    else:
        command += ["--session-id", session_id]
    command += ["-p"]
    if prompt_in_argv:
        command.append(prompt)
    command += [
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--setting-sources",
        "",
        "--permission-mode",
        agent_runner.PERMISSION_MODE_BY_PROFILE[profile],
    ]
    # Key the tool set on the *resolved* profile, not the task type alone: an
    # untrusted task downgraded to read-only (audit D7) must get `--tools` (which
    # replaces the built-in set, so Bash does not exist for the run), never the
    # `--disallowedTools` pattern layer that still leaves Bash present.
    if profile == agent_runner.PROFILE_READ_ONLY:
        command += ["--tools", ",".join(agent_runner.READ_ONLY_ALLOWED_TOOLS)]
    else:
        command += ["--disallowedTools", ",".join(agent_runner.GIT_WRITE_DISALLOWED_TOOLS)]
    if model:
        command += ["--model", model]
    return command


def _assert_no_forbidden_flags(command: list[str]) -> None:
    hit = _FORBIDDEN_FLAGS.intersection(command)
    if hit:
        raise SupervisorError(f"Forbidden flag(s) constructed into command: {sorted(hit)}")


class _ActiveRun:
    def __init__(
        self,
        *,
        process: subprocess.Popen,
        run_id: str,
        # Default to Claude, which is what every run was before providers
        # existed: a caller that does not name one gets exactly the historical
        # behaviour rather than having to restate it.
        provider: providers.ExecutionProvider | None = None,
        provider_runtime: providers.ProviderRuntime | None = None,
    ) -> None:
        self.process = process
        self.run_id = run_id
        # `start_new_session=True` makes the spawned process the leader of a
        # new process group whose id is its launch-time pid. Keep that value;
        # resolving `getpgid(process.pid)` later, after the child may have
        # been reaped, opens a PID-reuse window.
        self.process_group_id = process.pid
        # Every poll/reap and signal decision is serialized through this
        # lock. A signal is therefore either sent while the unreaped child
        # still pins its pid/pgid, or skipped after exit was observed.
        self.process_control_lock = threading.RLock()
        # Serializes competing cancellation, timeout and supervision-failure
        # termination sequences so TERM/KILL escalation happens at most once
        # at a time.
        self.termination_lock = threading.Lock()
        # OS-process exit is distinct from durable terminal-state persistence
        # and from best-effort report finalization.
        self.leader_exited_event = threading.Event()
        self.process_exited_event = threading.Event()
        self.process_reaped_event = threading.Event()
        self.terminal_persisted_event = threading.Event()
        # Process exit and terminal persistence happen before report and
        # finalization writes. Synchronous callers wait for this distinct
        # ownership boundary before they may tear down the workspace.
        self.supervision_finished_event = threading.Event()
        # Which provider produced this process, and its per-run runtime state.
        # Ownership must know: reaping, signalling and finalization are all
        # provider-specific below.
        self.provider = provider if provider is not None else providers.get_provider(providers.CLAUDE_ID)
        self.provider_runtime = (
            provider_runtime if provider_runtime is not None else providers.ClaudeRuntime()
        )
        self.done_event = threading.Event()
        self.finalization_retry_lock = threading.Lock()
        # Set only when the child is confirmed gone but durable terminal
        # persistence exhausted its bounded retries. `reconcile()` and
        # `wait_for_run()` may then safely retry without mistaking slow normal
        # finalization for a failure.
        self.finalization_failed_event = threading.Event()
        # A failed post-Popen setup cleanup retains ownership and is retried by
        # a recovery thread, ``reconcile()``, and ``wait_for_run()``.
        self.launch_cleanup_failed_event = threading.Event()
        # Those three recovery paths may race. Serialize the complete cleanup
        # and terminal-persistence attempt so a contender that was waiting for
        # another path to release ownership re-checks ``done_event`` before it
        # can touch the run database again.
        self.launch_cleanup_retry_lock = threading.Lock()
        # Set by `_timeout_watchdog` (never by `cancel()`) only after a signal
        # was delivered, so `_supervise` can
        # tell a timeout-triggered termination apart from an explicit,
        # human-confirmed cancellation when it decides the final state.
        self.timeout_triggered = threading.Event()
        # Set the first time the provider runtime recognizes readiness
        # evidence — the in-memory guard that makes the `first_output_at` /
        # `handshake_received` write happen exactly once per run (see
        # `_record_handshake`), without re-reading the run row on every line.
        # `handshake_lock` makes the check-and-set atomic across the two
        # concurrent reader threads, so a run whose stdout and stderr both
        # produce their first line at the same instant still records the
        # milestone exactly once.
        self.handshake_recorded = threading.Event()
        self.handshake_lock = threading.Lock()
        self.valid_result_recorded = threading.Event()
        self._diagnostic_lines: list[str] = []
        self._diagnostic_bytes = 0
        self._diagnostic_lock = threading.Lock()

    def add_diagnostic(self, text: str) -> None:
        encoded = text.encode("utf-8", errors="replace")
        with self._diagnostic_lock:
            if len(self._diagnostic_lines) >= MAX_PROVIDER_DIAGNOSTIC_EVENTS:
                return
            remaining = MAX_PROVIDER_DIAGNOSTIC_BYTES - self._diagnostic_bytes
            if remaining <= 0:
                return
            bounded = encoded[:remaining].decode("utf-8", errors="ignore")
            self._diagnostic_lines.append(bounded)
            self._diagnostic_bytes += len(bounded.encode("utf-8"))

    def diagnostic_lines(self) -> list[str]:
        with self._diagnostic_lock:
            return list(self._diagnostic_lines)


def _capture_stable_process_identity(
    process: subprocess.Popen, *, attempts: int = 20, interval_seconds: float = 0.01
) -> identity.ProcessIdentity | None:
    """Wait briefly for shebang/interpreter exec transitions to settle.

    Persisting an identity from the transient `/usr/bin/env` phase makes the
    same live child look like PID reuse milliseconds later. Two consecutive
    identical captures retain full start-time+command protection while
    avoiding that false mismatch.
    """
    # Sample once, immediately, before spending any time stabilizing. A short
    # run can finish inside the stabilization window, and once it has, its
    # identity is no longer readable at all — so waiting first and asking later
    # would refuse to launch exactly the fastest, healthiest runs.
    immediate = identity.capture_identity(process.pid)
    previous = None
    consecutive = 0
    for _ in range(attempts):
        if process.poll() is not None:
            # Finished before the exec transitions settled. Its PID is pinned by
            # this unreaped handle and cannot have been reused, so the immediate
            # sample is exactly as trustworthy as two matching ones.
            return immediate

        current = identity.capture_identity(process.pid)
        if current is not None and previous is not None and current.as_string() == previous.as_string():
            consecutive += 1
            if consecutive >= 1:
                return current
        else:
            consecutive = 0
        previous = current
        time.sleep(interval_seconds)
    return immediate


class Supervisor:
    """One Supervisor instance per running Execution Center backend process.

    Holds an in-memory registry of runs it personally launched and can still
    signal/read from (`self._active`). This registry is intentionally *not*
    the source of truth — the SQLite `run` table is — because it cannot
    survive a Supervisor process restart (a child process's stdout pipe and
    waitable-child relationship both belong to the specific process that
    called `Popen`, not to "the Supervisor" as a concept). See `reconcile()`
    for how a fresh Supervisor instance handles what its predecessor left
    RUNNING.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        maintenance_token: str | None = None,
        maintenance_lock_handle: RuntimeLockHandle | None = None,
        enable_completion_autopilot: bool | None = None,
    ) -> None:
        self._creator_pid = os.getpid()
        owner_pid, owner_identity, owner_token = _ensure_process_finalization_context()
        self._finalization_owner_token = owner_token
        self._finalization_owner_pid = owner_pid
        self._finalization_owner_identity = owner_identity.as_string()
        self.db_path = db_path or db.resolve_db_path()
        owns_runtime_lock = maintenance_lock_handle is None
        self._runtime_lock_handle = maintenance_lock_handle or _acquire_runtime_lock(
            self.db_path, exclusive=False
        )
        try:
            fence = offline_cutover_fence_path(self.db_path)
            if fence.exists():
                expected = fence.read_text(encoding="utf-8").strip()
                if not expected or maintenance_token != expected:
                    raise SupervisorError(
                        "runtime is fenced for an offline finalization cutover"
                    )
            db.migrate(self.db_path)
            # Extracted sides of the supervisor (NIGHT-W9): stream consumption
            # and finalize/outcome persistence live behind small interfaces;
            # this class keeps orchestration (process lifecycle, `_active`
            # registry, locks).
            self._streams = stream_reader.StreamReader(self.db_path)
            self._finalizer = run_finalizer.RunFinalizer(
                self.db_path,
                owner_token=self._finalization_owner_token,
                owner_pid=self._creator_pid,
            )
            self._active: dict[str, _ActiveRun] = {}
            # Run ids this instance has committed to launching (persisted as
            # PREPARED/QUEUED) but has not yet `Popen`'d — the gap
            # `self._active` alone cannot cover, because `_active` is populated
            # only after a process actually exists and its ownership record is
            # constructed (see `_launch_process`). Guarded by the same
            # `_active_lock`. See `reconcile()` for why this must be included
            # in its "don't touch this, it's mine" guard.
            self._launching: set[str] = set()
            self._active_lock = threading.Lock()
            # Cross-process reconcile debounce (audit P0). A run can *look*
            # gone because another process owns it and is in-flight. Require
            # the observation to persist before a non-owner terminalizes it.
            self._suspected_gone: dict[str, float] = {}
            self._suspected_gone_lock = threading.Lock()
            self._reconcile_absence_grace = _RECONCILE_ABSENCE_GRACE_SECONDS
            self._autopilot_thread: threading.Thread | None = None
            self._autopilot_stop: threading.Event | None = None
            # Opt-in: auto-start completion processing for a backend process.
            autopilot_enabled = (
                bool(os.environ.get("AICC_COMPLETION_AUTOPILOT"))
                if enable_completion_autopilot is None
                else enable_completion_autopilot
            )
            if autopilot_enabled:
                self.start_completion_autopilot()
        except BaseException:
            if owns_runtime_lock:
                try:
                    _release_runtime_lock(self._runtime_lock_handle)
                except Exception:
                    logger.exception("Could not release failed Supervisor startup lock")
            raise

    def _assert_current_process(self) -> None:
        """Reject a Supervisor facade inherited across ``fork``.

        Its locks, process handles, active registry and finalizer all belong to
        the parent crash domain.  A child must construct a fresh facade after
        the at-fork hook has established child-local authority.
        """
        if os.getpid() != self._creator_pid:
            raise SupervisorError(
                "A Supervisor inherited across fork cannot be used; "
                "construct a fresh Supervisor in the child process."
            )

    # ------------------------------------------------------------------
    # Completion pipeline (AICC-AUTONOMY-001) — advances the post-execution
    # lifecycle *independently* of the original Claude process. Bounded (only
    # due, non-terminal rows) and never blocks the UI: it is invoked either
    # from the opt-in background autopilot thread below or on demand.
    # ------------------------------------------------------------------

    def advance_completions(self, *, now=None, limit: int = 50, github=None) -> list:
        self._assert_current_process()
        from command_center.runtime.completion_service import CompletionOrchestrator

        orchestrator = CompletionOrchestrator(self.db_path, github=github)
        return orchestrator.advance_pending(now=now, limit=limit)

    def start_completion_autopilot(self, *, interval_seconds: float = 30.0) -> None:
        """Start a bounded, daemon background poller that advances due
        completion rows. Idempotent — a second call while one is running is a
        no-op. This is the "Supervisor advances completion workflows
        independently of the Claude process" integration, kept off the UI
        thread so completion validation/GitHub calls never block rendering."""
        self._assert_current_process()
        if self._autopilot_thread is not None and self._autopilot_thread.is_alive():
            return
        stop = threading.Event()
        self._autopilot_stop = stop

        def _loop() -> None:
            while not stop.wait(interval_seconds):
                try:
                    self.advance_completions()
                except Exception:  # noqa: BLE001 - one bad tick must never kill the poller
                    continue

        thread = threading.Thread(target=_loop, name="completion-autopilot", daemon=True)
        self._autopilot_thread = thread
        thread.start()

    def stop_completion_autopilot(self) -> None:
        self._assert_current_process()
        if self._autopilot_stop is not None:
            self._autopilot_stop.set()

    # ------------------------------------------------------------------
    # Launch
    # ------------------------------------------------------------------

    def start_raw(
        self,
        *,
        project: str,
        repository_path: str,
        task_type: str,
        prompt: str,
        confirmed: bool,
        task_id: str | None = None,
        title: str | None = None,
        session_id: str | None = None,
        is_resume: bool = False,
        model: str | None = None,
        timeout_seconds: int | None = None,
        expected_branch: str | None = None,
        launch_source: str | None = None,
        prompt_version: int | None = None,
        capability_override: str | None = None,
        repository_already_validated: bool = False,
        workspace_verification: workspace_provisioning.WorkspaceSpec | None = None,
        executor_id: str = providers.CLAUDE_ID,
        provider_route_ids: tuple[str, ...] | None = None,
        max_provider_attempts: int | None = None,
        provider_route_reason: str = "explicit_single_provider",
        provider_policy_version: str = "project_allowed_agents_v1",
        canonical_repository_path: str | None = None,
        max_global_concurrency: int | None = None,
        untrusted: bool = False,
        operator_elevated: bool = False,
    ) -> dict:
        """Prepare and launch a run from an already-final `prompt` string.

        `workspace_verification`, when given, is the **fail-closed workspace
        gate** — this is the single chokepoint every v2 task launch funnels
        through, so verifying here means no launch path (Kanban launcher,
        execution queue, portfolio, or a future caller of `start_run`) can
        bypass it. Before any run row is created or any process is spawned,
        `workspace_provisioning.verify_workspace` must pass; if it does not
        (wrong branch, not an isolated worktree, workspace belongs to another
        repository, dirty tree under a strict policy, ...) this raises
        `WorkspaceVerificationFailed` and the process is never started. The
        passing `VerificationEvidence` is recorded as a `workspace_verified`
        lifecycle event, so "Workspace Verified" is only ever emitted after
        every check has actually passed. `None` (the default) preserves the
        original behavior for non-task callers (chat, ad-hoc runs, tests) that
        have no isolated-worktree concept.

        `expected_branch`/`launch_source`/`prompt_version` are opaque,
        write-once Live Execution Center v2 metadata (see
        `command_center.runtime.session_view`/`task_sync`) — this method
        never inspects or validates them, just forwards them to
        `db.create_run` for later display/sync.

        `repository_already_validated`, default `False`, preserves the
        original v2 behavior for every existing caller: `repository_path`
        must equal `project`'s *configured* `repository_path`
        (`agent_runner.validate_repository`'s security boundary against an
        arbitrary/untrusted path). Set it only when the caller has already
        independently validated `repository_path` through an equivalent or
        stronger check — today, only `launch_service.execute_agent_launch_v2`
        does, via `launch.validate_launch` (existence, is-a-directory,
        is-a-git-repo) on the exact path `launch.resolve_workspace_path`
        already resolved (task workspace, else project default workspace,
        else project repository — see `docs/adr`). This is what makes a
        task's own worktree on its own feature branch launchable at all: the
        v1.2 synchronous flow (`agent_runner.run_claude_code`) never enforced
        project-repository equality either, only `launch.validate_launch`'s
        checks, so this keeps the v2 bridge exactly as permissive as the
        flow it replaces — never more.

        **Internal/low-level.** This method executes `prompt` verbatim — it
        does not call `context_service.assemble_context` and does not know
        or care whether `project` is sensitive (BANK/LEGAL). Application code
        that launches a run against a real project must go through
        `ExecutionCenterAPI.start_run` instead, which assembles the prompt
        through `context_service` before ever reaching here. This method
        exists for `ExecutionCenterAPI` itself to call (once it has already
        built a safe prompt) and for tests that need to exercise process
        lifecycle mechanics directly without the context-assembly layer.

        `timeout_seconds=None` means no automatic timeout — the run stays
        `RUNNING` until it exits on its own or is explicitly cancelled. Pass
        a number to enable the timeout watchdog (see `_timeout_watchdog`).

        Returns the run row once the subprocess has been started (state
        `RUNNING`), or a row left in state `FAILED` if `Popen` itself could
        not start the process (e.g. the `claude` binary is missing) — this
        method never raises for that specific failure mode, matching
        `agent_runner.run_claude_code`'s existing convention. It *does*
        raise before any subprocess is spawned for: missing confirmation,
        an unconfigured/mismatched repository path, or an invalid resume
        request (no such session).
        """
        self._assert_current_process()
        # Pre-launch gates, in the only order that is safe. Provider resolution
        # first, because every gate below is phrased in terms of it; then the
        # project-level policy on which providers may run at all; then the
        # human confirmation, which must precede every mutation; then the
        # provider's own readiness. Nothing here has yet touched the database
        # or the filesystem.
        route_ids = provider_route_ids or (executor_id,)
        route = provider_route.ProviderRoute(
            route_ids,
            max_attempts=max_provider_attempts or len(route_ids),
            selection_reason=provider_route_reason,
            policy_version=provider_policy_version,
        )
        if route.providers[0] != executor_id:
            raise SupervisorError("executor_id must be the first provider route candidate")
        for candidate in route.providers:
            providers.get_provider(candidate)
            project_config.require_execution_provider_allowed(project, candidate)
        provider = providers.get_provider(executor_id)
        context_service.require_launch_confirmation(confirmed, what=f"Launching a {provider.label} run")

        # Process-group supervision is a hard host requirement, independent of
        # provider: without `waitid(WNOWAIT)` this class cannot own a process
        # tree safely, so it refuses to spawn one at all rather than degrade.
        if os.name != "posix" or not all(
            hasattr(os, name)
            for name in ("waitid", "P_PID", "WEXITED", "WNOHANG", "WNOWAIT")
        ):
            raise SupervisorError(
                "Process-group supervision requires a POSIX host with "
                "waitid(WNOWAIT); this runtime does not provide equivalent "
                "process-tree ownership."
            )

        availability = provider.availability()
        if executor_id != providers.CLAUDE_ID and not availability.available:
            raise ProviderUnavailableError(f"{provider.label} unavailable ({availability.code}): {availability.message}")
        try:
            provider.validate_prompt(prompt)
        except ValueError as exc:
            raise ProviderUnavailableError(str(exc)) from exc

        supplied_repo_path = Path(repository_path).expanduser()
        if provider.requires_dedicated_worktree:
            supplied_absolute = supplied_repo_path.absolute()
            if supplied_absolute != supplied_absolute.resolve() or supplied_repo_path.is_symlink():
                raise SupervisorError("Unsafe Codex target: symlinked worktree paths are not permitted.")

        if repository_already_validated:
            repo_path = supplied_repo_path.resolve()
            if not repo_path.is_dir():
                raise SupervisorError(f"Workspace not found: {repo_path}")
        else:
            repo_path = agent_runner.validate_repository(project, repository_path)

        # Resolve the executor-capability decision up front (before any task/
        # session/run row is created). An invalid override fails closed here,
        # with nothing persisted — it is a configuration error, not a run.
        try:
            decision = capabilities.decide(task_type, prompt, capability_override)
        except capabilities.InvalidCapabilityOverrideError as exc:
            raise InvalidCapabilityOverrideError(str(exc)) from exc

        # Fail-closed workspace gate — the single chokepoint no v2 task launch
        # can bypass. Runs before any run row exists or any process is spawned;
        # its passing evidence is the *only* authorization to emit "Workspace
        # Verified" (recorded below, once the run row exists to attach it to).
        verification_evidence = None
        if workspace_verification is not None:
            verified_path = Path(workspace_verification.workspace_path).expanduser().resolve()
            if verified_path != repo_path:
                mismatch = workspace_provisioning.WorkspaceVerificationError(
                    failed_step="workspace_matches_launch_path",
                    remediation="Use the verified workspace as repository_path; never authorize a different cwd.",
                    expected_workspace=str(verified_path),
                    actual_workspace=str(repo_path),
                    expected_branch=workspace_verification.expected_branch,
                    detail="workspace verification spec does not match the process launch directory",
                )
                raise WorkspaceVerificationFailed(mismatch)
            try:
                verification_evidence = workspace_provisioning.verify_workspace(workspace_verification)
            except workspace_provisioning.WorkspaceVerificationError as exc:
                raise WorkspaceVerificationFailed(exc) from exc

        # Provider-specific workspace requirement, on top of (never instead of)
        # the fail-closed gate above.
        if provider.requires_dedicated_worktree:
            self._validate_dedicated_worktree(
                repo_path,
                canonical_repository_path=canonical_repository_path,
                expected_branch=expected_branch,
            )

        # Capability-denied launches must fail without starting *any* subprocess,
        # including the read-only git commands used to capture provenance.
        # Their run row is still persisted below, with the unavailable Git facts
        # left honestly unknown.
        launch_snapshot = agent_runner.git_snapshot(repo_path) if decision.ok else {}
        observed_branch = launch_snapshot.get("branch")
        if observed_branch == "(detached HEAD)":
            observed_branch = None
        provenance_base_branch = (
            workspace_verification.base_branch if workspace_verification is not None else None
        )
        provenance_repository = (
            workspace_verification.repository_path
            if workspace_verification is not None and workspace_verification.repository_path
            else canonical_repository_path
        )
        provenance_base_sha = (
            agent_runner.git_commit(repo_path, provenance_base_branch)
            if provenance_base_branch and decision.ok
            else launch_snapshot.get("head")
        )

        safe_default_title = "Codex CLI run" if executor_id == providers.CODEX_ID else prompt[:120]
        persisted_title = safe_default_title if executor_id == providers.CODEX_ID else (title or safe_default_title)
        if task_id is None:
            task = db.create_task(
                self.db_path, project=project, title=persisted_title, task_type=task_type
            )
            task_id = task["id"]
        elif db.get_task(self.db_path, task_id) is None:
            db.create_task(
                self.db_path,
                project=project,
                title=persisted_title,
                task_type=task_type,
                task_id=task_id,
            )

        if is_resume:
            if not session_id:
                raise SupervisorError("Resuming a session requires an explicit session_id.")
            session = db.get_session(self.db_path, session_id)
            if session is None:
                raise SupervisorError(f"No such session to resume: {session_id!r}")
        else:
            session = db.create_session(
                self.db_path,
                task_id=task_id,
                project=project,
                repository_path=str(repo_path),
                session_id=session_id,
            )
            session_id = session["id"]

        try:
            spec = provider.build_launch(
                repository_path=repo_path,
                session_id=session_id,
                prompt=prompt,
                task_type=task_type,
                is_resume=is_resume,
                model=model,
                untrusted=untrusted,
                operator_elevated=operator_elevated,
                capability_override=decision.override,
            )
        except (RuntimeError, ValueError) as exc:
            raise ProviderUnavailableError(str(exc)) from exc
        command = list(spec.argv)
        if executor_id == providers.CLAUDE_ID:
            _assert_no_forbidden_flags(command)

        try:
            # Keep creation and in-memory ownership registration atomic with
            # `reconcile()`'s active-id snapshot plus active-row query. Without
            # this shared lock, a dashboard refresh can observe the committed
            # PREPARED row in the few instructions before `_launching.add()`
            # and incorrectly transition this live launch to INTERRUPTED.
            # The lock covers one short SQLite transaction only; it is released
            # before git inspection or subprocess creation.
            with self._active_lock:
                run = db.create_run(
                    self.db_path,
                    session_id=session_id,
                    task_id=task_id,
                    project=project,
                    task_type=task_type,
                    repository_path=str(repo_path),
                    prompt=(
                        "[redacted: prompt transported via stdin]"
                        if spec.stdin_text is not None
                        else prompt
                    ),
                    is_resume=is_resume,
                    timeout_seconds=timeout_seconds,
                    command=command,
                    expected_branch=expected_branch,
                    launch_source=launch_source,
                    prompt_version=prompt_version,
                    capability_profile=decision.selected_profile,
                    capability_override=decision.override,
                    required_capabilities=",".join(decision.required_capabilities),
                    granted_capabilities=",".join(decision.granted_capabilities),
                    capability_preflight="ok" if decision.ok else "mismatch",
                    command_policy=decision.command_policy,
                    provider_id=executor_id,
                    provider_metadata_json=providers.audit_json(spec.audit_metadata),
                    provider_route=route.providers,
                    max_provider_attempts=route.max_attempts,
                    provider_route_reason=route.selection_reason,
                    provider_policy_version=route.policy_version,
                    canonical_repository_path=(
                        str(Path(provenance_repository).expanduser().resolve())
                        if provenance_repository
                        else None
                    ),
                    worktree_path=str(repo_path),
                    branch=observed_branch,
                    base_branch=provenance_base_branch,
                    base_sha=provenance_base_sha,
                    head_sha=launch_snapshot.get("head"),
                    finalization_owner_token=self._finalization_owner_token,
                    finalization_owner_pid=self._finalization_owner_pid,
                    finalization_owner_identity=self._finalization_owner_identity,
                    enforce_workspace_lock=True,
                    max_global_concurrency=max_global_concurrency,
                )
                self._launching.add(run["id"])
                with _PROCESS_OWNED_RUNS_GUARD:
                    _PROCESS_OWNED_RUNS.add(run["id"])
        except db.WorkspaceLockedError as exc:
            raise WorkspaceLockedError(exc.conflicting_run) from exc
        except db.TaskAlreadyActiveError as exc:
            raise TaskAlreadyActiveError(exc.conflicting_run) from exc

        # Executor capability preflight (Required fix 5). The decision itself is
        # already persisted on the run row's `capability_*`/`command_policy`
        # columns for every run (see `db.create_run` above). If the capabilities
        # the task requires (from its type and/or its prompt's own intent) are
        # not covered by the profile it will be granted, fail the run right here
        # — while it is still PREPARED, before the QUEUED transition, and before
        # any `subprocess.Popen` — with a `capability_mismatch:` failure_reason
        # the UI renders as a blocking preflight error distinct from a process
        # exit. A structured `capability_preflight` event plus a
        # `capability_preflight_failed` lifecycle event record the full decision
        # for the audit trail. The run stays guarded in `self._launching`
        # through the FAILED transition so a concurrent `reconcile()` never
        # races it to INTERRUPTED.
        if not decision.ok:
            try:
                db.append_run_event(self.db_path, run["id"], "capability_preflight", decision.as_metadata())
                run = db.update_run_state(
                    self.db_path,
                    run["id"],
                    expected_version=run["version"],
                    new_state="FAILED",
                    fields={
                        "completed_at": iso_now(),
                        "failure_reason": capabilities.failure_reason_code(decision),
                    },
                )
                db.append_run_event(
                    self.db_path,
                    run["id"],
                    "lifecycle",
                    stream_parser.lifecycle_event(
                        "capability_preflight_failed",
                        selected_profile=decision.selected_profile,
                        required=decision.required_capabilities,
                        granted=decision.granted_capabilities,
                        missing=decision.missing_capabilities,
                        reason=decision.reason,
                    )["payload"],
                )
                if not self._complete_owned_terminal_finalization(
                    run["id"],
                    exit_code=None,
                    lifecycle="capability_preflight_failed",
                ):
                    raise SupervisorError(
                        f"Run {run['id']!r} capability failure was not durably finalized."
                    )
                with _PROCESS_OWNED_RUNS_GUARD:
                    _PROCESS_OWNED_RUNS.discard(run["id"])
            finally:
                with self._active_lock:
                    self._launching.discard(run["id"])
            raise CapabilityMismatchError(db.get_run(self.db_path, run["id"]), decision)

        try:
            if verification_evidence is not None:
                db.append_run_event(
                    self.db_path,
                    run["id"],
                    "lifecycle",
                    stream_parser.lifecycle_event(
                        "workspace_verified", **verification_evidence.as_payload()
                    )["payload"],
                )
            run = db.update_run_state(self.db_path, run["id"], expected_version=run["version"], new_state="QUEUED")
            pre_run_snapshot = launch_snapshot
            pre_run_status = pre_run_snapshot.get("status_summary")
            run = db.update_run_fields(
                self.db_path,
                run["id"],
                expected_version=run["version"],
                fields={
                    "pre_run_git_status": pre_run_status,
                    # Short HEAD at launch. A committed change leaves the working
                    # tree clean, so the porcelain diff alone cannot tell "agent
                    # committed" from "agent did nothing"; comparing pre/post HEAD
                    # can. See `outcome.classify_process_result`.
                    "pre_run_head": pre_run_snapshot.get("head"),
                },
            )
        except Exception:
            # `_launch_process` (which owns clearing `self._launching` on
            # every path it can reach — success or a failed `Popen`) was
            # never entered, so nothing else will clear this run out of
            # `_launching` if it's left here.
            terminal_persisted = self._persist_run_failure(
                run["id"],
                exit_code=None,
                failure_reason="launch_preparation_failed",
                lifecycle="launch_preparation_failed",
            )
            if terminal_persisted:
                try:
                    terminal_persisted = self._complete_owned_terminal_finalization(
                        run["id"],
                        exit_code=None,
                        lifecycle="launch_preparation_failed",
                    )
                except Exception:
                    logger.exception(
                        "Could not finalize launch preparation failure for run %s",
                        run["id"],
                    )
                    terminal_persisted = False
            with self._active_lock:
                self._launching.discard(run["id"])
            if terminal_persisted:
                with _PROCESS_OWNED_RUNS_GUARD:
                    _PROCESS_OWNED_RUNS.discard(run["id"])
            raise

        return self._launch_process(run, spec, repo_path, provider)

    @staticmethod
    def _validate_dedicated_worktree(
        repo_path: Path, *, canonical_repository_path: str | None, expected_branch: str | None
    ) -> None:
        """Fail closed unless Codex targets the intended registered feature worktree."""
        from command_center import worktree_launcher

        if not canonical_repository_path:
            raise SupervisorError("Codex launch requires the project's canonical repository path.")
        canonical = Path(canonical_repository_path).expanduser().resolve()
        if repo_path == canonical:
            raise SupervisorError("Unsafe Codex target: the canonical checkout cannot be used for execution.")
        if not expected_branch:
            raise SupervisorError("Codex launch requires the intended task branch.")
        validation = worktree_launcher.validate_worktree(
            repository_root=canonical,
            worktree_path=repo_path,
            expected_branch=expected_branch,
            require_clean=True,
        )
        if not validation.can_launch:
            detail = "; ".join(validation.errors) or "worktree validation failed"
            raise SupervisorError(f"Unsafe Codex target worktree: {detail}")

    def _launch_process(
        self,
        run: dict,
        spec: providers.LaunchSpec,
        repo_path: Path,
        provider: providers.ExecutionProvider,
    ) -> dict:
        run_id = run["id"]
        provider_runtime = provider.create_runtime(
            prompt=spec.stdin_text if spec.stdin_text is not None else "",
            environment=spec.environment,
        )
        try:
            return self._launch_process_unguarded(
                run, spec, repo_path, provider, provider_runtime
            )
        finally:
            # Whatever happened above — a successful launch (`self._active`
            # now has `run_id`) or a failed `Popen` (state already FAILED) —
            # this run is no longer "committed to being launched but not yet
            # observable" (see `start_raw`'s `self._launching.add`). Runs
            # before `self._active[run_id] = active` so there is never a gap
            # where `run_id` is in neither set for a concurrent `reconcile()`
            # to see through.
            with self._active_lock:
                self._launching.discard(run_id)

    def _launch_process_unguarded(
        self,
        run: dict,
        spec: providers.LaunchSpec,
        repo_path: Path,
        provider: providers.ExecutionProvider,
        provider_runtime: providers.ProviderRuntime,
    ) -> dict:
        run_id = run["id"]
        db.start_provider_attempt(
            self.db_path,
            run_id=run_id,
            attempt_number=1,
            provider_id=provider.id,
            started_at=iso_now(),
        )
        try:
            process = subprocess.Popen(
                list(spec.argv),
                cwd=repo_path,
                stdin=subprocess.PIPE if spec.stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Strip Git/GitHub push/merge credentials from the agent's
                # environment (H1): the pipeline, never the launched agent, owns
                # remote writes. See agent_runner.scrub_vcs_credentials.
                env=agent_runner.scrub_vcs_credentials(spec.environment),
                text=True,
                bufsize=1,
                shell=False,
                start_new_session=True,
            )
        except OSError as exc:
            failure_reason = "executable_missing" if isinstance(exc, FileNotFoundError) else "provider_launch_failed"
            db.finish_provider_attempt(
                self.db_path,
                run_id=run_id,
                attempt_number=1,
                outcome="failed",
                classification=provider_route.classify_failure(failure_reason),
                disposition=provider_route.TERMINAL,
                error_code=failure_reason,
                completed_at=iso_now(),
            )
            run = db.update_run_state(
                self.db_path,
                run_id,
                expected_version=run["version"],
                new_state="FAILED",
                fields={"completed_at": iso_now(), "failure_reason": failure_reason},
            )
            self._append_lifecycle_event_best_effort(
                "launch_failed", run_id, error=str(exc), error_type=type(exc).__name__
            )
            if not self._complete_owned_terminal_finalization(
                run_id, exit_code=None, lifecycle="launch_failed"
            ):
                raise SupervisorError(
                    f"Run {run_id!r} launch failure was not durably finalized."
                ) from exc
            with _PROCESS_OWNED_RUNS_GUARD:
                _PROCESS_OWNED_RUNS.discard(run_id)
            return run

        pid = process.pid
        try:
            active = _ActiveRun(
                process=process,
                run_id=run_id,
                provider=provider,
                provider_runtime=provider_runtime,
            )
        except Exception as exc:
            # Even construction of the process-local ownership record is
            # inside the post-Popen safety boundary.
            exited = self._terminate_unregistered_process(process, process_group_id=pid)
            if exited:
                terminal_persisted = self._persist_run_failure(
                    run_id,
                    exit_code=process.returncode,
                    failure_reason="launch_setup_failed",
                    lifecycle="launch_failed",
                )
                if terminal_persisted:
                    terminal_persisted = self._complete_owned_terminal_finalization(
                        run_id,
                        exit_code=process.returncode,
                        lifecycle="launch_failed",
                    )
                if terminal_persisted:
                    with _PROCESS_OWNED_RUNS_GUARD:
                        _PROCESS_OWNED_RUNS.discard(run_id)
            self._append_lifecycle_event_best_effort(
                "launch_failed", run_id, error=str(exc), error_type=type(exc).__name__, pid=pid
            )
            raise

        # Ownership starts at Popen, before any fallible identity/SQLite/thread
        # setup. A concurrent reconcile therefore never sees a live child as
        # orphaned during this window.
        with self._active_lock:
            self._active[run_id] = active

        try:
            # Stable capture, not a single sample: a process caught mid-exec
            # (`/usr/bin/env` -> interpreter) yields an identity that no longer
            # matches milliseconds later, which reconciliation would read as PID
            # reuse for a perfectly healthy child.
            proc_identity = _capture_stable_process_identity(process)
            if proc_identity is None:
                raise SupervisorError(f"Could not capture process identity for spawned pid {pid}.")
            # Persist pid, restart-safe identity and RUNNING atomically. There
            # is no intermediate QUEUED row carrying only part of the
            # ownership evidence.
            run = db.update_run_state(
                self.db_path,
                run_id,
                expected_version=run["version"],
                new_state="RUNNING",
                fields={
                    "pid": pid,
                    "process_start_identity": proc_identity.as_string(),
                    "started_at": iso_now(),
                },
            )
        except Exception as exc:
            self._abort_launch_after_popen(run_id, active, exc)
            raise

        # The PID and RUNNING transition above are lifecycle correctness; this
        # append-only event is audit telemetry. A transient event-store failure
        # must not kill an otherwise registered, healthy child process.
        self._append_lifecycle_event_best_effort("process_started", run_id, pid=pid)

        try:
            # This is the only background-thread start that remains in the
            # synchronous launch path. Watchdog and reader threads are started
            # inside `_supervise`, whose exception boundary owns terminate /
            # reap cleanup.
            self._start_daemon_thread(
                target=self._supervise,
                args=(run_id, active, repo_path, run.get("timeout_seconds"), spec.stdin_text),
                name=f"run-supervisor-{run_id}",
            )
        except Exception as exc:
            self._abort_launch_after_popen(run_id, active, exc)
            raise

        return run

    @staticmethod
    def _start_daemon_thread(*, target, args: tuple, name: str) -> threading.Thread:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        thread.start()
        return thread

    @staticmethod
    def _terminate_unregistered_process(
        process: subprocess.Popen,
        *,
        process_group_id: int,
    ) -> bool:
        """Terminate/reap a child before an `_ActiveRun` could be installed."""
        try:
            if process.poll() is None:
                try:
                    os.killpg(process_group_id, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                except OSError:
                    logger.exception(
                        "Could not send SIGTERM to unregistered process group %s",
                        process_group_id,
                    )
                try:
                    process.wait(timeout=DEFAULT_CANCEL_GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process_group_id, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    except OSError:
                        logger.exception(
                            "Could not send SIGKILL to unregistered process group %s",
                            process_group_id,
                        )
                    try:
                        process.wait(timeout=DEFAULT_CANCEL_GRACE_SECONDS)
                    except subprocess.TimeoutExpired:
                        logger.error(
                            "Spawned process %s did not exit after SIGKILL",
                            process.pid,
                        )
            exited = process.poll() is not None
            members = Supervisor._live_process_group_members(
                process_group_id,
                leader_pid=process.pid,
            )
            group_termination_confirmed = members == []
            if exited and (members is None or members):
                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                    group_termination_confirmed = True
                except ProcessLookupError:
                    group_termination_confirmed = True
                except OSError:
                    logger.exception(
                        "Could not kill residual unregistered process group %s",
                        process_group_id,
                    )
                    return False
                deadline = time.monotonic() + DEFAULT_CANCEL_GRACE_SECONDS
                while time.monotonic() < deadline:
                    members = Supervisor._live_process_group_members(
                        process_group_id,
                        leader_pid=process.pid,
                    )
                    if not members:
                        break
                    time.sleep(0.02)
                if members:
                    group_termination_confirmed = False
            return exited and group_termination_confirmed
        finally:
            for pipe in (process.stdout, process.stderr):
                if pipe is not None:
                    try:
                        pipe.close()
                    except OSError:
                        pass

    @staticmethod
    def _live_process_group_members(
        process_group_id: int,
        *,
        leader_pid: int,
    ) -> list[int] | None:
        """Return live non-leader members, or ``None`` if inspection failed.

        The unreaped leader pins the launch-time pgid while this probe runs, so
        a later signal cannot be redirected to an unrelated reused id. Zombie
        descendants are already non-running and hold no open pipes, so they do
        not block ownership release.
        """
        process = None
        try:
            process = _SYSTEM_POPEN(
                ["ps", "-axo", "pid=,pgid=,state="],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
            )
            stdout, _stderr = process.communicate(timeout=2)
            if process.returncode != 0:
                return None
        except (OSError, subprocess.SubprocessError, ValueError):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            return None

        members = []
        for line in stdout.splitlines():
            parts = line.split(None, 2)
            if len(parts) != 3:
                continue
            try:
                pid = int(parts[0])
                pgid = int(parts[1])
            except ValueError:
                continue
            state = parts[2]
            if pgid == process_group_id and pid != leader_pid and not state.startswith("Z"):
                members.append(pid)
        return members

    @staticmethod
    def _observe_leader_exit_locked(active: _ActiveRun) -> bool:
        """Observe direct-child exit without reaping the process-group leader."""
        if active.leader_exited_event.is_set():
            return True
        try:
            result = os.waitid(
                os.P_PID,
                active.process.pid,
                os.WEXITED | os.WNOHANG | os.WNOWAIT,
            )
        except ChildProcessError:
            # A test double or an unexpected external waiter may not expose a
            # waitable child. Fall back conservatively to Popen's own status.
            if active.process.poll() is None:
                return False
            active.process_reaped_event.set()
            active.leader_exited_event.set()
            return True
        if result is None:
            return False
        active.leader_exited_event.set()
        return True

    def _finish_exited_process_group_locked(
        self,
        run_id: str,
        active: _ActiveRun,
        *,
        lifecycle_prefix: str,
        group_term_sent: bool,
        grace_deadline: float | None,
    ) -> tuple[bool, bool]:
        """Drain descendants, reap the pinned leader, and report signal use.

        Returns ``(fully_terminated, signal_sent)``. The caller holds
        ``process_control_lock`` throughout, and the leader stays unreaped
        until every live member is gone, preventing pgid reuse.
        """
        if active.process_exited_event.is_set():
            return True, False
        if not active.leader_exited_event.is_set():
            return False, False

        members = self._live_process_group_members(
            active.process_group_id,
            leader_pid=active.process.pid,
        )
        signal_sent = False

        if members and not group_term_sent:
            term_sent = self._signal_process_group_locked(active, signal.SIGTERM)
            signal_sent = signal_sent or term_sent
            lifecycle = (
                f"{lifecycle_prefix}_residual_sigterm_sent"
                if term_sent
                else f"{lifecycle_prefix}_residual_sigterm_failed"
            )
            self._append_lifecycle_event_best_effort(
                lifecycle,
                run_id,
                pid=active.process.pid,
                descendant_pids=members,
            )
            if not term_sent:
                return False, signal_sent
            grace_deadline = time.monotonic() + PROCESS_GROUP_DRAIN_GRACE_SECONDS

        if members and grace_deadline is not None:
            while members and time.monotonic() < grace_deadline:
                time.sleep(0.02)
                members = self._live_process_group_members(
                    active.process_group_id,
                    leader_pid=active.process.pid,
                )
                if members is None:
                    break

        # Unknown membership is handled fail-closed: SIGKILL the still-pinned
        # group before reaping the leader. Known live descendants are the
        # normal escalation case.
        if members is None or members:
            kill_sent = self._signal_process_group_locked(active, signal.SIGKILL)
            signal_sent = signal_sent or kill_sent
            lifecycle = (
                f"{lifecycle_prefix}_sigkill_sent"
                if kill_sent
                else f"{lifecycle_prefix}_sigkill_failed"
            )
            self._append_lifecycle_event_best_effort(
                lifecycle,
                run_id,
                pid=active.process.pid,
                descendant_pids=members,
            )
            if not kill_sent:
                return False, signal_sent

            verify_deadline = time.monotonic() + DEFAULT_CANCEL_GRACE_SECONDS
            while time.monotonic() < verify_deadline:
                members = self._live_process_group_members(
                    active.process_group_id,
                    leader_pid=active.process.pid,
                )
                if members is None or not members:
                    break
                time.sleep(0.02)
            if members:
                return False, signal_sent

        if not active.process_reaped_event.is_set():
            try:
                active.process.wait(timeout=0)
            except subprocess.TimeoutExpired:
                return False, signal_sent
            active.process_reaped_event.set()
        active.process_exited_event.set()
        return True, signal_sent

    def _wait_for_process_exit(
        self,
        run_id: str,
        active: _ActiveRun,
        *,
        timeout: float | None,
        lifecycle_prefix: str,
        group_term_sent: bool = False,
        grace_deadline: float | None = None,
    ) -> int:
        """Wait for and fully contain the Supervisor-owned process group.

        ``waitid(WNOWAIT)`` observes the leader without releasing its pid/pgid.
        Descendants are drained before the leader is reaped, so reader pipes
        cannot be held open by an orphan and no late signal can hit a reused
        process group.
        """
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        while True:
            with active.process_control_lock:
                if self._observe_leader_exit_locked(active):
                    terminated, _signal_sent = self._finish_exited_process_group_locked(
                        run_id,
                        active,
                        lifecycle_prefix=lifecycle_prefix,
                        group_term_sent=group_term_sent,
                        grace_deadline=grace_deadline,
                    )
                    if not terminated:
                        raise SupervisorError(
                            f"Process group {active.process_group_id} for run "
                            f"{run_id!r} could not be confirmed terminated."
                        )
                    return active.process.returncode
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(
                        getattr(active.process, "args", []),
                        timeout,
                    )
                active.process_exited_event.wait(timeout=min(0.02, remaining))
            else:
                active.process_exited_event.wait(timeout=0.02)

    @staticmethod
    def _signal_process_group_locked(active: _ActiveRun, sig: signal.Signals) -> bool:
        """Send to the captured pgid; return true only after successful killpg."""
        try:
            os.killpg(active.process_group_id, sig)
            return True
        except ProcessLookupError:
            # The group disappeared between the locked liveness observation
            # and delivery. Never claim a signal was sent.
            return False
        except OSError:
            logger.exception(
                "Could not send %s to process group %s",
                sig.name,
                active.process_group_id,
            )
            return False

    def _signal_process_group(self, active: _ActiveRun, sig: signal.Signals) -> bool:
        with active.process_control_lock:
            if self._observe_leader_exit_locked(active):
                return False
            return self._signal_process_group_locked(active, sig)

    def _append_lifecycle_event_best_effort(self, lifecycle: str, run_id: str, **payload: object) -> None:
        """Delegates to `run_finalizer.RunFinalizer` (extracted side)."""
        self._finalizer.append_lifecycle_event_best_effort(lifecycle, run_id, **payload)

    def _auto_commit_completed_work(self, run_id: str, repo_path: Path) -> str | None:
        """Delegates to `run_finalizer.RunFinalizer` (extracted side)."""
        return self._finalizer.auto_commit_completed_work(run_id, repo_path)

    def _persist_run_failure(
        self,
        run_id: str,
        *,
        exit_code: int | None,
        failure_reason: str,
        lifecycle: str,
    ) -> bool:
        """Delegates to `run_finalizer.RunFinalizer` (extracted side)."""
        return self._finalizer.persist_run_failure(
            run_id, exit_code=exit_code, failure_reason=failure_reason, lifecycle=lifecycle
        )

    def _persist_supervision_failure(self, run_id: str, *, exit_code: int | None) -> bool:
        return self._finalizer.persist_supervision_failure(run_id, exit_code=exit_code)

    def _mark_finalized(self, run_id: str) -> bool:
        """Delegates to `run_finalizer.RunFinalizer` (extracted side)."""
        return self._finalizer.mark_finalized(run_id)

    def _owns_open_finalization_claim(self, run_id: str) -> bool:
        claim = db.get_run_finalization_claim(self.db_path, run_id)
        return bool(
            claim
            and claim["owner_token"] == self._finalization_owner_token
            and claim.get("completed_at") is None
        )

    def _acquire_recovery_finalization_claim(self, run_id: str) -> bool:
        """Take ownership only after proving the prior supervisor is gone."""
        claim = db.get_run_finalization_claim(self.db_path, run_id)
        if claim is not None:
            if claim.get("completed_at") is not None:
                return False
            if claim["owner_token"] == self._finalization_owner_token:
                return True
            owner_query = identity.query_identity(int(claim["owner_pid"]))
            if owner_query.status is identity.ProcessQueryStatus.UNKNOWN:
                return False
            if owner_query.status is identity.ProcessQueryStatus.LIVE:
                # A live PID without a readable identity is not proof of reuse.
                # The command portion is mutable (process-title/argv changes,
                # locale/rendering differences), so a command-only mismatch
                # must also fail closed.  Only a different birth timestamp can
                # prove that this live PID is a later process.
                if owner_query.identity is None:
                    return False
                owner_match = identity.compare_recorded_identity(
                    owner_query.identity, claim["owner_identity"]
                )
                # True means the same birth identity. None means a legacy or
                # unknown scheme: during a rolling upgrade a timezone/format
                # change is not proof of PID reuse, so remain fail-closed.
                if owner_match is not False:
                    return False
            expected = claim["owner_token"]
        else:
            # Pre-v25 rows have no fenced owner.  Their safety cannot be
            # inferred while an old supervisor may still be finalizing them.
            # Deployment must prove a zero-unfinalized drain before upgrading;
            # absent claims remain fail-closed instead of being time-stolen.
            return False
        acquired = db.claim_run_finalization(
            self.db_path,
            run_id,
            owner_token=self._finalization_owner_token,
            owner_pid=self._finalization_owner_pid,
            owner_identity=self._finalization_owner_identity,
            expected_owner_token=expected,
        )
        return bool(
            acquired
            and acquired["owner_token"] == self._finalization_owner_token
        )

    def _ensure_terminal_report(self, run: dict) -> None:
        """Create or verify the immutable report before finalized_at.

        Recovery may resume after the file rename but before the report-row
        insert. Re-rendering that deterministic path is safe until the row is
        registered; once registered, both the row and referenced file must be
        present and are never rewritten.
        """
        existing = db.get_report(self.db_path, run["id"])
        if existing is not None:
            resolved = reports.resolve_report_path(existing.get("path"))
            if resolved is None or not resolved.is_file():
                raise SupervisorError(
                    f"Run {run['id']!r} has a report row without a valid durable file."
                )
            return
        events = db.list_run_events(
            self.db_path, run["id"], after_seq=0, limit=1_000_000
        )
        path = reports.save_report(run, events)
        try:
            db.create_report(
                self.db_path, run["id"], reports.stored_report_path(path)
            )
        except Exception:
            # A retry can observe an insert completed by an earlier attempt
            # whose caller failed immediately after commit. Accept only the
            # exact durable row/file, never the exception by itself.
            existing = db.get_report(self.db_path, run["id"])
            if existing is None:
                raise
        stored = db.get_report(self.db_path, run["id"])
        resolved = reports.resolve_report_path(stored.get("path") if stored else None)
        if stored is None or resolved is None or not resolved.is_file():
            raise SupervisorError(f"Run {run['id']!r} report was not durably persisted.")

    def _ensure_lifecycle_event(
        self, run_id: str, lifecycle: str, **payload: object
    ) -> None:
        if self._has_lifecycle_event(run_id, lifecycle):
            return
        self._append_lifecycle_event_best_effort(lifecycle, run_id, **payload)
        if not self._has_lifecycle_event(run_id, lifecycle):
            raise SupervisorError(
                f"Run {run_id!r} lifecycle event {lifecycle!r} was not persisted."
            )

    def _complete_owned_terminal_finalization(
        self,
        run_id: str,
        *,
        exit_code: int | None,
        lifecycle: str,
    ) -> bool:
        """Idempotently finish every durability write under the exact claim."""
        with _FINALIZATION_LOCKS_GUARD:
            finalization_lock = _FINALIZATION_LOCKS.setdefault(run_id, threading.Lock())
        with finalization_lock:
            current = db.get_run(self.db_path, run_id)
            if current is None:
                return True
            if current.get("finalized_at"):
                return True
            if current["state"] not in db.TERMINAL_STATES:
                return False
            if not self._owns_open_finalization_claim(run_id):
                return False

            self._finalizer.finish_started_attempt(
                current,
                fallback_failure_reason=current.get("failure_reason") or "supervision_failed",
            )
            self._ensure_lifecycle_event(
                run_id, lifecycle, exit_code=exit_code, state=current["state"]
            )
            if current["state"] == "COMPLETED" and not any(
                self._has_lifecycle_event(run_id, marker)
                for marker in (
                    "auto_committed",
                    "auto_commit_skipped_clean_tree",
                    "auto_commit_failed",
                )
            ):
                self._auto_commit_completed_work(
                    run_id, Path(current["repository_path"])
                )
            current = db.get_run(self.db_path, run_id)
            if current is None:
                return True
            try:
                self._ensure_terminal_report(current)
            except Exception as exc:
                self._append_lifecycle_event_best_effort(
                    "report_persistence_failed", run_id, error=str(exc)
                )
                raise
            finalized = self._mark_finalized(run_id)
            return finalized

    def _release_active(self, run_id: str, active: _ActiveRun) -> None:
        active.terminal_persisted_event.set()
        with self._active_lock:
            if self._active.get(run_id) is active:
                self._active.pop(run_id, None)
        active.done_event.set()
        with _PROCESS_OWNED_RUNS_GUARD:
            _PROCESS_OWNED_RUNS.discard(run_id)

    def _retry_failed_finalization(self, run_id: str, active: _ActiveRun) -> bool:
        with active.finalization_retry_lock:
            if active.done_event.is_set():
                return True
            if not (
                active.process_exited_event.is_set()
                and active.finalization_failed_event.is_set()
            ):
                return False
            current = db.get_run(self.db_path, run_id)
            if current is None:
                self._release_active(run_id, active)
                return True
            if current["state"] not in db.TERMINAL_STATES and not self._persist_supervision_failure(
                run_id, exit_code=active.process.returncode
            ):
                return False
            if not self._complete_owned_terminal_finalization(
                run_id,
                exit_code=active.process.returncode,
                lifecycle="supervision_failed",
            ):
                return False
            self._release_active(run_id, active)
            return True

    def _abort_launch_after_popen(
        self,
        run_id: str,
        active: _ActiveRun,
        error: Exception,
    ) -> None:
        """Fail closed for every exception after Popen but before supervision."""
        exited = self._terminate_active_process(
            run_id,
            active,
            grace_seconds=DEFAULT_CANCEL_GRACE_SECONDS,
            lifecycle_prefix="launch_setup_failure",
        )
        self._append_lifecycle_event_best_effort(
            "launch_failed",
            run_id,
            error=str(error),
            pid=active.process.pid,
        )
        if not exited:
            active.launch_cleanup_failed_event.set()
            self._append_lifecycle_event_best_effort(
                "launch_cleanup_failed",
                run_id,
                pid=active.process.pid,
            )
            try:
                recovery = threading.Thread(
                    target=self._recover_failed_launch,
                    args=(run_id, active),
                    name=f"run-launch-recovery-{run_id}",
                    daemon=True,
                )
                recovery.start()
            except Exception:
                # The active record and recovery flag remain visible to
                # reconcile()/wait_for_run() even if a recovery thread cannot
                # be created.
                logger.exception(
                    "Could not start failed-launch recovery for run %s",
                    run_id,
                )
            raise SupervisorError(
                f"Launch setup failed for run {run_id!r}, and its child process "
                "could not be confirmed terminated; ownership is retained and "
                "recovery remains active."
            ) from error

        terminal_persisted = self._persist_run_failure(
            run_id,
            exit_code=active.process.returncode,
            failure_reason="launch_setup_failed",
            lifecycle="launch_setup_failed",
        )
        if terminal_persisted:
            try:
                terminal_persisted = self._complete_owned_terminal_finalization(
                    run_id,
                    exit_code=active.process.returncode,
                    lifecycle="launch_setup_failed",
                )
            except Exception:
                logger.exception("Could not finalize failed launch for run %s", run_id)
                terminal_persisted = False
        if not terminal_persisted:
            active.finalization_failed_event.set()
            return
        self._release_active(run_id, active)

    def _retry_failed_launch_cleanup(self, run_id: str, active: _ActiveRun) -> bool:
        with active.launch_cleanup_retry_lock:
            if active.done_event.is_set():
                return True
            exited = self._terminate_active_process(
                run_id,
                active,
                grace_seconds=DEFAULT_CANCEL_GRACE_SECONDS,
                lifecycle_prefix="launch_recovery",
            )
            if not exited:
                return False
            active.launch_cleanup_failed_event.clear()
            terminal_persisted = self._persist_run_failure(
                run_id,
                exit_code=active.process.returncode,
                failure_reason="launch_setup_failed",
                lifecycle="launch_setup_failed",
            )
            if terminal_persisted:
                try:
                    terminal_persisted = self._complete_owned_terminal_finalization(
                        run_id,
                        exit_code=active.process.returncode,
                        lifecycle="launch_setup_failed",
                    )
                except Exception:
                    logger.exception(
                        "Could not finalize recovered failed launch for run %s", run_id
                    )
                    terminal_persisted = False
            if terminal_persisted:
                self._release_active(run_id, active)
                return True
            active.finalization_failed_event.set()
            return False

    def _recover_failed_launch(self, run_id: str, active: _ActiveRun) -> None:
        while not active.done_event.is_set():
            try:
                if self._retry_failed_launch_cleanup(run_id, active):
                    return
            except Exception:
                logger.exception("Failed-launch recovery attempt failed for run %s", run_id)
            active.done_event.wait(timeout=1.0)

    def _terminate_active_process(
        self,
        run_id: str,
        active: _ActiveRun,
        *,
        grace_seconds: float,
        lifecycle_prefix: str,
        before_signal=None,
        on_first_signal_sent=None,
        on_no_signal=None,
    ) -> bool:
        """Serialize TERM/grace/KILL and return only confirmed OS-exit status."""
        pid = active.process.pid
        with active.termination_lock:
            first_signal_recorded = False

            def record_first_signal() -> None:
                nonlocal first_signal_recorded
                if first_signal_recorded:
                    return
                first_signal_recorded = True
                if on_first_signal_sent is not None:
                    on_first_signal_sent()

            with active.process_control_lock:
                if self._observe_leader_exit_locked(active):
                    try:
                        self._wait_for_process_exit(
                            run_id,
                            active,
                            timeout=0,
                            lifecycle_prefix="process_group_cleanup",
                        )
                        return True
                    except (subprocess.TimeoutExpired, SupervisorError):
                        return False

                if before_signal is not None:
                    before_signal()

                # The database claim above may take long enough for a natural
                # exit. WNOWAIT detects that physical exit before any signal;
                # undo the claim while the same lock still blocks terminal
                # finalization.
                if self._observe_leader_exit_locked(active):
                    if on_no_signal is not None:
                        on_no_signal()
                    try:
                        self._wait_for_process_exit(
                            run_id,
                            active,
                            timeout=0,
                            lifecycle_prefix="process_group_cleanup",
                        )
                        return True
                    except (subprocess.TimeoutExpired, SupervisorError):
                        return False

                grace_deadline = time.monotonic() + max(grace_seconds, 0.0)
                term_sent = self._signal_process_group_locked(active, signal.SIGTERM)
                if term_sent:
                    record_first_signal()
                term_lifecycle = (
                    f"{lifecycle_prefix}_sigterm_sent"
                    if term_sent
                    else f"{lifecycle_prefix}_sigterm_failed"
                )
                self._append_lifecycle_event_best_effort(term_lifecycle, run_id, pid=pid)

                if term_sent:
                    try:
                        self._wait_for_process_exit(
                            run_id,
                            active,
                            timeout=max(grace_deadline - time.monotonic(), 0.0),
                            lifecycle_prefix=lifecycle_prefix,
                            group_term_sent=True,
                            grace_deadline=grace_deadline,
                        )
                        return True
                    except subprocess.TimeoutExpired:
                        pass
                    except SupervisorError:
                        self._append_lifecycle_event_best_effort(
                            f"{lifecycle_prefix}_termination_unconfirmed",
                            run_id,
                            pid=pid,
                        )
                        return False

                # If TERM itself failed, do not wait for a natural exit that
                # could be retroactively relabelled. Try KILL immediately.
                kill_sent = self._signal_process_group_locked(active, signal.SIGKILL)
                if kill_sent:
                    record_first_signal()
                kill_lifecycle = (
                    f"{lifecycle_prefix}_sigkill_sent"
                    if kill_sent
                    else f"{lifecycle_prefix}_sigkill_failed"
                )
                self._append_lifecycle_event_best_effort(kill_lifecycle, run_id, pid=pid)

                if not first_signal_recorded and on_no_signal is not None:
                    on_no_signal()

                try:
                    self._wait_for_process_exit(
                        run_id,
                        active,
                        timeout=grace_seconds,
                        lifecycle_prefix=lifecycle_prefix,
                        group_term_sent=term_sent or kill_sent,
                        grace_deadline=time.monotonic(),
                    )
                    return True
                except (subprocess.TimeoutExpired, SupervisorError):
                    pass

                self._append_lifecycle_event_best_effort(
                    f"{lifecycle_prefix}_termination_unconfirmed",
                    run_id,
                    pid=pid,
                )
                return False

    @staticmethod
    def _terminate_owned_spawn(process: subprocess.Popen, *, grace_seconds: float = 0.5) -> None:
        """Boundedly clean up a just-spawned child before it becomes RUNNING.

        The unreaped Popen handle proves ownership here, so its PID cannot
        have been reused. This helper is used only during launch failure,
        before the process is exposed as an active run.
        """
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            process.wait(timeout=grace_seconds)
            return
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                pass

    def _write_stdin(self, run_id: str, active: _ActiveRun, prompt: str) -> None:
        self._streams.write_stdin(run_id, active, prompt)

    # ------------------------------------------------------------------
    # Streaming consumption (runs in background reader threads)
    # ------------------------------------------------------------------

    def _record_handshake(self, run_id: str, active: _ActiveRun) -> None:
        """Delegates to `stream_reader.StreamReader` (extracted side)."""
        self._streams.record_handshake(run_id, active)

    def _drain_stdout(self, run_id: str, active: _ActiveRun) -> None:
        self._streams.drain_stdout(run_id, active)

    def _persist_stdout_event(self, run_id: str, active: _ActiveRun, line: str) -> None:
        self._streams.persist_stdout_event(run_id, active, line)

    def _drain_stderr(self, run_id: str, active: _ActiveRun) -> None:
        self._streams.drain_stderr(run_id, active)

    def _persist_stderr_event(self, run_id: str, active: _ActiveRun, line: str) -> None:
        self._streams.persist_stderr_event(run_id, active, line)

    def _append_stream_event(self, run_id: str, event_type: str, payload: dict) -> None:
        """Delegates to `stream_reader.StreamReader` (extracted side)."""
        self._streams.append_stream_event(run_id, event_type, payload)

    def _final_result_payload(self, run_id: str) -> dict | None:
        """Delegates to `run_finalizer.RunFinalizer` (extracted side)."""
        return self._finalizer.final_result_payload(run_id)

    def _supervise(
        self,
        run_id: str,
        active: _ActiveRun,
        repo_path: Path,
        timeout_seconds: float | None,
        stdin_text: str | None = None,
    ) -> None:
        terminal_persisted = False
        reader_threads: list[threading.Thread] = []
        try:
            # Providers that take their prompt on stdin (Codex, Ollama) block on
            # the first read until it arrives, so the writer must start with the
            # readers — not after them, and never on this thread, whose next
            # call blocks until the process exits.
            if stdin_text is not None:
                self._start_daemon_thread(
                    target=self._write_stdin,
                    args=(run_id, active, stdin_text),
                    name=f"run-stdin-{run_id}",
                )

            if timeout_seconds is not None:
                self._start_daemon_thread(
                    target=self._timeout_watchdog,
                    args=(run_id, active, timeout_seconds),
                    name=f"run-timeout-{run_id}",
                )

            reader_threads.append(
                self._start_daemon_thread(
                    target=self._drain_stdout,
                    args=(run_id, active),
                    name=f"run-stdout-{run_id}",
                )
            )
            reader_threads.append(
                self._start_daemon_thread(
                    target=self._drain_stderr,
                    args=(run_id, active),
                    name=f"run-stderr-{run_id}",
                )
            )

            exit_code = self._wait_for_process_exit(
                run_id,
                active,
                timeout=None,
                lifecycle_prefix="process_group_cleanup",
            )
            for thread in reader_threads:
                thread.join(timeout=DEFAULT_CANCEL_GRACE_SECONDS)
                if thread.is_alive():
                    raise SupervisorError(
                        f"Output reader {thread.name!r} did not finish after "
                        "the owned process group terminated."
                    )

            run = db.get_run(self.db_path, run_id)
            if run is None:
                raise KeyError(f"No such run: {run_id!r}")

            post_snapshot = agent_runner.git_snapshot(repo_path)
            post_status = post_snapshot.get("status_summary")
            post_head = post_snapshot.get("head")
            pre_status = run.get("pre_run_git_status")
            pre_head = run.get("pre_run_head")
            working_tree_changed = pre_status is not None and post_status != pre_status
            # A committed change leaves the working tree clean, so a clean pre/
            # post diff does NOT mean "nothing happened" — the agent may have
            # committed. If HEAD advanced during the run, count that as work
            # produced so the run is not mis-classified
            # `incomplete:working_tree_unchanged` (AICC-DESKTOP-017: copilot_cli
            # and claude_code routinely commit their work).
            if pre_head and post_head and pre_head != post_head:
                working_tree_changed = True
            result_payload = self._final_result_payload(run_id) if exit_code == 0 else None

            for _ in range(_TERMINAL_CAS_MAX_ATTEMPTS):
                cancel_requested = bool(run.get("cancel_requested"))
                timed_out = active.timeout_triggered.is_set()

                # An explicit human cancellation takes precedence in
                # classification over a timeout that may have fired
                # concurrently with it.
                failure_reason = None
                if cancel_requested:
                    new_state = "CANCELLED"
                elif timed_out:
                    new_state = "FAILED"
                    failure_reason = "timeout"
                elif exit_code != 0:
                    new_state = "FAILED"
                    # Provider-authored evidence (bounded and sanitized by the
                    # reader threads) turns a bare nonzero exit into an
                    # actionable reason: quota, authentication, an unreachable
                    # local daemon. `None` keeps the historical bare-FAILED
                    # behavior for providers that classify nothing.
                    failure_reason = active.provider.classify_failure(
                        exit_code=exit_code, diagnostic_lines=active.diagnostic_lines()
                    )
                elif (
                    active.provider_runtime.requires_valid_result
                    and not active.handshake_recorded.is_set()
                ):
                    # A provider that promises a structured stream exited zero
                    # without ever handshaking: the work cannot be assumed done.
                    new_state = "FAILED"
                    failure_reason = "incomplete:provider_handshake_missing"
                elif (
                    active.provider_runtime.requires_valid_result
                    and not active.valid_result_recorded.is_set()
                ):
                    new_state = "FAILED"
                    failure_reason = "incomplete:provider_result_missing"
                else:
                    # exit_code == 0 only proves the provider process itself
                    # did not crash. Evaluate the persisted result before
                    # recording COMPLETED.
                    classification, reason = outcome.classify_process_result(
                        task_type=run["task_type"],
                        result_text=(result_payload or {}).get("result"),
                        permission_denials=(result_payload or {}).get("permission_denials"),
                        working_tree_changed=working_tree_changed,
                        provider_completion_valid=(result_payload or {}).get("provider_completion_valid"),
                    )
                    if classification == outcome.OK:
                        new_state = "COMPLETED"
                    else:
                        # Keep blocked and incomplete display-distinct while
                        # both persist through the existing FAILED state.
                        new_state = "FAILED"
                        failure_reason = f"{classification}:{reason}"

                try:
                    run = db.update_run_state(
                        self.db_path,
                        run_id,
                        expected_version=run["version"],
                        new_state=new_state,
                        fields={
                            "exit_code": exit_code,
                            "completed_at": iso_now(),
                            "post_run_git_status": post_status,
                            "working_tree_changed": 1 if working_tree_changed else 0,
                            "failure_reason": failure_reason,
                        },
                    )
                    terminal_persisted = True
                    if new_state == "COMPLETED":
                        attempt_outcome = "succeeded"
                        attempt_classification = provider_route.SUCCESS
                        attempt_error = None
                    elif new_state == "CANCELLED":
                        attempt_outcome = "cancelled"
                        attempt_classification = provider_route.CANCELLED
                        attempt_error = "cancelled"
                    else:
                        attempt_outcome = "failed"
                        attempt_classification = provider_route.classify_failure(
                            failure_reason
                        )
                        attempt_error = failure_reason or "provider_exit_nonzero"
                    db.finish_provider_attempt(
                        self.db_path,
                        run_id=run_id,
                        attempt_number=1,
                        outcome=attempt_outcome,
                        classification=attempt_classification,
                        disposition=(
                            provider_route.SUCCEEDED
                            if new_state == "COMPLETED"
                            else provider_route.TERMINAL
                        ),
                        error_code=attempt_error,
                        completed_at=run["completed_at"],
                    )
                    active.terminal_persisted_event.set()
                    provenance.update_identity(
                        self.db_path,
                        run_id,
                        head_sha=post_head,
                        branch=post_snapshot.get("branch")
                        if post_snapshot.get("branch") != "(detached HEAD)"
                        else None,
                    )
                    break
                except (db.LostUpdateError, db.InvalidTransitionError):
                    current = db.get_run(self.db_path, run_id)
                    if current is None:
                        raise KeyError(f"No such run: {run_id!r}") from None
                    if current["state"] in db.TERMINAL_STATES:
                        # Another owner already persisted a terminal decision.
                        # Respect it and avoid a duplicate process-exited event
                        # or report; this instance still releases ownership in
                        # the outer finally block.
                        terminal_persisted = self._complete_owned_terminal_finalization(
                            run_id,
                            exit_code=exit_code,
                            lifecycle="process_exited",
                        )
                        if not terminal_persisted:
                            raise SupervisorError(
                                f"Run {run_id!r} terminal decision was not durably finalized."
                            )
                        active.terminal_persisted_event.set()
                        return
                    if current["state"] != "RUNNING":
                        raise
                    run = current
            else:
                raise SupervisorError(
                    f"Run {run_id!r} could not persist its terminal state after "
                    f"{_TERMINAL_CAS_MAX_ATTEMPTS} attempts against concurrent writes."
                )

            self._append_lifecycle_event_best_effort(
                "process_exited",
                run_id,
                exit_code=exit_code,
                state=new_state,
                working_tree_changed=working_tree_changed,
                failure_reason=failure_reason,
            )
            if new_state == "CANCELLED" and working_tree_changed:
                self._append_lifecycle_event_best_effort(
                    "cancellation_working_tree_changed_requires_inspection",
                    run_id,
                    pre_run_git_status=pre_status,
                    post_run_git_status=post_status,
                )

            # The last write of the run, and the reason `finalized_at` exists.
            # Everything above — the `process_exited` event, the auto-commit of
            # the agent's work, the report — happens *after* the terminal state
            # is already visible to every reader, on a daemon thread that
            # interpreter shutdown does not join. For the width of that window
            # — measured over 20 runs at a 6.1 ms median with a clean tree and a
            # 139 ms median (152 ms max) once the auto-commit has a real `git
            # commit` to make — the run looks finished and is not, and a death
            # inside it used to be indistinguishable from a clean completion.
            # Note which way round that is: the window is twenty times wider on
            # exactly the runs that produced work worth losing.
            #
            # Placing the marker here, rather than in the `fields` of the
            # `update_run_state` call above, is the entire point: it makes
            # "finalized" a consequence of the durable writes instead of a
            # promise made before them. Move it up and the column still exists
            # while the guarantee is gone.
            if not self._complete_owned_terminal_finalization(
                run_id, exit_code=exit_code, lifecycle="process_exited"
            ):
                raise SupervisorError(f"Run {run_id!r} finalization marker was not persisted.")
        except Exception:
            logger.exception("Unexpected supervision failure for run %s", run_id)
            exited = active.process_exited_event.is_set()
            if not exited:
                exited = self._terminate_active_process(
                    run_id,
                    active,
                    grace_seconds=DEFAULT_CANCEL_GRACE_SECONDS,
                    lifecycle_prefix="supervision_failure",
                )
            for thread in reader_threads:
                thread.join(timeout=DEFAULT_CANCEL_GRACE_SECONDS)
            if exited:
                current = db.get_run(self.db_path, run_id)
                terminal_persisted = bool(
                    current and current["state"] in db.TERMINAL_STATES
                )
                if not terminal_persisted:
                    terminal_persisted = self._persist_supervision_failure(
                        run_id,
                        exit_code=active.process.returncode,
                    )
                if terminal_persisted:
                    try:
                        terminal_persisted = self._complete_owned_terminal_finalization(
                            run_id,
                            exit_code=active.process.returncode,
                            lifecycle="supervision_failed",
                        )
                    except Exception:
                        logger.exception(
                            "Could not recover durable finalization for run %s", run_id
                        )
                        terminal_persisted = False
                if terminal_persisted:
                    active.terminal_persisted_event.set()
                else:
                    active.finalization_failed_event.set()
            else:
                # Never free the workspace lock while the child might still
                # be alive. Ownership remains in `_active`; a later exit can
                # still be handled by explicit operator/restart recovery.
                self._append_lifecycle_event_best_effort(
                    "supervision_termination_unconfirmed",
                    run_id,
                    pid=active.process.pid,
                )
        finally:
            try:
                if terminal_persisted:
                    self._release_active(run_id, active)
            finally:
                active.supervision_finished_event.set()

    # ------------------------------------------------------------------
    # Cancellation — requires explicit confirmation from the caller/UI
    # ------------------------------------------------------------------

    def cancel(
        self, run_id: str, *, confirmed: bool, grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS
    ) -> dict:
        """SIGTERM to the run's process group, then SIGKILL only after
        `grace_seconds` if it hasn't exited. Never runs `git restore/reset/
        clean` — the working tree is left exactly as the process left it,
        and its post-cancellation status is captured and compared to the
        pre-run snapshot (see `_supervise`'s
        `cancellation_working_tree_changed_requires_inspection` event).
        """
        self._assert_current_process()
        context_service.require_launch_confirmation(confirmed, what="Cancelling a run")

        with self._active_lock:
            active = self._active.get(run_id)
        if active is None:
            raise SupervisorError(
                f"Run {run_id!r} is not an actively supervised run in this process instance "
                "(already finished, or supervised by a different process instance)."
            )

        cancel_recorded = False

        def record_cancel_before_signal() -> None:
            nonlocal cancel_recorded
            run = db.get_run(self.db_path, run_id)
            if run is None:
                raise KeyError(f"No such run: {run_id!r}")
            if run["state"] != "RUNNING":
                raise SupervisorError(
                    f"Run {run_id!r} is not RUNNING (state={run['state']!r}); nothing to cancel."
                )

            # The process-control lock held by `_terminate_active_process`
            # makes the liveness observation, this CAS, and the first signal
            # one linearized operation relative to exit/reap.
            for _ in range(_CANCEL_CAS_MAX_ATTEMPTS):
                try:
                    db.update_run_fields(
                        self.db_path,
                        run_id,
                        expected_version=run["version"],
                        fields={"cancel_requested": 1, "cancel_requested_at": iso_now()},
                    )
                    cancel_recorded = True
                    return
                except db.LostUpdateError:
                    current = db.get_run(self.db_path, run_id)
                    if current is None:
                        raise KeyError(f"No such run: {run_id!r}") from None
                    if current["state"] != "RUNNING":
                        raise SupervisorError(
                            f"Run {run_id!r} changed state before cancellation could be recorded "
                            f"(now state={current['state']!r})."
                        ) from None
                    run = current
            raise SupervisorError(
                f"Run {run_id!r} could not be marked for cancellation after "
                f"{_CANCEL_CAS_MAX_ATTEMPTS} attempts against concurrent writes."
            )

        def rollback_unsignalled_cancel() -> None:
            nonlocal cancel_recorded
            if not cancel_recorded:
                return
            for _ in range(_CANCEL_CAS_MAX_ATTEMPTS):
                current = db.get_run(self.db_path, run_id)
                if current is None or current["state"] != "RUNNING":
                    cancel_recorded = False
                    return
                if not current.get("cancel_requested"):
                    cancel_recorded = False
                    return
                try:
                    db.update_run_fields(
                        self.db_path,
                        run_id,
                        expected_version=current["version"],
                        fields={"cancel_requested": 0, "cancel_requested_at": None},
                    )
                    cancel_recorded = False
                    return
                except db.LostUpdateError:
                    continue
            raise SupervisorError(
                f"Run {run_id!r} exited before cancellation could be signalled, "
                "and its provisional cancellation claim could not be rolled back."
            )

        def record_cancel_signal() -> None:
            self._append_lifecycle_event_best_effort("cancel_requested", run_id)

        exited = self._terminate_active_process(
            run_id,
            active,
            grace_seconds=grace_seconds,
            lifecycle_prefix="cancel",
            before_signal=record_cancel_before_signal,
            on_first_signal_sent=record_cancel_signal,
            on_no_signal=rollback_unsignalled_cancel,
        )
        if not cancel_recorded:
            raise SupervisorError(
                f"Run {run_id!r} has already exited and is finalizing; cancellation was not recorded."
            )
        if not exited:
            raise SupervisorError(
                f"Run {run_id!r} did not exit after cancellation escalation; ownership is retained."
            )
        # `grace_seconds` controls only TERM -> KILL escalation. Once process
        # exit is confirmed, returning while the sole supervision writer is
        # still active would race report/database teardown.
        active.supervision_finished_event.wait()
        if active.finalization_failed_event.is_set() and not self._retry_failed_finalization(
            run_id, active
        ):
            raise SupervisorError(
                f"Run {run_id!r} exited after cancellation, but durable finalization failed."
            )
        if not active.done_event.is_set():
            raise SupervisorError(
                f"Run {run_id!r} exited after cancellation without releasing local ownership."
            )
        final = db.get_run(self.db_path, run_id)
        if final is None or final["state"] not in db.TERMINAL_STATES or not final.get(
            "finalized_at"
        ):
            raise SupervisorError(
                f"Run {run_id!r} cancellation lacks a durable finalized terminal row."
            )
        return final

    def _orphan_past_deadline(self, run: dict) -> bool:
        """Whether an adopted orphan (a RUNNING row from a previous incarnation of
        this app whose process is still alive) has run past
        `started_at + timeout_seconds`. Conservative: if either field is missing
        or unparseable, returns False, so an orphan with no recorded deadline is
        never force-reaped — it stays adopted RUNNING, the prior behaviour."""
        timeout_seconds = run.get("timeout_seconds")
        started_at = run.get("started_at")
        if not timeout_seconds or not started_at:
            return False
        try:
            started = datetime.fromisoformat(started_at)
        except (TypeError, ValueError):
            return False
        return datetime.now() >= started + timedelta(seconds=float(timeout_seconds))

    def _sigkill_orphan_group(self, run_id: str, pid: int | None, recorded_identity: str | None) -> bool:
        """Terminalize only an orphan whose process is already confirmed gone.

        A persisted PID plus birth token can classify reuse, but cannot close
        the check-to-signal race or prove ownership of every descendant in a
        process group. Cross-restart destructive cleanup therefore requires a
        future pidfd+cgroup/job-object ownership handle; this conservative path
        never signals an adopted orphan from database metadata alone.
        """
        if pid is None:
            return True  # nothing to signal; safe to terminalize
        process_query = identity.query_identity(pid)
        if process_query.status in {
            identity.ProcessQueryStatus.ABSENT,
            identity.ProcessQueryStatus.ZOMBIE,
        }:
            return True  # already gone; terminalize
        if process_query.status is identity.ProcessQueryStatus.UNKNOWN:
            return False  # unreadable is not safe to signal or terminalize
        return False

    def _timeout_watchdog(
        self,
        run_id: str,
        active: _ActiveRun,
        timeout_seconds: float,
        grace_seconds: float = DEFAULT_CANCEL_GRACE_SECONDS,
    ) -> None:
        """Waits (using a monotonic-backed `threading.Event`, not wall-clock
        polling) up to `timeout_seconds` for the run to finish on its own. If
        it hasn't, terminates it via the identical process-group SIGTERM ->
        grace -> SIGKILL sequence `cancel()` uses, and marks
        `active.timeout_triggered` so `_supervise` records the terminal state
        as `FAILED` with `failure_reason="timeout"` rather than `CANCELLED`
        (which is reserved for an explicit, human-confirmed cancellation).
        Never runs `git restore/reset/clean` — same guarantee as `cancel()`.
        """
        exited_before_deadline = active.leader_exited_event.wait(timeout=max(timeout_seconds, 0.0))
        if exited_before_deadline:
            return  # finished on its own before the deadline; nothing to do

        timeout_claimed = False

        def claim_timeout_before_signal() -> None:
            nonlocal timeout_claimed
            active.timeout_triggered.set()
            timeout_claimed = True
            self._append_lifecycle_event_best_effort(
                "timeout_exceeded",
                run_id,
                timeout_seconds=timeout_seconds,
            )

        exited = self._terminate_active_process(
            run_id,
            active,
            grace_seconds=grace_seconds,
            lifecycle_prefix="timeout",
            on_first_signal_sent=claim_timeout_before_signal,
        )
        if timeout_claimed and not exited:
            logger.error(
                "Timed-out run %s did not exit after signal escalation; ownership retained",
                run_id,
            )

    # ------------------------------------------------------------------
    # Startup reconciliation
    # ------------------------------------------------------------------

    def _persist_reconciliation_state(
        self,
        run_id: str,
        *,
        classification: str,
        pid: int | None = None,
        detail: str = "reconciled orphan",
    ) -> dict | None:
        """Persist one reconciliation decision with bounded CAS retries."""
        for _ in range(_TERMINAL_CAS_MAX_ATTEMPTS):
            current = db.get_run(self.db_path, run_id)
            if current is None:
                return current
            if current["state"] in db.TERMINAL_STATES:
                # Never infer that another owner's post-terminal writes are
                # done. A terminal-but-unfinalized row remains visibly pending
                # until its owner completes or a later recovery proves that
                # owner dead and takes the explicit claim.
                if current.get("finalized_at"):
                    return current
                if self._owns_open_finalization_claim(run_id):
                    if self._complete_owned_terminal_finalization(
                        run_id,
                        exit_code=current.get("exit_code"),
                        lifecycle="reconciliation_classified",
                    ):
                        return db.get_run(self.db_path, run_id)
                    continue
                return current
            if current["state"] not in db.EXECUTION_CENTER_ACTIVE_STATES:
                return current
            if classification == "RUNNING" and current["state"] == "RUNNING":
                return current
            if (
                classification in db.TERMINAL_STATES
                and not self._acquire_recovery_finalization_claim(run_id)
            ):
                return current
            try:
                reconciled = db.update_run_state(
                    self.db_path,
                    run_id,
                    expected_version=current["version"],
                    new_state=classification,
                    fields=None if classification == "RUNNING" else {"completed_at": iso_now()},
                )
            except (db.LostUpdateError, db.InvalidTransitionError):
                continue
            if classification in db.TERMINAL_STATES:
                if not self._has_lifecycle_event(run_id, "reconciliation_classified"):
                    self._append_lifecycle_event_best_effort(
                        "reconciliation_classified",
                        run_id,
                        pid=pid,
                        classification=classification,
                        detail=detail,
                    )
                if not self._complete_owned_terminal_finalization(
                    run_id,
                    exit_code=reconciled.get("exit_code"),
                    lifecycle="reconciliation_classified",
                ):
                    continue
                return db.get_run(self.db_path, run_id)
            return reconciled
        raise SupervisorError(
            f"Run {run_id!r} could not persist reconciliation state "
            f"{classification!r} after {_TERMINAL_CAS_MAX_ATTEMPTS} attempts."
        )

    def _has_lifecycle_event(self, run_id: str, lifecycle: str) -> bool:
        try:
            events = db.list_run_events(
                self.db_path,
                run_id,
                after_seq=0,
                limit=1_000_000,
                event_type="lifecycle",
            )
        except Exception:
            return False
        return any(event["payload"].get("lifecycle") == lifecycle for event in events)

    def reconcile(self) -> list[dict]:
        """Inspect every run currently recorded in an active state
        (`db.EXECUTION_CENTER_ACTIVE_STATES` — `PREPARED`, `QUEUED`, or
        `RUNNING`) and classify it conservatively. Never signals a process
        based only on a reused pid, and never guesses that a run silently
        completed. Does not consult `claude agents --json` — this SQLite
        `run` table is the Supervisor's own lifecycle registry, entirely
        independent of the `claude` CLI's background-agent registry (which
        p-mode runs never touch anyway, since `--background`/`--bg` is
        prohibited everywhere in this module).

        `PREPARED`/`QUEUED` rows are included, not just `RUNNING`, because a
        Supervisor process can crash between `start_raw` creating the row
        and `_launch_process` actually `Popen`-ing it — without this, such a
        row would sit "active" forever (never reachable again once its
        Supervisor is gone), permanently occupying its workspace's lock (see
        `db.create_run`'s `enforce_workspace_lock`). In practice a `PREPARED`
        row never has a `pid` (nothing has attempted `Popen` yet at that
        point), so it always resolves to `INTERRUPTED` below; a `QUEUED` row
        can rarely carry a `pid` (the narrow window between recording it and
        persisting the `RUNNING` transition), in which case it goes through
        the exact same pid/identity classification as a `RUNNING` row.

        Skips any live/finalizing run currently in `self._active`, plus every
        run in `self._launching`. The one intentional exception is a locally
        owned child whose OS exit is confirmed and whose terminal persistence
        already exhausted its bounded retries: reconciliation retries that
        durable write and releases ownership once it succeeds.

        In all other cases, a run *this* instance is actively supervising, or
        has committed to launching but not yet `Popen`'d, is never a
        candidate for reconciliation:

        - `self._active`: the opposite of orphaned — its own `_supervise`
          background thread already holds the real waitable-child handle and
          will write the authoritative terminal state itself the moment the
          process exits. Without this guard, calling `reconcile()`
          repeatedly during normal operation (the Live Execution Center v2
          dashboard's refresh tick calls it on every tick, not just at
          startup — see `task_sync.reconcile_and_sync`) would race a
          fast-exiting process: if the OS process happens to exit before
          this instance's own `_supervise` thread gets to `process.wait()`
          and persist the result, `identity.capture_identity(pid)` here
          would see "pid gone" and misclassify a run that is completing
          completely normally as `INTERRUPTED`.
        - `self._launching`: now that `PREPARED`/`QUEUED` rows are in scope
          above, a run this same instance is mid-`start_raw` for (row
          persisted as `PREPARED` or `QUEUED`, no pid yet — `_launch_process`
          hasn't called `Popen`) would otherwise look identical to a
          genuinely abandoned `PREPARED`/`QUEUED` row from a crashed
          predecessor and get misclassified `INTERRUPTED` out from under its
          own in-flight launch. `start_raw` adds the run id here immediately
          once `db.create_run` returns (while the row is still `PREPARED`),
          not after the subsequent `QUEUED` transition — registering it any
          later would leave exactly that window unguarded.

        Classification:

        - No pid was ever recorded -> `INTERRUPTED` (we never captured what
          to check; the process's fate is simply unrecorded).
        - pid does not currently exist -> `INTERRUPTED` (provably gone; we do
          not know whether it completed or failed before it disappeared, so
          we do not claim `COMPLETED`/`FAILED`).
        - pid exists but no identity was recorded at launch time to compare
          against -> `UNKNOWN` (nothing here proves or disproves it's ours).
        - pid exists but its current identity does not match what was
          recorded at launch -> `INTERRUPTED` (a reused pid now running a
          different process; the original process is gone).
        - pid exists and its identity matches exactly -> classified/left as
          `RUNNING` (transitioning a matched `QUEUED` row explicitly, since
          we now have positive proof it is actually running), but flagged
          with a `reconciliation_orphaned` event and *not* re-registered as
          an actively supervised run: this Supervisor instance has no
          stdout/stderr pipe or waitable-child handle for a process it did
          not itself `Popen`, so it cannot resume incremental persistence
          and must not attempt to signal/cancel it.
        """
        self._assert_current_process()
        outcomes = []
        # The row query belongs to the same critical section as the in-memory
        # snapshot. `start_raw()` uses this lock around create_run()+registration,
        # so reconciliation can see either no new row or a row already present
        # in one of the ownership sets, never the unguarded state between them.
        with self._active_lock:
            active_snapshot = dict(self._active)
            launching_ids = set(self._launching)
        with _PROCESS_OWNED_RUNS_GUARD:
            process_owned_ids = set(_PROCESS_OWNED_RUNS)

        # Self-heal terminal persistence failures without waiting for a
        # Supervisor restart. Slow normal finalization is not eligible because
        # `finalization_failed_event` is set only after bounded failure.
        for run_id, active in active_snapshot.items():
            try:
                if active.launch_cleanup_failed_event.is_set():
                    if self._retry_failed_launch_cleanup(run_id, active):
                        current = db.get_run(self.db_path, run_id)
                        outcomes.append(
                            {
                                "run_id": run_id,
                                "classification": current["state"] if current else "MISSING",
                                "detail": "recovered failed post-launch process cleanup",
                            }
                        )
                    continue
                if not active.finalization_failed_event.is_set():
                    continue
                if self._retry_failed_finalization(run_id, active):
                    current = db.get_run(self.db_path, run_id)
                    outcomes.append(
                        {
                            "run_id": run_id,
                            "classification": current["state"] if current else "MISSING",
                            "detail": "recovered failed terminal persistence for an exited local process",
                        }
                    )
            except Exception:
                logger.exception("Could not retry failed finalization for run %s", run_id)

        with self._active_lock:
            actively_supervised_ids = set(self._active.keys()) | launching_ids
            # Queried while the lock is held, not after: `start_raw` registers a
            # run under this same lock around `create_run`, so releasing it
            # first would expose the window where a row exists but its
            # ownership registration is not yet visible — exactly the state the
            # comment above says reconciliation must never observe.
            active_rows = db.list_runs(self.db_path, states=db.EXECUTION_CENTER_ACTIVE_STATES)

        # Recover terminal rows only through the durable ownership claim. A
        # live prior supervisor means "still finalizing", not an orphan; an
        # unknown process query also fails closed. If the exact owner identity
        # is gone, one CAS winner reconstructs the report/attempt/commit and
        # only then closes the marker.
        for pending in db.list_unfinalized_runs(self.db_path):
            run_id = pending["id"]
            if run_id in actively_supervised_ids:
                continue
            try:
                if not self._acquire_recovery_finalization_claim(run_id):
                    continue
                if not self._has_lifecycle_event(run_id, "finalization_recovered"):
                    self._append_lifecycle_event_best_effort(
                        "finalization_recovered",
                        run_id,
                        prior_state=pending["state"],
                    )
                if self._complete_owned_terminal_finalization(
                    run_id,
                    exit_code=pending.get("exit_code"),
                    lifecycle="finalization_recovered",
                ):
                    with _PROCESS_OWNED_RUNS_GUARD:
                        _PROCESS_OWNED_RUNS.discard(run_id)
                    outcomes.append(
                        {
                            "run_id": run_id,
                            "classification": pending["state"],
                            "detail": "recovered terminal finalization after owner exit",
                        }
                    )
            except Exception:
                logger.exception("Could not recover terminal finalization for run %s", run_id)
        # Forget "gone" suspicions for any run that is no longer active — it
        # either reached a terminal state (its owner CAS'd it) or is now
        # supervised, so it must not carry a stale suspicion into a later pass.
        active_ids = {run["id"] for run in active_rows}
        with self._suspected_gone_lock:
            for stale in [rid for rid in self._suspected_gone if rid not in active_ids]:
                self._suspected_gone.pop(stale, None)
        for run in active_rows:
            run_id = run["id"]
            if run_id in actively_supervised_ids or run_id in process_owned_ids:
                continue
            try:
                pid = run.get("pid")
                recorded_identity = run.get("process_start_identity")

                looks_gone = False
                if pid is None:
                    looks_gone = True
                    detail = "no pid recorded for this run"
                else:
                    process_query = identity.query_identity(pid)
                    if process_query.status in {
                        identity.ProcessQueryStatus.ABSENT,
                        identity.ProcessQueryStatus.ZOMBIE,
                    }:
                        looks_gone = True
                        detail = "pid no longer executes"
                    elif process_query.status is identity.ProcessQueryStatus.UNKNOWN:
                        classification = "UNKNOWN"
                        detail = "pid identity could not be queried safely"
                    elif process_query.identity is None:
                        classification = "UNKNOWN"
                        detail = "live pid query returned no identity"
                    elif not recorded_identity:
                        classification = "UNKNOWN"
                        detail = "pid exists but no identity was recorded at launch time"
                    elif identity.compare_recorded_identity(
                        process_query.identity, recorded_identity
                    ) is True:
                        classification = "RUNNING"
                        detail = "pid exists and identity matches; orphaned from this supervisor instance"
                    elif identity.compare_recorded_identity(
                        process_query.identity, recorded_identity
                    ) is False:
                        classification = "INTERRUPTED"
                        detail = "pid exists but identity does not match recorded identity (pid reuse)"
                    else:
                        classification = "UNKNOWN"
                        detail = (
                            "live pid has a legacy or unknown identity scheme; "
                            "PID reuse cannot be proved safely"
                        )

                if looks_gone:
                    # Debounce (audit P0): another process may own this row and be
                    # mid-launch (pid still None until Popen) or mid-finalization
                    # (its process exited but it is about to CAS COMPLETED). Only
                    # terminalize once the "gone" observation has persisted across
                    # the grace window; otherwise wait and re-check on a later
                    # reconcile, so a peer's succeeding run is never INTERRUPTED
                    # out from under it (which would lose its work and free its
                    # workspace lock). A row that resolves in the meantime — the
                    # owner CAS'd it terminal — simply leaves the active set and is
                    # pruned above, so it is never falsely interrupted.
                    now = time.monotonic()
                    with self._suspected_gone_lock:
                        first_seen = self._suspected_gone.setdefault(run_id, now)
                    if (now - first_seen) < self._reconcile_absence_grace:
                        continue
                    classification = "INTERRUPTED"

                # Resolved, or now confirmed gone past the grace window: this run
                # no longer needs a pending "gone" suspicion.
                with self._suspected_gone_lock:
                    self._suspected_gone.pop(run_id, None)

                if classification == "RUNNING" and self._orphan_past_deadline(run):
                    # Adopted orphan (identity verified above) that has run past
                    # its timeout. It is NOT re-registered in self._active, so its
                    # timeout watchdog is gone; left alone it would run forever,
                    # holding its workspace lock and a global-concurrency slot with
                    # no way to reap it from the app (audit P0/H2). reconcile runs
                    # periodically, so enforce the timeout here: SIGKILL its
                    # (re-verified) process group and terminalize the row, which
                    # releases the workspace lock and the global slot.
                    if self._sigkill_orphan_group(run_id, pid, recorded_identity):
                        self._append_lifecycle_event_best_effort(
                            "reconciliation_orphan_timeout",
                            run_id,
                            pid=pid,
                            detail="adopted orphan exceeded its timeout; process group killed",
                        )
                        classification = "INTERRUPTED"
                        detail = "orphaned run exceeded its timeout; process group terminated"

                persisted = self._persist_reconciliation_state(
                    run_id,
                    classification=classification,
                    pid=pid,
                    detail=detail,
                )
                if persisted is not None and persisted["state"] in db.TERMINAL_STATES:
                    classification = persisted["state"]

                if classification == "RUNNING":
                    if not self._has_lifecycle_event(run_id, "reconciliation_orphaned"):
                        self._append_lifecycle_event_best_effort(
                            "reconciliation_orphaned",
                            run_id,
                            pid=pid,
                            detail=detail,
                        )
                else:
                    if not self._has_lifecycle_event(run_id, "reconciliation_classified"):
                        self._append_lifecycle_event_best_effort(
                            "reconciliation_classified",
                            run_id,
                            pid=pid,
                            classification=classification,
                            detail=detail,
                        )
                outcomes.append({"run_id": run_id, "classification": classification, "detail": detail})
            except Exception as exc:
                # One damaged/contended row must not prevent recovery of every
                # later run in the same reconciliation pass.
                logger.exception("Could not reconcile run %s", run_id)
                outcomes.append(
                    {
                        "run_id": run_id,
                        "classification": "ERROR",
                        "detail": str(exc),
                    }
                )
        return outcomes

    # ------------------------------------------------------------------
    # Convenience read/test helpers
    # ------------------------------------------------------------------

    def get_run(self, run_id: str) -> dict | None:
        return db.get_run(self.db_path, run_id)

    def active_run_ids(self) -> list[str]:
        self._assert_current_process()
        with self._active_lock:
            return list(self._active.keys())

    def wait_for_run(self, run_id: str, timeout: float | None = None) -> dict:
        """Block until `run_id` leaves this instance's active-run registry
        (i.e. reaches a terminal state) or `timeout` elapses."""
        self._assert_current_process()
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        while True:
            with self._active_lock:
                active = self._active.get(run_id)
            if active is None or active.done_event.is_set():
                break
            if active.launch_cleanup_failed_event.is_set():
                try:
                    if self._retry_failed_launch_cleanup(run_id, active):
                        break
                except Exception:
                    logger.exception(
                        "Could not retry failed launch cleanup while waiting for run %s",
                        run_id,
                    )
            if active.finalization_failed_event.is_set():
                try:
                    if self._retry_failed_finalization(run_id, active):
                        break
                except Exception:
                    logger.exception("Could not retry finalization while waiting for run %s", run_id)
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                active.done_event.wait(timeout=min(0.1, remaining))
            else:
                active.done_event.wait(timeout=0.1)
        return db.get_run(self.db_path, run_id)
