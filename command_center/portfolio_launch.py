"""Dry-run planning and real launch of Portfolio task cards.

This module is the only place a Portfolio card ever turns into a git branch,
a git worktree, a generated agent prompt, an execution-queue entry, or a
running agent process. It deliberately does not reimplement any of those: it
calls straight into the existing `command_center.execution_queue` /
`command_center.launch_service` / `command_center.runtime` stack — the same
queue, the same launcher, the same SQLite workspace lock every other launch
path in this app already goes through (per
`command_center.runtime.db.create_run(..., enforce_workspace_lock=True)`,
reached via `execution_queue.launch_ready` ->
`launch_service.execute_agent_launch_v2` -> `ExecutionCenterAPI.start_run` ->
`Supervisor.start_raw`). The only genuinely new mutations this module
performs are `git worktree add` (and its rollback counterparts), since
nothing else in this codebase creates a worktree or a branch.

Branch/worktree resolution:

- A card's own `branch`/`worktree` fields are honored as an explicit override
  when non-blank (`branch_source`/`worktree_source` == `"card_override"` on
  the resulting `LaunchPlan`) — validated (a real git ref name; an absolute
  path), never silently discarded or silently replaced by a generated
  default. A blank/absent field falls back to the deterministic generated
  default (`task/<task_id lower-cased>` /
  `<worktrees_root>/<task_id lower-cased>`, `branch_source`/`worktree_source`
  == `"generated_default"`).
- If the resolved worktree path already exists on disk, this module *never*
  calls `git worktree add` against it (`worktree_mode == "existing"`):
  instead it validates the existing directory is a real worktree of the
  resolved repository, already on the resolved branch, and clean, then
  launches directly into it. Nothing this module didn't create is ever
  removed, reset, or have its branch deleted — see `_rollback_worktree`.
- If the resolved worktree path does not exist (`worktree_mode == "new"`),
  this module creates it. If the resolved branch already exists in the
  repository *and* came from an explicit card override, that is treated as
  "attach this existing branch to a new worktree" (`git worktree add <path>
  <branch>`, no `-b`, no base ref consumed) rather than a blocker — a
  generated-default branch already existing is still a hard blocker (it
  should never pre-exist; if it does, something wasn't cleaned up).

Safety model:

- `build_launch_plan`/`build_batch_plan` are pure and read-only: they inspect
  the filesystem and run only read-only git subcommands (`rev-parse`,
  `branch --list`, `check-ref-format`, `status --porcelain` via
  `command_center.git_info`), and never write anything — dry-run planning
  must produce zero mutation.
- Because both the generated branch and worktree names are deterministic per
  `task_id`, and this module never deletes a branch/worktree it created, git
  itself is the backstop against re-launching the same task twice under
  generated defaults (`git worktree add` fails outright if the path or
  branch already exists). For an explicit card override, the same backstop
  doesn't automatically hold (two different cards could each ask to reuse
  the same branch/worktree) — see `_find_conflicting_registration` and
  `_detect_batch_conflicts`, which check the resolved branch/worktree
  against every other task's registry entry / the rest of the same batch.
- A small local registry (`data/portfolio_launches.json`, gitignored) records
  every task once successfully launched, purely for fast duplicate rejection
  and UI history — never the sole authority (git's own state is). Three
  independent locks guard it, for three different races:
  - The exclusive-publish lock file in `data/portfolio_locks/<task_id>.lock`
    (a fully-written temporary inode atomically published with `os.link`)
    serializes two concurrent launch
    *attempts for the same task_id* — the same technique this module needs
    for worktree/branch creation because, unlike the SQLite-backed
    workspace lock in `runtime.db`, that has no existing atomic primitive
    in this codebase to reuse. Its owner metadata allows a later launch to
    recover it after a process crash, while a live owner is never displaced.
  - `data/portfolio_locks/workspace-<sha256 of resolved path>.lock`
    (same publish/recovery mechanics as the task_id lock, via
    `_workspace_lock_path`/`_claim_workspace`/`_release_workspace`) serializes
    two concurrent launch attempts that resolve to the *same worktree path*
    under two *different* task_ids — an explicit-override collision the
    task_id lock above cannot see, since each such launch holds its own
    distinct `<task_id>.lock`. Held for the same critical section as the
    task_id claim (conflict re-check through worktree creation), so it closes
    the window before `git worktree add`/`attach_worktree` runs, not just
    after it in the registry (see `launch_portfolio_task`).
  - `_registry_lock` (`data/portfolio_locks/registry.lock`, an OS advisory
    file lock — `fcntl.flock` on POSIX, `msvcrt.locking` on Windows) guards
    the registry file's read-modify-write cycle itself, across *every*
    task_id, in every process. Two concurrent launches of *different*
    task_ids each hold their own `<task_id>.lock` (and, now, their own
    `workspace-*.lock` when they target different paths) and so never block
    each other on those — but both still read-modify-write the same
    `portfolio_launches.json`. Without `_registry_lock`, both could load the
    same pre-write snapshot, each add their own entry, and the second
    `save_registry` call would silently overwrite the first task's entry (a
    lost update): a temp-file + `os.replace` write is atomic *on its own*
    but does nothing to prevent that, since the race is between the two
    reads, not the two writes. `_persist_registry_entry` is the only
    function that writes the registry, and it holds `_registry_lock` for
    its entire read-modify-write cycle.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import socket
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from command_center import execution_queue, git_info, models, storage, worktree_launcher
from command_center.portfolio_models import PortfolioCardError, PortfolioTask, unmet_requirements, validate_task_id
from command_center.runtime import db as runtime_db

WORKTREES_ROOT_ENV = "AICC_PORTFOLIO_WORKTREES_ROOT"
DEFAULT_WORKTREES_ROOT = Path.home() / "Projects" / "worktrees"

REGISTRY_FILE_NAME = "portfolio_launches.json"
LOCKS_DIR_NAME = "portfolio_locks"
CLAIM_LOCK_VERSION = 1
CLAIM_RECOVERY_LOCK_SUFFIX = ".recovery"
CLAIM_RECOVERY_LOCK_TIMEOUT_SECONDS = 5.0
_CLAIM_RECOVERY_LOCK_POLL_SECONDS = 0.02

DEFAULT_MAX_CONCURRENT_LAUNCHES = 3

SOURCE_CARD_OVERRIDE = "card_override"
SOURCE_GENERATED_DEFAULT = "generated_default"

WORKTREE_MODE_EXISTING = "existing"
WORKTREE_MODE_NEW = "new"

# Main-line branches must never be used as the task branch of a Portfolio
# launch. Portfolio launches are isolated implementation/audit runs: allowing
# one to resolve to a repository's primary branch would make safety depend on
# `git worktree add` noticing that the branch is already checked out elsewhere.
# Matched case-insensitively: on a case-insensitive filesystem (the default on
# macOS) a loose ref `Main` collides with `main`, so a case variant is not a
# distinct branch — it is the protected one reached by a different spelling.
PROTECTED_BRANCH_NAMES = frozenset({"main", "master"})

# `build_launch_plan`'s blocker text for "this task_id is already in the
# registry" — a named constant (rather than a string literal repeated at the
# append site and every reader) so `build_card_presentation` can reliably
# split it out from genuine precondition blockers without string-matching.
ALREADY_LAUNCHED_BLOCKER = "задача уже была запущена ранее — см. журнал запусков Portfolio"

# Card `type` values that map to a read-only agent role
# (`agent_runner.READ_ONLY_TASK_TYPES`). Everything else — including
# `implementation`/`design`/`documentation`/`infrastructure`/`audit`, none of
# which are read-only in practice for this Portfolio's own cards — maps to
# `implementation`, which keeps Bash available (needed to run the card's own
# `validation` commands) while still blocking git-write subcommands via
# `agent_runner.GIT_WRITE_DISALLOWED_TOOLS`.
_TASK_TYPE_MAP: dict[str, str] = {
    "review": "review",
    "final_gate": "final_gate",
    "architecture_review": "architecture_review",
}


def _map_task_type(card_type: str | None) -> str:
    return _TASK_TYPE_MAP.get(card_type or "", "implementation")


def worktrees_root() -> Path:
    override = os.environ.get(WORKTREES_ROOT_ENV, "").strip()
    return Path(override) if override else DEFAULT_WORKTREES_ROOT


def _validate_task_id_for_launch(task_id: str) -> str:
    """Defense-in-depth re-check of `task_id`, independent of whatever the
    caller already validated (`command_center.portfolio_models.parse_card`
    validates every card-loaded `task_id` at parse time — this is the second,
    unconditional gate for every function here that turns a `task_id` into a
    filesystem path, so a bypass of the parser — a hand-built `PortfolioTask`,
    a future call site that forgets to go through `parse_card` — can never
    turn into a path-traversal write (Founder Gate re-review Blocker 1).
    Raises `PortfolioLaunchError` (this module's exception, not the parser's
    `PortfolioCardError`) since every caller here is on the mutation/launch
    path, not the card-parsing path."""
    try:
        return validate_task_id(task_id)
    except PortfolioCardError as exc:
        raise PortfolioLaunchError(str(exc)) from exc


def _assert_within_root(candidate: Path, root: Path, *, what: str) -> None:
    """Raises `PortfolioLaunchError` if `candidate` does not resolve to a
    location inside `root` — the last-line defense against a symlinked
    ancestor directory redirecting a syntactically-safe `task_id`-derived
    path outside its intended sandbox. Resolves both sides (symlinks
    included) before comparing; does not require either path to exist yet."""
    resolved_root = root.resolve()
    resolved_candidate = candidate.resolve(strict=False)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise PortfolioLaunchError(
            f"resolved {what} path escapes the configured sandbox root ({resolved_root}): {resolved_candidate}"
        ) from exc


def branch_name_for(task_id: str) -> str:
    task_id = _validate_task_id_for_launch(task_id)
    return f"task/{task_id.lower()}"


def worktree_path_for(task_id: str) -> Path:
    task_id = _validate_task_id_for_launch(task_id)
    root = worktrees_root()
    candidate = root / task_id.lower()
    _assert_within_root(candidate, root, what="worktree")
    return candidate


# --------------------------------------------------------------------------
# Branch / worktree resolution — card override vs. generated default
# --------------------------------------------------------------------------


def _validate_branch_name(name: str) -> str | None:
    """Returns an error message if `name` is not a safe git branch name,
    else `None`. Rejects protected main-line branches explicitly, then
    delegates syntax validation to `git check-ref-format --branch`, which
    needs no repository context and does not touch the filesystem."""
    if name.casefold() in PROTECTED_BRANCH_NAMES:
        protected = ", ".join(sorted(PROTECTED_BRANCH_NAMES))
        return (
            f"защищённую ветку «{name}» нельзя использовать для Portfolio-задачи "
            f"(защищённые ветки: {protected}); укажите отдельную рабочую ветку"
        )
    try:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", name],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"не удалось проверить имя ветки: {exc}"
    if result.returncode != 0:
        return (result.stderr or result.stdout).strip() or "недопустимое имя ветки"
    return None


def resolve_branch(task: PortfolioTask) -> tuple[str | None, str, str | None]:
    """Returns `(resolved_branch, source, blocker)`.

    A `None`/absent card `branch` always resolves to a valid generated
    default (`blocker` is `None`). A present-but-blank (empty or
    whitespace-only) or invalid override yields `resolved_branch=None` and a
    descriptive `blocker` — it is never silently replaced by the generated
    default."""
    raw = task.branch
    if raw is None:
        if not task.task_id:
            return None, SOURCE_GENERATED_DEFAULT, None
        try:
            default = branch_name_for(task.task_id)
        except PortfolioLaunchError as exc:
            # `build_launch_plan`/`build_batch_plan` are pure and must never
            # raise (see their docstring) — a `task_id` invalid enough that
            # `branch_name_for` rejects it (Founder Gate re-review Blocker 1)
            # is surfaced as an ordinary plan blocker instead, same as any
            # other resolution failure here.
            return None, SOURCE_GENERATED_DEFAULT, str(exc)
        return default, SOURCE_GENERATED_DEFAULT, None

    stripped = raw.strip()
    if not stripped:
        return None, SOURCE_CARD_OVERRIDE, "поле branch в карточке пустое или состоит только из пробелов"

    error = _validate_branch_name(stripped)
    if error:
        return None, SOURCE_CARD_OVERRIDE, f"branch в карточке недопустим: {error}"
    return stripped, SOURCE_CARD_OVERRIDE, None


def resolve_worktree(task: PortfolioTask) -> tuple[Path | None, str, str | None]:
    """Returns `(resolved_worktree, source, blocker)` — same contract as
    `resolve_branch`. A relative override path is rejected as ambiguous
    (relative to what?) rather than silently interpreted."""
    raw = task.worktree
    if raw is None:
        if not task.task_id:
            return None, SOURCE_GENERATED_DEFAULT, None
        try:
            default = worktree_path_for(task.task_id)
        except PortfolioLaunchError as exc:
            # Same rationale as the generated-branch case in `resolve_branch`
            # above — never raise out of a pure planning function.
            return None, SOURCE_GENERATED_DEFAULT, str(exc)
        return default, SOURCE_GENERATED_DEFAULT, None

    stripped = raw.strip()
    if not stripped:
        return None, SOURCE_CARD_OVERRIDE, "поле worktree в карточке пустое или состоит только из пробелов"

    candidate = Path(stripped).expanduser()
    if not candidate.is_absolute():
        return None, SOURCE_CARD_OVERRIDE, f"worktree в карточке должен быть абсолютным путём: {stripped}"
    return candidate.resolve(), SOURCE_CARD_OVERRIDE, None


def _git_common_dir(path: Path) -> Path | None:
    """The resolved `--git-common-dir` for `path` — identical for every
    worktree of the same repository, and how this module tells "an existing
    directory that happens to be *some* git repo" apart from "an existing
    worktree of *this specific* mapped repository"."""
    result = git_info.run_git_command(path, ["rev-parse", "--git-common-dir"])
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (path / common)
    try:
        return common.resolve()
    except OSError:
        return None


# --------------------------------------------------------------------------
# Registry (duplicate-launch bookkeeping) — data/portfolio_launches.json
# --------------------------------------------------------------------------


def _registry_file(root: Path) -> Path:
    return storage.resolve_data_dir(root) / REGISTRY_FILE_NAME


def load_registry(root: Path) -> dict[str, dict]:
    data = storage.read_json(_registry_file(root), {})
    return data if isinstance(data, dict) else {}


def save_registry(root: Path, registry: dict[str, dict]) -> None:
    storage.atomic_write_json(_registry_file(root), registry)


def _find_conflicting_registration(
    registry: dict[str, dict],
    *,
    branch: str | None,
    worktree: str | None,
    exclude_task_id: str | None,
) -> str | None:
    """The task_id of another already-registered launch that claims the same
    resolved `branch` or the same resolved `worktree`, if any — checked
    against the *resolved* values so a card-override collision between two
    different task ids is caught, not just a collision between two
    generated-default names (which git's own atomicity already prevents)."""
    for other_task_id, entry in registry.items():
        if other_task_id == exclude_task_id:
            continue
        if branch and entry.get("branch") == branch:
            return other_task_id
        if worktree and entry.get("worktree") == worktree:
            return other_task_id
    return None


def _locks_dir(root: Path) -> Path:
    return storage.resolve_data_dir(root) / LOCKS_DIR_NAME


def _claim_lock_path(root: Path, task_id: str) -> Path:
    """The per-task claim lock path for `task_id`, validated end to end
    before any filesystem call: `task_id` shape first (Founder Gate
    re-review Blocker 1 — the same defense-in-depth gate as
    `branch_name_for`/`worktree_path_for`), then that the resulting path is
    actually contained under the locks directory (belt-and-suspenders
    against a symlinked ancestor, since a valid `task_id` alone can no
    longer contain a path separator)."""
    task_id = _validate_task_id_for_launch(task_id)
    locks_dir = _locks_dir(root)
    candidate = locks_dir / f"{task_id}.lock"
    _assert_within_root(candidate, locks_dir, what="lock")
    return candidate


def _workspace_lock_path(root: Path, worktree_path: Path) -> Path:
    """The path-keyed claim lock for `worktree_path`'s *resolved* location.

    `_claim_lock_path` above only closes a race between two launches of the
    same `task_id` — the resource it actually protects downstream (`git
    worktree add`/`attach_worktree` in `launch_portfolio_task`) is the
    worktree path, not the task_id. Two different task_ids whose card
    overrides both resolve to the same worktree path are only caught by the
    unlocked `_find_conflicting_registration` pre-check and the locked
    re-check inside `_persist_registry_entry` — which runs *after* the
    worktree mutation, not before it — so two such launches racing each
    other still both reach `git worktree add` against the same path. This
    lock, held for that whole window (see `launch_portfolio_task`), closes
    it. Hashed rather than embedded verbatim in the filename: an arbitrary
    absolute path can exceed filesystem name-length limits and contain
    characters a lock directory should not have to sanitize, unlike a
    `task_id` (already regex-validated by `_claim_lock_path`)."""
    resolved = str(Path(worktree_path).expanduser().resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()
    locks_dir = _locks_dir(root)
    candidate = locks_dir / f"workspace-{digest}.lock"
    _assert_within_root(candidate, locks_dir, what="lock")
    return candidate


@dataclass(frozen=True)
class ClaimLockStatus:
    label: str
    path: Path
    exists: bool
    stale: bool
    recoverable: bool
    reason: str
    owner_pid: int | None = None
    age_seconds: float | None = None


_owned_claim_tokens: dict[Path, str] = {}
_owned_claim_tokens_lock = threading.Lock()


def _recovery_lock_path(lock_path: Path) -> Path:
    return lock_path.with_name(lock_path.name + CLAIM_RECOVERY_LOCK_SUFFIX)


def _claim_recovery_lock_path(root: Path, task_id: str) -> Path:
    return _recovery_lock_path(_claim_lock_path(root, task_id))


def _process_identity(pid: int) -> str | None:
    """Return a value that changes when a PID is reused.

    Linux exposes the process start tick directly. On macOS and other POSIX
    hosts `ps lstart` is the portable fallback. Failure to obtain an identity
    is handled conservatively by the caller: a live PID is never reclaimed.
    """
    proc_stat = Path(f"/proc/{pid}/stat")
    try:
        raw = proc_stat.read_text(encoding="utf-8")
        # The command name is parenthesized and may contain spaces. Everything
        # after its final ')' starts at field 3; starttime is field 22.
        tail = raw.rsplit(")", 1)[1].strip().split()
        return f"linux-start-ticks:{tail[19]}"
    except (OSError, IndexError):
        pass
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return f"ps-lstart:{value}" if result.returncode == 0 and value else None


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_claim_metadata(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _inspect_claim_at(lock_path: Path, label: str, *, now: float | None = None) -> ClaimLockStatus:
    """Inspect the claim at `lock_path` without mutating it. Shared core for
    `inspect_claim` (task_id-keyed) and `inspect_workspace_claim`
    (worktree-path-keyed) — `label` is purely descriptive (surfaced on the
    returned `ClaimLockStatus`), never used to derive `lock_path`.

    A lock is automatically recoverable only when its structured owner
    metadata proves that the owning local process no longer exists (or that
    the PID was reused). Legacy/invalid files are reported but deliberately
    not reclaimed: age alone cannot prove that a long-running launch is dead.

    NOTE: "a live owner is never displaced" holds for a single-host lock
    directory. A claim from a *different* host is treated as unrecoverable
    (its PID cannot be checked locally), but the cross-host discriminator is
    the short hostname, which is not globally unique. A lock directory shared
    between two hosts with the same hostname is out of scope -- the same
    single-host assumption the `flock`/`os.link` primitives already require.
    """
    path = lock_path
    try:
        stat = path.stat()
    except FileNotFoundError:
        return ClaimLockStatus(label, path, False, False, False, "lock отсутствует")
    except OSError as exc:
        return ClaimLockStatus(label, path, True, False, False, f"lock недоступен: {exc}")

    current_time = time.time() if now is None else now
    age = max(0.0, current_time - stat.st_mtime)
    metadata = _read_claim_metadata(path)
    if not metadata:
        return ClaimLockStatus(
            label, path, True, False, False,
            "lock создан старой версией или повреждён; владелец не может быть безопасно подтверждён",
            age_seconds=age,
        )

    pid = metadata.get("pid")
    token = metadata.get("token")
    hostname = metadata.get("hostname")
    identity = metadata.get("process_identity")
    if (
        metadata.get("version") != CLAIM_LOCK_VERSION
        or not isinstance(pid, int)
        or not isinstance(token, str)
        or not token
        or not isinstance(hostname, str)
    ):
        return ClaimLockStatus(
            label, path, True, False, False, "метаданные lock неполны; автоматическое снятие небезопасно",
            age_seconds=age,
        )
    if hostname != socket.gethostname():
        return ClaimLockStatus(
            label, path, True, False, False,
            f"lock принадлежит другому хосту ({hostname}); локальная проверка процесса невозможна",
            owner_pid=pid, age_seconds=age,
        )
    if not _pid_is_alive(pid):
        return ClaimLockStatus(
            label, path, True, True, True, f"процесс-владелец PID {pid} не существует",
            owner_pid=pid, age_seconds=age,
        )
    current_identity = _process_identity(pid)
    if identity and current_identity and identity != current_identity:
        return ClaimLockStatus(
            label, path, True, True, True, f"PID {pid} был переиспользован другим процессом",
            owner_pid=pid, age_seconds=age,
        )
    return ClaimLockStatus(
        label, path, True, False, False, f"процесс-владелец PID {pid} активен",
        owner_pid=pid, age_seconds=age,
    )


def inspect_claim(root: Path, task_id: str, *, now: float | None = None) -> ClaimLockStatus:
    """Inspect the per-task_id claim without mutating it."""
    return _inspect_claim_at(_claim_lock_path(root, task_id), task_id, now=now)


def inspect_workspace_claim(root: Path, worktree_path: Path, *, now: float | None = None) -> ClaimLockStatus:
    """Inspect the per-worktree-path claim without mutating it — see
    `_workspace_lock_path` for why this is a separate lock from the
    per-task_id one above."""
    resolved = str(Path(worktree_path).expanduser().resolve())
    return _inspect_claim_at(_workspace_lock_path(root, worktree_path), resolved, now=now)


def _recover_stale_claim_at(lock_path: Path, label: str) -> bool:
    """Remove a provably orphaned claim at `lock_path`, serialized against
    claim/release. Shared core for `recover_stale_claim` (task_id-keyed) and
    `recover_stale_workspace_claim` (worktree-path-keyed).

    The status is re-read while holding the recovery lock and the file token
    is checked again immediately before unlink. This makes recovery safe
    against another process concurrently recovering and acquiring the task.
    """
    guard = _recovery_lock_path(lock_path)
    try:
        with storage.file_lock(
            guard,
            timeout=CLAIM_RECOVERY_LOCK_TIMEOUT_SECONDS,
            poll_seconds=_CLAIM_RECOVERY_LOCK_POLL_SECONDS,
        ):
            status = _inspect_claim_at(lock_path, label)
            if not status.recoverable:
                return False
            metadata = _read_claim_metadata(lock_path)
            if not metadata or not _inspect_claim_at(lock_path, label).recoverable:
                return False
            expected_token = metadata.get("token")
            latest = _read_claim_metadata(lock_path)
            if not latest or latest.get("token") != expected_token:
                return False
            lock_path.unlink(missing_ok=True)
            return True
    except storage.LockTimeoutError:
        return False


def recover_stale_claim(root: Path, task_id: str) -> bool:
    return _recover_stale_claim_at(_claim_lock_path(root, task_id), task_id)


def recover_stale_workspace_claim(root: Path, worktree_path: Path) -> bool:
    resolved = str(Path(worktree_path).expanduser().resolve())
    return _recover_stale_claim_at(_workspace_lock_path(root, worktree_path), resolved)


def _claim_at(lock_path: Path, label: str) -> bool:
    """Atomically claim `lock_path`, recovering a provably orphaned claim.
    Shared core for `_claim` (task_id-keyed) and `_claim_workspace`
    (worktree-path-keyed).

    Creation and recovery share a short-lived OS advisory guard. The claim
    itself contains owner identity and a random token; a live owner's claim
    is never removed, while a process crash is recovered on the next attempt.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        guard_context = storage.file_lock(
            _recovery_lock_path(lock_path),
            timeout=CLAIM_RECOVERY_LOCK_TIMEOUT_SECONDS,
            poll_seconds=_CLAIM_RECOVERY_LOCK_POLL_SECONDS,
        )
        with guard_context:
            if lock_path.exists():
                status = _inspect_claim_at(lock_path, label)
                if not status.recoverable:
                    return False
                metadata = _read_claim_metadata(lock_path)
                if not metadata or not _inspect_claim_at(lock_path, label).recoverable:
                    return False
                lock_path.unlink(missing_ok=True)

            token = uuid.uuid4().hex
            metadata = {
                "version": CLAIM_LOCK_VERSION,
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "process_identity": _process_identity(os.getpid()),
                "created_at": time.time(),
                "token": token,
            }
            # Publish a fully written inode with an atomic, exclusive hard
            # link. A crash can leave only the uniquely named temp file, never
            # a visible empty/partial claim that cannot identify its owner.
            temp_path = lock_path.with_name(f".{lock_path.name}.{token}.tmp")
            fd = os.open(temp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                os.write(fd, (json.dumps(metadata, sort_keys=True) + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                os.link(temp_path, lock_path)
            finally:
                temp_path.unlink(missing_ok=True)
            with _owned_claim_tokens_lock:
                _owned_claim_tokens[lock_path] = token
            return True
    except (FileExistsError, storage.LockTimeoutError):
        return False


def _claim(root: Path, task_id: str) -> bool:
    return _claim_at(_claim_lock_path(root, task_id), task_id)


def _claim_workspace(root: Path, worktree_path: Path) -> bool:
    resolved = str(Path(worktree_path).expanduser().resolve())
    return _claim_at(_workspace_lock_path(root, worktree_path), resolved)


def _release_at(lock_path: Path) -> None:
    try:
        with storage.file_lock(
            _recovery_lock_path(lock_path),
            timeout=CLAIM_RECOVERY_LOCK_TIMEOUT_SECONDS,
            poll_seconds=_CLAIM_RECOVERY_LOCK_POLL_SECONDS,
        ):
            with _owned_claim_tokens_lock:
                token = _owned_claim_tokens.pop(lock_path, None)
            metadata = _read_claim_metadata(lock_path)
            if token and metadata and metadata.get("token") == token:
                lock_path.unlink(missing_ok=True)
    except storage.LockTimeoutError:
        # A timed-out cleanup must not remove a lock whose ownership could
        # have changed. It will be recovered automatically after this process
        # exits if it genuinely becomes orphaned.
        return


def _release(root: Path, task_id: str) -> None:
    _release_at(_claim_lock_path(root, task_id))


def _release_workspace(root: Path, worktree_path: Path) -> None:
    _release_at(_workspace_lock_path(root, worktree_path))


REGISTRY_LOCK_FILE_NAME = "registry.lock"
REGISTRY_LOCK_TIMEOUT_SECONDS = 30.0
_REGISTRY_LOCK_POLL_SECONDS = 0.05


def _registry_lock_path(root: Path) -> Path:
    locks_dir = _locks_dir(root)
    locks_dir.mkdir(parents=True, exist_ok=True)
    return locks_dir / REGISTRY_LOCK_FILE_NAME


@contextlib.contextmanager
def _registry_lock(root: Path, *, timeout: float = REGISTRY_LOCK_TIMEOUT_SECONDS):
    """Cross-process, cross-thread mutual exclusion for the *entire*
    read-modify-write cycle on `data/portfolio_launches.json` — every writer
    of the registry (currently just `_persist_registry_entry`) must acquire
    this for its whole read+write, not just around the final `os.replace`:
    acquiring it only around the write would still let two callers both read
    the same pre-write state and each write back a version missing the
    other's entry (the lost update this lock exists to prevent).

    Thin wrapper around `command_center.storage.file_lock` (a dedicated
    `registry.lock` file, never the registry file itself, so a lock holder
    never blocks a plain `load_registry` read) — see that function's
    docstring for the underlying `fcntl`/`msvcrt` mechanism. Translates a
    `storage.LockTimeoutError` into `PortfolioLaunchError` so every existing
    caller here keeps seeing this module's own exception type.
    """
    lock_path = _registry_lock_path(root)
    try:
        with storage.file_lock(lock_path, timeout=timeout, poll_seconds=_REGISTRY_LOCK_POLL_SECONDS):
            yield
    except storage.LockTimeoutError as exc:
        raise PortfolioLaunchError(
            f"не удалось получить блокировку реестра запусков Portfolio за {timeout:.0f}с: {lock_path}"
        ) from exc


def _load_registry_strict(root: Path) -> dict[str, dict]:
    """Like `load_registry`, but raises `PortfolioLaunchError` instead of
    silently treating a corrupt or non-object registry file as an empty
    registry. `load_registry`'s tolerant default suits its callers (dry-run
    planning, UI history — for those, "nothing recorded yet" and
    "unreadable" are both safe to treat the same way, non-destructively);
    this one guards the write path inside `_registry_lock`, where silently
    treating unreadable-but-nonempty content as `{}` and then persisting
    that would silently discard every previously registered launch."""
    path = _registry_file(root)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PortfolioLaunchError(f"не удалось прочитать реестр запусков Portfolio ({path}): {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PortfolioLaunchError(
            f"реестр запусков Portfolio повреждён и не будет тихо перезаписан ({path}): {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PortfolioLaunchError(
            f"реестр запусков Portfolio имеет неожиданный формат (ожидался JSON-объект): {path}"
        )
    return data


def _persist_registry_entry(root: Path, task_id: str, entry: dict) -> str | None:
    """The only function that writes `data/portfolio_launches.json`. Re-reads
    the registry, re-checks for a duplicate/conflicting registration, and
    writes the merged result — all inside one `_registry_lock` acquisition,
    so the read this decision is based on and the write that commits it are
    always adjacent: no other concurrent writer (this process or another)
    can slip a write in between. `launch_batch` gets the exact same
    guarantee for free, by calling `launch_portfolio_task` (which calls this)
    rather than writing the registry itself.

    Returns `None` on success, or a blocking message if `task_id` (or its
    branch/worktree) was claimed by someone else in the window between this
    launch's own `_claim` and this call — vanishingly unlikely given `_claim`
    already serializes same-`task_id` launches, but the guarantee this
    function gives is unconditional rather than assumed.
    """
    with _registry_lock(root):
        registry = _load_registry_strict(root)
        if task_id in registry:
            return "задача уже была запущена ранее — см. журнал запусков Portfolio"
        conflict_owner = _find_conflicting_registration(
            registry, branch=entry.get("branch"), worktree=entry.get("worktree"), exclude_task_id=task_id
        )
        if conflict_owner:
            return f"конфликт: ветка/worktree уже используется задачей {conflict_owner}"
        registry[task_id] = entry
        save_registry(root, registry)
    return None


# --------------------------------------------------------------------------
# Dry-run planning
# --------------------------------------------------------------------------


@dataclass
class LaunchPlan:
    task_id: str
    project: str
    title: str | None
    source_path: str
    lane: str
    repository_root: str | None
    base_branch: str | None
    base_sha: str | None
    branch: str
    worktree: str
    task_type: str
    requested_branch: str | None = None
    branch_source: str = SOURCE_GENERATED_DEFAULT
    requested_worktree: str | None = None
    worktree_source: str = SOURCE_GENERATED_DEFAULT
    worktree_mode: str = WORKTREE_MODE_NEW
    # Displayable "Launch profile" / "Permission profile" for this launch —
    # which executor and which tool-permission model the run will actually get
    # (see `command_center.worktree_launcher.describe_launch_profile`). Purely
    # descriptive; the enforcement lives in `agent_runner`/`runtime.supervisor`.
    executor_id: str = "claude_code"
    launch_profile_label: str = ""
    permission_profile_key: str = ""
    permission_profile_label: str = ""
    permission_profile_summary: str = ""
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def launchable(self) -> bool:
        return not self.blockers


def build_launch_plan(
    task: PortfolioTask,
    *,
    tasks_by_id: dict[str, PortfolioTask],
    repository_paths: dict[str, str | None],
    registry: dict[str, dict] | None = None,
) -> LaunchPlan:
    """Pure, read-only. Never mutates the filesystem, never runs a git-write
    subcommand — only `command_center.git_info`'s read-only helpers plus
    `git check-ref-format`/`rev-parse --git-common-dir` (both read-only)."""
    registry = registry or {}
    blockers: list[str] = []
    warnings: list[str] = []

    missing_fields = task.missing_required_fields()
    if missing_fields:
        blockers.append(f"в карточке отсутствуют обязательные поля: {', '.join(missing_fields)}")

    if task.lane != "ready":
        blockers.append(f"задача находится в статусе «{task.lane}», автозапуск разрешён только для «ready»")

    # Advisory/fast-path only: `registry` here is whatever the caller loaded
    # (unlocked), so this can race with a concurrent launch. The authoritative
    # duplicate/conflict decision is made inside `_persist_registry_entry`
    # under `_registry_lock` — do not remove that locked re-check in favor of
    # this one.
    if task.task_id and task.task_id in registry:
        blockers.append(ALREADY_LAUNCHED_BLOCKER)

    repo_root: Path | None = None
    repo_root_raw = repository_paths.get(task.project) if task.project else None
    if not repo_root_raw:
        blockers.append(f"репозиторий для проекта «{task.project}» не сопоставлен — настройте его в конфигурации")
    else:
        repo_root = Path(repo_root_raw).expanduser().resolve()
        if not repo_root.is_dir():
            blockers.append(f"путь репозитория не существует: {repo_root}")
        elif not (repo_root / ".git").exists():
            blockers.append(f"путь репозитория не является git-репозиторием: {repo_root}")

    resolved_branch, branch_source, branch_error = resolve_branch(task)
    if branch_error:
        blockers.append(branch_error)

    resolved_worktree, worktree_source, worktree_error = resolve_worktree(task)
    if worktree_error:
        blockers.append(worktree_error)

    branch = resolved_branch or ""
    worktree = str(resolved_worktree) if resolved_worktree else ""

    worktree_mode = WORKTREE_MODE_NEW
    base_branch = task.base_branch
    base_sha: str | None = None
    attach_existing_branch = False

    if resolved_worktree is not None and resolved_worktree.exists():
        worktree_mode = WORKTREE_MODE_EXISTING
        if not resolved_worktree.is_dir():
            blockers.append(f"существующий путь worktree не является директорией: {resolved_worktree}")
        else:
            status = git_info.get_status(resolved_worktree)
            if not status.get("is_repo"):
                blockers.append(f"существующий worktree не является git-репозиторием: {resolved_worktree}")
            else:
                metadata_ok, metadata_detail = worktree_launcher.git_metadata_accessible(resolved_worktree)
                if not metadata_ok:
                    blockers.append(metadata_detail)
                if repo_root is not None:
                    existing_common = _git_common_dir(resolved_worktree)
                    canonical_common = _git_common_dir(repo_root)
                    if existing_common is None or canonical_common is None or existing_common != canonical_common:
                        blockers.append(
                            f"существующий worktree принадлежит другому репозиторию, а не сопоставленному «{repo_root}»"
                        )
                    # Stronger than the git-common-dir check above: require the
                    # path to be an actually *registered* worktree of the
                    # mapped repository (present in `git worktree list`), not
                    # merely a directory sharing its common dir. Catches a
                    # hand-built `.git` gitfile / a pruned-but-not-removed
                    # worktree directory (task AICC-LAUNCH-001: "registered
                    # worktree" validation; "never launches against the wrong
                    # worktree").
                    elif not worktree_launcher.is_registered_worktree(repo_root, resolved_worktree):
                        blockers.append(
                            f"существующий каталог не является зарегистрированным worktree репозитория «{repo_root}» "
                            "(отсутствует в git worktree list)"
                        )
                actual_branch = status.get("branch")
                if branch and actual_branch != branch:
                    blockers.append(
                        f"существующий worktree на ветке «{actual_branch}», ожидалась «{branch}»"
                    )
                if status.get("dirty"):
                    blockers.append(f"существующий worktree не чист (uncommitted changes): {resolved_worktree}")
            head_result = git_info.run_git_command(resolved_worktree, ["rev-parse", "HEAD"])
            base_sha = head_result.stdout.strip() if head_result and head_result.returncode == 0 else None
    else:
        if resolved_branch and repo_root is not None:
            existing_branches = git_info.get_branches(repo_root)
            if resolved_branch in existing_branches:
                if branch_source == SOURCE_CARD_OVERRIDE:
                    attach_existing_branch = True
                    warnings.append(
                        f"ветка «{resolved_branch}» уже существует — будет привязана к новому worktree "
                        "без создания новой ветки"
                    )
                else:
                    blockers.append(f"ветка уже существует: {resolved_branch}")

        if repo_root is not None:
            if not attach_existing_branch:
                if not base_branch:
                    blockers.append("в карточке не указана base_branch")
                else:
                    verify = git_info.run_git_command(repo_root, ["rev-parse", "--verify", f"{base_branch}^{{commit}}"])
                    if verify is None or verify.returncode != 0:
                        blockers.append(f"базовая ветка/ref не найдена в репозитории: {base_branch}")
                    else:
                        sha_result = git_info.run_git_command(repo_root, ["rev-parse", base_branch])
                        base_sha = sha_result.stdout.strip() if sha_result and sha_result.returncode == 0 else None
            elif base_branch:
                # Informational only in the attach case — `base_branch` is
                # not consumed by the `git worktree add <path> <branch>`
                # command that will actually run, so an invalid/missing
                # value here is never a blocker.
                verify = git_info.run_git_command(repo_root, ["rev-parse", "--verify", f"{base_branch}^{{commit}}"])
                if verify is not None and verify.returncode == 0:
                    sha_result = git_info.run_git_command(repo_root, ["rev-parse", base_branch])
                    base_sha = sha_result.stdout.strip() if sha_result and sha_result.returncode == 0 else None

    # Advisory/fast-path only, same caveat as the duplicate check above — this
    # `registry` snapshot is unlocked and can be stale. The authoritative
    # conflict decision is `_find_conflicting_registration` called again
    # inside `_persist_registry_entry` under `_registry_lock`.
    conflict_owner = _find_conflicting_registration(
        registry, branch=branch or None, worktree=worktree or None, exclude_task_id=task.task_id
    )
    if conflict_owner:
        blockers.append(f"конфликт: ветка/worktree уже используется другой запущенной задачей ({conflict_owner})")

    unmet = unmet_requirements(task, tasks_by_id)
    if unmet:
        blockers.append(f"не выполнены зависимости (requires): {', '.join(unmet)}")

    if task.conflicts_with:
        warnings.append(f"карточка объявляет conflicts_with: {', '.join(task.conflicts_with)} — проверьте вручную")
    if task.gated_by:
        warnings.append(f"карточка требует gated_by: {', '.join(task.gated_by)} — не проверяется автоматически")
    if task.autonomy and task.autonomy != "confirmed":
        warnings.append(f"autonomy карточки: «{task.autonomy}» (ожидалось «confirmed»)")

    resolved_task_type = _map_task_type(task.type)
    launch_profile = worktree_launcher.describe_launch_profile(
        executor_id="claude_code", task_type=resolved_task_type
    )

    return LaunchPlan(
        task_id=task.task_id or "",
        project=task.project or "",
        title=task.title,
        source_path=str(task.source_path),
        lane=task.lane,
        repository_root=str(repo_root) if repo_root else None,
        base_branch=base_branch,
        base_sha=base_sha,
        branch=branch,
        worktree=worktree,
        task_type=resolved_task_type,
        requested_branch=task.branch,
        branch_source=branch_source,
        requested_worktree=task.worktree,
        worktree_source=worktree_source,
        worktree_mode=worktree_mode,
        executor_id=launch_profile.executor_id,
        launch_profile_label=launch_profile.label,
        permission_profile_key=launch_profile.permission.key,
        permission_profile_label=launch_profile.permission.label,
        permission_profile_summary=launch_profile.permission.summary,
        blockers=blockers,
        warnings=warnings,
    )


# --------------------------------------------------------------------------
# Card presentation — single, unambiguous launch-state for the UI
# --------------------------------------------------------------------------
#
# `LaunchPlan.launchable` alone cannot drive a card's headline status: it is
# `False` both when a precondition is genuinely broken (missing repo mapping,
# bad worktree, ...) and when the *only* reason is that this task was already
# launched before — and those two cases must never look the same (a prior
# launch is not an error). `build_card_presentation` is the one place that
# tells them apart and picks exactly one headline status to show.

STATUS_READY = "ready"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_ALREADY_LAUNCHED = "already_launched"
STATUS_BLOCKED = "blocked"

STATUS_LABELS: dict[str, str] = {
    STATUS_READY: "Ready",
    STATUS_RUNNING: "Running",
    STATUS_COMPLETED: "Completed",
    STATUS_FAILED: "Failed",
    STATUS_CANCELLED: "Cancelled",
    STATUS_ALREADY_LAUNCHED: "Already launched",
    STATUS_BLOCKED: "Blocked",
}

# Maps a v2 run's `state` (`command_center.runtime.db`) onto a card status.
# `CANCELLED`/`INTERRUPTED` both read as "Cancelled" here — from the card's
# point of view (should a plain `Launch` ever be offered again?) they mean
# the same thing: the run stopped without succeeding or reporting a failure.
# Any state not listed here (or no run found at all) falls back to
# `STATUS_ALREADY_LAUNCHED` in `build_card_presentation` — the safe fallback
# the task description calls for when the real status can't be determined.
_RUN_STATE_TO_STATUS: dict[str, str] = {
    "PREPARED": STATUS_RUNNING,
    "QUEUED": STATUS_RUNNING,
    "RUNNING": STATUS_RUNNING,
    "COMPLETED": STATUS_COMPLETED,
    "FAILED": STATUS_FAILED,
    "CANCELLED": STATUS_CANCELLED,
    "INTERRUPTED": STATUS_CANCELLED,
}

_EXISTING_RUN_SEVERITY: dict[str, str] = {
    STATUS_RUNNING: "info",
    STATUS_COMPLETED: "success",
    STATUS_FAILED: "error",
    STATUS_CANCELLED: "warning",
    STATUS_ALREADY_LAUNCHED: "warning",
}

# The message *alert box* severity for an existing-run card — deliberately
# never "error", even when the run itself `STATUS_FAILED`: the badge can (and
# should) say "Failed" in red, but the explanatory message is informational/
# advisory ("here's what happened, duplicate prevention is working as
# intended"), not a report of something wrong with *this* render.
_EXISTING_RUN_MESSAGE_SEVERITY: dict[str, str] = {
    STATUS_RUNNING: "info",
    STATUS_COMPLETED: "info",
    STATUS_FAILED: "warning",
    STATUS_CANCELLED: "warning",
    STATUS_ALREADY_LAUNCHED: "warning",
}


@dataclass(frozen=True)
class PortfolioCardPresentation:
    """The single, unambiguous launch-state a Portfolio task card renders as.
    Exactly one of `status_key`/`status_label` is the card's headline status;
    `launch_allowed` alone gates the `Launch` button. A card's workflow
    status (`PortfolioTask.status`/`.lane`, e.g. "ready") is metadata about
    the *card*, never this launch-state, and must only ever be shown
    labelled as such (e.g. inside an expander), never as the headline."""

    status_key: str
    status_label: str
    severity: str  # "success" | "info" | "warning" | "error" — headline badge
    launch_allowed: bool
    existing_run_id: str | None
    message: str | None
    message_severity: str = "info"  # "info" | "warning" | "error" — the alert box, if any


def build_card_presentation(
    plan: LaunchPlan,
    *,
    registry_entry: dict | None = None,
    existing_run: dict | None = None,
) -> PortfolioCardPresentation:
    """Pure — never touches the filesystem, the registry file, or the
    execution database itself. `registry_entry` is this task's own
    `registry.get(task_id)` (if any); `existing_run` is whatever the caller
    got back from `ExecutionCenterAPI.get_run(registry_entry["run_id"])` —
    `None` (run row not found, or the caller has no run_id/api to look it up
    with) always falls back to the safe `STATUS_ALREADY_LAUNCHED`, never a
    guess.

    A genuine precondition blocker (missing repo mapping, bad worktree, an
    unmet `requires`, ...) always wins over "already launched" — the mere
    existence of a prior run must never mask a real error (task description
    §6)."""
    real_blockers = [b for b in plan.blockers if b != ALREADY_LAUNCHED_BLOCKER]

    if real_blockers:
        return PortfolioCardPresentation(
            status_key=STATUS_BLOCKED,
            status_label=STATUS_LABELS[STATUS_BLOCKED],
            severity="error",
            launch_allowed=False,
            existing_run_id=(registry_entry or {}).get("run_id"),
            message="; ".join(real_blockers),
            message_severity="error",
        )

    if registry_entry is not None:
        run_id = registry_entry.get("run_id")
        run_state = (existing_run or {}).get("state")
        status_key = _RUN_STATE_TO_STATUS.get(run_state, STATUS_ALREADY_LAUNCHED)

        detail_parts = [f"run `{run_id}`"] if run_id else []
        started_at = (existing_run or {}).get("started_at") or registry_entry.get("launched_at")
        if started_at:
            detail_parts.append(f"начат: {started_at}")
        completed_at = (existing_run or {}).get("completed_at")
        if completed_at:
            detail_parts.append(f"завершён: {completed_at}")
        message = (
            f"Задача уже была запущена ранее ({STATUS_LABELS[status_key]}"
            + (" — " + " · ".join(detail_parts) if detail_parts else "")
            + "). Повторный запуск отключён — это ожидаемая защита от дублирования; "
            "откройте существующий запуск или журнал запусков Portfolio, чтобы посмотреть детали."
        )

        return PortfolioCardPresentation(
            status_key=status_key,
            status_label=STATUS_LABELS[status_key],
            severity=_EXISTING_RUN_SEVERITY[status_key],
            launch_allowed=False,
            existing_run_id=run_id,
            message=message,
            message_severity=_EXISTING_RUN_MESSAGE_SEVERITY[status_key],
        )

    return PortfolioCardPresentation(
        status_key=STATUS_READY,
        status_label=STATUS_LABELS[STATUS_READY],
        severity="success",
        launch_allowed=plan.launchable,
        existing_run_id=None,
        message=None,
    )


def build_batch_plan(
    tasks: list[PortfolioTask],
    *,
    tasks_by_id: dict[str, PortfolioTask],
    repository_paths: dict[str, str | None],
    registry: dict[str, dict] | None = None,
) -> list[LaunchPlan]:
    registry = registry or {}
    return [
        build_launch_plan(task, tasks_by_id=tasks_by_id, repository_paths=repository_paths, registry=registry)
        for task in tasks
    ]


def _detect_batch_conflicts(plans: list[LaunchPlan]) -> dict[str, str]:
    """task_id -> blocking message for any task in `plans` whose *resolved*
    branch or worktree is also requested by another task in the same batch —
    caught before any mutation, since two tasks launched into the same
    branch/worktree would corrupt each other's work."""
    by_branch: dict[str, list[str]] = {}
    by_worktree: dict[str, list[str]] = {}
    for plan in plans:
        if plan.branch:
            by_branch.setdefault(plan.branch, []).append(plan.task_id)
        if plan.worktree:
            by_worktree.setdefault(plan.worktree, []).append(plan.task_id)

    conflicts: dict[str, str] = {}
    for branch, task_ids in by_branch.items():
        if len(task_ids) > 1:
            others = ", ".join(sorted(set(task_ids)))
            for task_id in task_ids:
                conflicts[task_id] = f"конфликт в пакете: несколько задач запрашивают одну и ту же ветку «{branch}» ({others})"
    for worktree, task_ids in by_worktree.items():
        if len(task_ids) > 1:
            others = ", ".join(sorted(set(task_ids)))
            message = f"конфликт в пакете: несколько задач запрашивают один и тот же worktree «{worktree}» ({others})"
            for task_id in task_ids:
                conflicts[task_id] = f"{conflicts[task_id]}; {message}" if task_id in conflicts else message
    return conflicts


# --------------------------------------------------------------------------
# Prompt generation
# --------------------------------------------------------------------------


def build_agent_prompt(task: PortfolioTask, plan: LaunchPlan) -> str:
    """The full instruction handed to the launched agent — the original card
    verbatim, every location/identity fact the agent needs, and the explicit
    guardrails required by the founder prompt (no other repos, no merge, no
    push, report format). The final-report section intentionally matches the
    headings `command_center.report_parser.parse_report` already recognizes
    (Verdict, Findings by severity, Files Modified/Created/Deleted, Commit
    Hash, Branch, Validation, Git Status, Recommended Next Action), so a
    Portfolio-launched run's report is parsed by the exact same, already-
    tested code path as every other run in this app."""
    deliverables = "\n".join(f"- {item}" for item in task.deliverables) or "- (не указано в карточке)"
    stop_conditions = "\n".join(f"- {item}" for item in task.stop_conditions) or "- (не указано в карточке)"
    validation_commands = "\n".join(task.validation) or "# в карточке не указаны команды проверки"

    if plan.worktree_mode == WORKTREE_MODE_EXISTING:
        worktree_note = (
            "Этот worktree УЖЕ СУЩЕСТВОВАЛ и был проверен AI Command Center перед запуском "
            "(верная ветка, тот же репозиторий, чистое рабочее дерево) — он не был создан этим запуском."
        )
        worktree_rules = (
            "- Worktree уже существовал до этого запуска. НЕ переключай ветку, НЕ выполняй `git reset`/"
            "`git clean`, НЕ изменяй состояние других worktree этого репозитория.\n"
            "- Сохраняй предшествующее состояние репозитория, кроме изменений, требуемых этой задачей."
        )
    else:
        worktree_note = "Этот worktree был создан AI Command Center специально для этого запуска."
        worktree_rules = (
            "- Worktree создан специально для этой задачи. НЕ изменяй другие worktree или репозитории."
        )

    return f"""# Portfolio Task: {task.task_id}

Ты выполняешь задачу, полученную из Portfolio через AI Command Center. Ниже —
полная исходная карточка задачи, за ней — параметры окружения выполнения и
обязательные ограничения.

## Исходная карточка (полностью, без изменений)

```markdown
{task.raw_text}
```

## Окружение выполнения

- Task ID: `{task.task_id}`
- Project: `{task.project}`
- Repository (базовый): `{plan.repository_root}`
- Worktree (рабочая директория — работай только здесь): `{plan.worktree}`
- Branch: `{plan.branch}`
- Base branch: `{plan.base_branch}`
- Base/HEAD commit SHA: `{plan.base_sha}`
- Worktree mode: `{plan.worktree_mode}` — {worktree_note}

## Acceptance criteria (deliverables)

{deliverables}

## Stop conditions

{stop_conditions}

## Validation commands

```bash
{validation_commands}
```

## Обязательные ограничения

- Работай ТОЛЬКО в указанном worktree ({plan.worktree}). Не изменяй файлы ни в
  каком другом репозитории или worktree на этой машине.
{worktree_rules}
- НЕ выполняй `git merge`, `git push`, не открывай и не сливай Pull Request.
- НЕ отмечай задачу выполненной в Portfolio и не перемещай карточку задачи.
- Не выходи за рамки объявленных deliverables/scope этой карточки.
- Commit разрешён только в рамках ветки `{plan.branch}`; никогда не коммить в другую ветку.

## Формат итогового отчёта

Заверши работу отчётом в следующем формате (используй эти заголовки
дословно):

```markdown
## Verdict

READY FOR COMMIT | APPROVED FOR COMMIT | NOT APPROVED FOR COMMIT | FAILED

## Findings

**Blocker:** ...
**High:** ...
**Medium:** ...
**Low:** ...

## Files Modified

- ...

## Files Created

- ...

## Files Deleted

- ...

Commit Hash: <hash или "нет">
Branch: {plan.branch}

## Validation

<вывод команд проверки выше>

## Git Status

<git status --porcelain>

## Recommended Next Action

<...>
```
"""


# --------------------------------------------------------------------------
# Git worktree/branch mutation (the only write git subcommands in this module)
# --------------------------------------------------------------------------


class PortfolioLaunchError(Exception):
    """Raised for any failure that must abort a launch before the agent is
    started — never raised after `execution_queue.launch_ready` has actually
    started a run."""


def _run_git_write(args: list[str], *, cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)


def create_worktree(repo_root: Path, *, branch: str, worktree_path: Path, base_branch: str) -> None:
    """`git worktree add -b <branch> <path> <base_branch>` — a brand-new
    branch and a brand-new worktree. Only called when both need to be
    created (`worktree_mode == "new"`, no existing branch to attach)."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git_write(
        ["worktree", "add", "-b", branch, str(worktree_path), base_branch],
        cwd=repo_root,
    )
    if result.returncode != 0:
        raise PortfolioLaunchError(
            f"git worktree add failed: {(result.stderr or result.stdout).strip()}"
        )


def attach_worktree(repo_root: Path, *, branch: str, worktree_path: Path) -> None:
    """`git worktree add <path> <branch>` — attaches an already-existing
    branch (named by an explicit card override) to a brand-new worktree; no
    `-b`, no base ref consumed. Only the worktree is created here — the
    branch already existed and must never be deleted by this launch's
    rollback."""
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    result = _run_git_write(["worktree", "add", str(worktree_path), branch], cwd=repo_root)
    if result.returncode != 0:
        raise PortfolioLaunchError(
            f"git worktree add (existing branch) failed: {(result.stderr or result.stdout).strip()}"
        )


def remove_worktree(repo_root: Path, worktree_path: Path) -> None:
    """Best-effort rollback — never raises. Only called for a worktree this
    launch attempt itself created (see `_rollback_worktree`)."""
    _run_git_write(["worktree", "remove", "--force", str(worktree_path)], cwd=repo_root)


def delete_branch(repo_root: Path, branch: str) -> None:
    """Best-effort rollback counterpart to `create_worktree` — never raises.
    Only called for a branch this launch attempt itself created (never for
    an attached-but-pre-existing branch, and never for an existing
    worktree's branch — see `_rollback_worktree`)."""
    _run_git_write(["branch", "-D", branch], cwd=repo_root)


def _rollback_worktree(
    repo_root: Path, worktree_path: Path, *, worktree_created: bool, branch: str, branch_created: bool
) -> None:
    """Removes only what this launch attempt itself created. An existing
    worktree (`worktree_mode == "existing"`) is never touched — neither
    `worktree_created` nor `branch_created` is ever `True` for one, by
    construction (see `launch_portfolio_task`). Attaching a pre-existing
    branch to a new worktree sets `worktree_created=True` but
    `branch_created=False`, so the worktree is removed but the branch that
    predates this launch survives."""
    if worktree_created:
        remove_worktree(repo_root, worktree_path)
    if branch_created:
        delete_branch(repo_root, branch)


# --------------------------------------------------------------------------
# Real launch
# --------------------------------------------------------------------------


@dataclass
class PortfolioLaunchResult:
    task_id: str
    launched: bool
    run_id: str | None = None
    message: str = ""
    plan: LaunchPlan | None = None


def _synthetic_task(task: PortfolioTask, plan: LaunchPlan, *, worktree_path: Path, prompt: str) -> dict:
    """A plain task dict shaped exactly like `command_center.tasks_repository`
    records, so it can be handed to `execution_queue`/`launch_service`
    unchanged — reusing that stack rather than a parallel one. Never
    persisted to `data/tasks.json`; it exists only for the duration of this
    one launch call."""
    record: dict = {
        "id": task.task_id,
        "project": task.project,
        "title": task.title or task.task_id,
        "status": "Ready",
        "depends_on": [],
    }
    models.normalize_task_workflow(record)
    models.normalize_task_execution(record)
    record["goal"] = task.title or task.task_id
    record["prompt"] = prompt
    record["workspace_path"] = str(worktree_path)
    record["branch"] = plan.branch
    record["executor"] = "claude_code"
    record["agent"] = "claude_code"
    record["task_type"] = plan.task_type
    return record


def launch_portfolio_task(
    root: Path,
    task: PortfolioTask,
    *,
    tasks_by_id: dict[str, PortfolioTask],
    repository_paths: dict[str, str | None],
    execution_center_api,
    confirmed: bool,
) -> PortfolioLaunchResult:
    """Launch exactly one Portfolio task. Requires `confirmed=True` — the
    caller's (UI's) explicit human confirmation; never launches on a plain
    render path.

    On any failure prior to the agent actually starting, this function rolls
    back whatever *it* created (worktree, branch, queue entry) — never an
    existing worktree/branch this launch reused — and never leaves either the
    task_id claim or the worktree-path claim held, so a retry after fixing
    the reported problem is always possible.
    Once `execution_queue.launch_ready` reports the run started, nothing is
    rolled back regardless of what happens afterward (the run itself, and
    its own workspace lock, are the system of record from that point on)."""
    if not confirmed:
        raise PortfolioLaunchError("запуск требует явного подтверждения")

    registry = load_registry(root)
    plan = build_launch_plan(task, tasks_by_id=tasks_by_id, repository_paths=repository_paths, registry=registry)
    if not plan.launchable:
        return PortfolioLaunchResult(task_id=task.task_id or "", launched=False, message="; ".join(plan.blockers), plan=plan)

    # `plan.launchable` guarantees `task.task_id`/`task.project`/
    # `plan.repository_root`/`plan.branch`/`plan.worktree` are all present —
    # `build_launch_plan` adds a blocker for each of these otherwise —
    # narrowing them here (rather than threading `Optional` through every
    # call below) reflects that guarantee for the type checker too.
    assert task.task_id is not None
    assert task.project is not None
    assert plan.repository_root is not None
    assert plan.branch
    assert plan.worktree

    if not _claim(root, task.task_id):
        return PortfolioLaunchResult(
            task_id=task.task_id,
            launched=False,
            message="запуск для этой задачи уже выполняется или был выполнен параллельно",
            plan=plan,
        )

    repo_root = Path(plan.repository_root)
    worktree_path = Path(plan.worktree)
    branch_created = False
    worktree_created = False

    # The `task.task_id` claim above only serializes two launches of the
    # *same* task_id — it does nothing for two different task_ids whose card
    # overrides both resolve to the same `worktree_path` (an explicit-override
    # collision; see `_workspace_lock_path`). Claim the path itself too,
    # before the conflict re-check below, so that check and the worktree
    # mutation that follows it run under a lock actually keyed on the
    # resource they touch — closing the race `_find_conflicting_registration`
    # alone cannot (it is only re-checked, locked, inside
    # `_persist_registry_entry`, which runs *after* the worktree is created).
    if not _claim_workspace(root, worktree_path):
        _release(root, task.task_id)
        return PortfolioLaunchResult(
            task_id=task.task_id,
            launched=False,
            message=f"запуск в этот worktree уже выполняется параллельно: {worktree_path}",
            plan=plan,
        )

    try:
        # Re-check duplicate/conflict state under the claim — closes the
        # race window between building `plan` above and holding the claim
        # (two concurrent callers could have both passed the earlier,
        # unlocked check).
        registry = load_registry(root)
        if task.task_id in registry:
            return PortfolioLaunchResult(
                task_id=task.task_id, launched=False, message="задача уже была запущена ранее", plan=plan
            )
        conflict_owner = _find_conflicting_registration(
            registry, branch=plan.branch, worktree=plan.worktree, exclude_task_id=task.task_id
        )
        if conflict_owner:
            return PortfolioLaunchResult(
                task_id=task.task_id,
                launched=False,
                message=f"конфликт: ветка/worktree уже используется задачей {conflict_owner}",
                plan=plan,
            )

        if plan.worktree_mode == WORKTREE_MODE_EXISTING:
            # Never call `git worktree add` here — re-validate the existing
            # worktree fresh (it could have changed since the dry-run plan
            # was built) and launch directly into it if it still checks out.
            status = git_info.get_status(worktree_path)
            if not worktree_path.is_dir() or not status.get("is_repo"):
                return PortfolioLaunchResult(
                    task_id=task.task_id, launched=False, message=f"существующий worktree недоступен: {worktree_path}", plan=plan
                )
            if status.get("branch") != plan.branch:
                return PortfolioLaunchResult(
                    task_id=task.task_id,
                    launched=False,
                    message=f"существующий worktree сменил ветку: ожидалась «{plan.branch}», сейчас «{status.get('branch')}»",
                    plan=plan,
                )
            if status.get("dirty"):
                return PortfolioLaunchResult(
                    task_id=task.task_id, launched=False, message=f"существующий worktree не чист: {worktree_path}", plan=plan
                )
        else:
            if worktree_path.exists():
                return PortfolioLaunchResult(
                    task_id=task.task_id, launched=False, message=f"путь worktree уже существует: {worktree_path}", plan=plan
                )
            branch_exists = plan.branch in git_info.get_branches(repo_root)
            try:
                if branch_exists:
                    attach_worktree(repo_root, branch=plan.branch, worktree_path=worktree_path)
                else:
                    if not plan.base_branch:
                        return PortfolioLaunchResult(
                            task_id=task.task_id, launched=False, message="не удалось создать worktree: base_branch не определён", plan=plan
                        )
                    create_worktree(repo_root, branch=plan.branch, worktree_path=worktree_path, base_branch=plan.base_branch)
                    branch_created = True
                worktree_created = True
            except PortfolioLaunchError as exc:
                return PortfolioLaunchResult(task_id=task.task_id, launched=False, message=str(exc), plan=plan)

        prompt = build_agent_prompt(task, plan)
        synthetic_task = _synthetic_task(task, plan, worktree_path=worktree_path, prompt=prompt)
        synthetic_tasks_by_id = {synthetic_task["id"]: synthetic_task}

        entries = execution_queue.enqueue_and_persist(root, synthetic_task, synthetic_tasks_by_id)
        new_entry = next(
            (e for e in entries if e.get("task_id") == task.task_id and e.get("state") in execution_queue.OPEN_STATES),
            None,
        )
        if new_entry is None:
            _rollback_worktree(
                repo_root, worktree_path, worktree_created=worktree_created, branch=plan.branch, branch_created=branch_created
            )
            return PortfolioLaunchResult(
                task_id=task.task_id, launched=False, message="не удалось создать запись очереди запуска", plan=plan
            )
        _, results = execution_queue.launch_ready(
            root,
            entries,
            [synthetic_task],
            synthetic_tasks_by_id,
            {
                task.project: {
                    "repository_path": str(repo_root),
                    "base_branch": plan.base_branch,
                }
            },
            execution_center_api,
            entry_ids=[new_entry["id"]],
        )
        result = next((r for r in results if r.entry_id == new_entry["id"]), None)

        if result is None or not result.launched:
            execution_queue.dequeue_and_persist(root, new_entry["id"])
            _rollback_worktree(
                repo_root, worktree_path, worktree_created=worktree_created, branch=plan.branch, branch_created=branch_created
            )
            message = result.message if result else "запуск не выполнен"
            return PortfolioLaunchResult(task_id=task.task_id, launched=False, message=message, plan=plan)

        entry = {
            "task_id": task.task_id,
            "project": task.project,
            "branch": plan.branch,
            "worktree": str(worktree_path),
            "worktree_mode": plan.worktree_mode,
            "repository_root": str(repo_root),
            "base_branch": plan.base_branch,
            "base_sha": plan.base_sha,
            "run_id": result.run_id,
            "queue_entry_id": new_entry["id"],
            "launched_at": models.iso_now(),
            "source_path": str(task.source_path),
        }
        # The agent run has already started (`result.launched` above) — from
        # here on nothing is rolled back regardless of what happens (see
        # module docstring), so a registry conflict at this point (someone
        # else registered this exact task_id/branch/worktree in the window
        # between our `_claim` and here) is reported but the live run stands.
        conflict_message = _persist_registry_entry(root, task.task_id, entry)
        if conflict_message is not None:
            return PortfolioLaunchResult(
                task_id=task.task_id, launched=True, run_id=result.run_id, message=conflict_message, plan=plan
            )

        return PortfolioLaunchResult(task_id=task.task_id, launched=True, run_id=result.run_id, plan=plan)
    finally:
        _release_workspace(root, worktree_path)
        _release(root, task.task_id)


def launch_batch(
    root: Path,
    tasks: list[PortfolioTask],
    *,
    tasks_by_id: dict[str, PortfolioTask],
    repository_paths: dict[str, str | None],
    execution_center_api,
    confirmed: bool,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_LAUNCHES,
) -> list[PortfolioLaunchResult]:
    """Launch every task in `tasks`, in order, reporting a result for every
    one. Two passes before any mutation:

    1. `_detect_batch_conflicts` — any two tasks in this same batch resolving
       to the same branch or worktree are both skipped with a blocking
       message (an override collision, since generated defaults are unique
       per task_id and can't collide with each other).
    2. The concurrency limit (`runtime.db.EXECUTION_CENTER_ACTIVE_STATES`
       count vs. `max_concurrent`) is checked before each remaining launch,
       reusing the Execution Center's own run-state accounting rather than a
       second concurrency tracker.
    """
    registry = load_registry(root)
    plans = build_batch_plan(tasks, tasks_by_id=tasks_by_id, repository_paths=repository_paths, registry=registry)
    batch_conflicts = _detect_batch_conflicts(plans)

    results: list[PortfolioLaunchResult] = []
    for task, plan in zip(tasks, plans):
        task_id = task.task_id or ""
        if task_id in batch_conflicts:
            results.append(PortfolioLaunchResult(task_id=task_id, launched=False, message=batch_conflicts[task_id], plan=plan))
            continue

        active_count = len(execution_center_api.list_runs(states=runtime_db.EXECUTION_CENTER_ACTIVE_STATES))
        if active_count >= max_concurrent:
            results.append(
                PortfolioLaunchResult(
                    task_id=task_id,
                    launched=False,
                    message=f"достигнут лимит одновременных запусков ({max_concurrent})",
                )
            )
            continue
        results.append(
            launch_portfolio_task(
                root,
                task,
                tasks_by_id=tasks_by_id,
                repository_paths=repository_paths,
                execution_center_api=execution_center_api,
                confirmed=confirmed,
            )
        )
    return results
