"""Shared workspace provisioning + mandatory pre-launch verification.

This is the **single** implementation every task-launch path routes through
to guarantee an agent can only ever start inside the correct, isolated git
worktree on the expected branch. It exists because of a live production
defect: a task whose expected branch was `audit/execution-queue` launched
Claude in the *main* repository on `main` (the silent
`repository_path` fallback in `command_center.launch.resolve_workspace_path`),
reached "Workspace Verified", and left untracked files in the main repo.
Nothing verified the branch before starting the process — branch mismatch was
only a *warning*, never a hard gate.

Two responsibilities, kept separate so callers can compose them:

- `provision_workspace(spec)` — idempotent worktree provisioning. Creates the
  worktree (and, when needed, the branch from `base_branch`) if it is absent;
  reuses an already-correct worktree untouched; never rewrites
  `workspace_path` to fall back to the source repository.
- `verify_workspace(spec)` — the mandatory, fail-closed gate. Proves every
  invariant in `docs`/the task brief and raises `WorkspaceVerificationError`
  (a *structured* exception: expected/actual workspace, expected/actual
  branch, the exact failed step, and a remediation hint) on the first failure.
  It returns `VerificationEvidence` only when *every* check passes — that is
  the single source of truth for when "Workspace Verified" may be emitted.

`provision_and_verify(spec)` runs both in order.

`remove_workspace(workspace_path, repository_path)` is the lifecycle's other
half (VOYN-W0-AICC-ISOLATED-WORKTREE-PER-ATTEMPT): best-effort `git worktree
remove` for a worktree this module (or an equivalent provisioner using the
same convention) provisioned, gated by `is_pipeline_owned_worktree` so it can
never remove a human's primary working tree, a different repository, or a
worktree it cannot prove it owns.

`prune_repository(repository_path)` closes the gap `remove_workspace`'s
paired prune does not cover: a worker crash between `provision_workspace`
and any cleanup attempt, or a `remove_workspace` call that returned
`"not_owned"`/`"remove_failed"`, leaves a dangling `.git/worktrees/<name>`
entry that nothing else ever revisits. `command_center.worktree_sweep` calls
this periodically (via `aicc-worktree-prune.timer`) for every repository this
host has configured, rather than adding a standalone reconciler here — the
prune primitive belongs beside `remove_workspace`'s own use of it, the
scheduling loop does not.

Git-write surface: the only mutating git subcommands this module ever runs
are `git worktree add` (from `provision_workspace`, only when the target path
does not already exist) and `git worktree remove` / `git worktree prune`
(from `remove_workspace`, only on a path already proven pipeline-owned, and
from `prune_repository`, scoped to a repository path the caller already
verified is configured on this host). It never runs
`commit`/`push`/`merge`/`reset`/`rebase`/`clean`/`checkout`/branch-delete,
and never touches a worktree it did not itself create or cannot prove it
owns. All other git access is read-only, via `command_center.git_info`.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from hashlib import sha256
from pathlib import Path

from command_center import git_info
from command_center.workspace_authority import decode_workspace_authority_key

_GIT_OPERATION_ERRORS = (OSError, subprocess.SubprocessError)
_MARKER_READ_ERRORS = (OSError, ValueError, TypeError)

# Branch names that denote a repository's main line rather than isolated
# feature/audit work. A task whose expected branch is one of these (or equals
# its own base branch) is treated as main-line work that may legitimately run
# in the primary working tree; anything else must run in an isolated worktree.
MAIN_BRANCH_NAMES = frozenset({"main", "master"})

# Working-tree cleanliness policies a caller can require before launch.
STATUS_POLICY_ALLOW_DIRTY = "allow_dirty"  # default — never blocks on dirtiness
STATUS_POLICY_NO_UNTRACKED = "no_untracked"  # block if there are untracked files
STATUS_POLICY_CLEAN = "clean"  # block on any modified/untracked file
STATUS_POLICIES = frozenset(
    {STATUS_POLICY_ALLOW_DIRTY, STATUS_POLICY_NO_UNTRACKED, STATUS_POLICY_CLEAN}
)
PROVISION_OUTCOMES = frozenset({"skipped", "reused", "attached", "created", "cloned"})

_TASK_LOCAL_MARKER = "aicc-task-workspace.json"
_TASK_CLONE_PARENT_SUFFIX = "-task-clones"

_DETACHED_HEAD = "(detached HEAD)"


@dataclass(frozen=True)
class WorkspaceSpec:
    """Everything provisioning + verification needs to make a launch safe.

    `workspace_path` is where the agent will actually run (the cwd handed to
    `claude`). `repository_path` is the canonical/source repository the
    worktree must belong to (used for the belongs-to-repository check, base-
    branch existence, and provisioning) — when unknown/unset, the checks that
    require it degrade to "skipped/passed" rather than blocking, so non-task
    callers are never forced to invent one. `expected_branch`/`base_branch`
    drive the branch and isolation gates; `status_policy` the cleanliness
    gate; `task_type` is informational metadata carried into the evidence.
    """

    workspace_path: str
    expected_branch: str | None = None
    base_branch: str | None = None
    repository_path: str | None = None
    task_type: str | None = None
    status_policy: str = STATUS_POLICY_ALLOW_DIRTY
    allow_provision: bool = True
    provision_outcome: str = "skipped"
    # Mutating server workers run inside a Codex ``workspace-write`` sandbox.
    # A linked worktree keeps its writable index/refs in the source repo's
    # ``.git/worktrees`` directory, outside that sandbox.  This policy creates
    # an independent clone whose complete Git metadata lives below
    # ``workspace_path``.  Desktop launch paths keep the historical linked
    # worktree default.
    task_local_git_metadata: bool = False
    # Optional trusted pin.  When absent, standalone provisioning resolves the
    # base branch directly against the source repository's canonical remote and
    # persists that exact SHA in its ownership marker.
    base_sha: str | None = None
    # Optional deployment-owned root for standalone task clones.  When set,
    # the exact clone path is still derived from the canonical source
    # repository and branch; callers cannot choose an arbitrary descendant.
    # This lets hardened workers place the complete task-local Git surface
    # below /srv/aicc-workspaces without falling back to linked worktrees.
    task_clone_root: str | None = None


class WorkspaceVerificationError(Exception):
    """Structured, fail-closed verification failure.

    Carries the exact machine-readable fields the task brief requires so a UI
    (or a caller re-raising it) can render every dimension of the failure —
    the expected vs. actual workspace, expected vs. actual branch, the
    verification step that failed, and a concrete remediation suggestion —
    without string-parsing the message."""

    def __init__(
        self,
        *,
        failed_step: str,
        remediation: str,
        expected_workspace: str | None,
        actual_workspace: str | None = None,
        expected_branch: str | None = None,
        actual_branch: str | None = None,
        detail: str = "",
    ) -> None:
        self.failed_step = failed_step
        self.remediation = remediation
        self.expected_workspace = expected_workspace
        self.actual_workspace = actual_workspace
        self.expected_branch = expected_branch
        self.actual_branch = actual_branch
        self.detail = detail
        message = (
            f"Workspace verification failed at step '{failed_step}': {detail}. "
            f"Expected workspace={expected_workspace!r} on branch={expected_branch!r}; "
            f"actual workspace={actual_workspace!r} on branch={actual_branch!r}. "
            f"Remediation: {remediation}"
        )
        super().__init__(message)

    def as_dict(self) -> dict:
        return {
            "failed_step": self.failed_step,
            "expected_workspace": self.expected_workspace,
            "actual_workspace": self.actual_workspace,
            "expected_branch": self.expected_branch,
            "actual_branch": self.actual_branch,
            "remediation": self.remediation,
            "detail": self.detail,
        }


@dataclass
class VerificationEvidence:
    """The positive record of a passed verification — every check that ran and
    its outcome. Persisted (via `as_payload`) as the `workspace_verified`
    lifecycle event so "Workspace Verified" is always backed by auditable
    proof, never an unchecked assumption."""

    workspace_path: str
    repository_path: str | None
    expected_branch: str | None
    actual_branch: str | None
    is_worktree: bool = False
    is_isolated_worktree: bool = False
    provision_outcome: str = "skipped"
    git_dir: str | None = None
    git_common_dir: str | None = None
    base_sha: str | None = None
    start_sha: str | None = None
    remote_task_sha: str | None = None
    remote_url: str | None = None
    workspace_device: int | None = None
    workspace_inode: int | None = None
    checks: list[dict] = field(default_factory=list)

    def record(self, step: str, passed: bool, detail: str = "") -> None:
        self.checks.append({"step": step, "passed": passed, "detail": detail})

    def as_payload(self) -> dict:
        return {
            "workspace_path": self.workspace_path,
            "repository_path": self.repository_path,
            "expected_branch": self.expected_branch,
            "actual_branch": self.actual_branch,
            "is_worktree": self.is_worktree,
            "is_isolated_worktree": self.is_isolated_worktree,
            "provision_outcome": self.provision_outcome,
            "git_dir": self.git_dir,
            "git_common_dir": self.git_common_dir,
            "base_sha": self.base_sha,
            "start_sha": self.start_sha,
            "remote_task_sha": self.remote_task_sha,
            "remote_url": self.remote_url,
            "workspace_device": self.workspace_device,
            "workspace_inode": self.workspace_inode,
            "checks": list(self.checks),
        }


# --------------------------------------------------------------------------
# Small git helpers (read-only unless noted)
# --------------------------------------------------------------------------


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def task_workspace_path(
    repository: str | Path, branch: str, *, clone_root: str | Path | None = None
) -> Path:
    """Collision-resistant standalone workspace path for one task branch."""
    repo = Path(repository).expanduser().resolve()
    normalized = branch.strip().strip("/") or "work"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized).strip("-._") or "work"
    digest = sha256(branch.encode("utf-8")).hexdigest()[:12]
    if clone_root is None:
        parent = repo.parent / f"{repo.name}{_TASK_CLONE_PARENT_SUFFIX}"
    else:
        root = Path(clone_root).expanduser().resolve(strict=True)
        repository_id = sha256(str(repo).encode("utf-8")).hexdigest()[:16]
        repository_slug = re.sub(r"[^A-Za-z0-9._-]", "-", repo.name)
        parent = root / f"{repository_slug}-{repository_id}"
    return parent / f"{slug[:80]}-{digest}"


def _git_rev_parse_path(cwd: Path, flag: str) -> Path | None:
    """Resolved absolute `git rev-parse <flag>` path (e.g. `--git-dir`,
    `--git-common-dir`), or `None` if it cannot be determined."""
    result = git_info.run_git_command(cwd, ["rev-parse", flag])
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None
    candidate = Path(result.stdout.strip())
    if not candidate.is_absolute():
        candidate = cwd / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


def _git_common_dir(cwd: Path) -> Path | None:
    """Identical for every worktree of the same repository — the reliable way
    to tell "a worktree of *this* repo" apart from "some other git repo"."""
    return _git_rev_parse_path(cwd, "--git-common-dir")


def _is_primary_worktree(cwd: Path) -> bool:
    """True if `cwd` is the repository's *primary* working tree (where `.git`
    is a directory), False for a linked worktree (where `--git-dir` points
    into `<common>/worktrees/<name>` and so differs from `--git-common-dir`).
    Fails closed: if it cannot be determined, treats the path as primary so a
    feature/audit task is blocked rather than allowed to run somewhere
    unproven."""
    git_dir = _git_rev_parse_path(cwd, "--git-dir")
    common = _git_common_dir(cwd)
    if git_dir is None or common is None:
        return True
    return git_dir == common


def is_pipeline_owned_worktree(
    workspace: str | Path, repository_path: str | Path | None
) -> bool:
    """True when `workspace` is a *linked* worktree of `repository_path` — i.e. a
    directory this application provisions and owns, not a human's primary
    working tree and not an unrelated repository.

    This is the safety boundary for any automatic repair of a workspace. A
    linked worktree of the configured project repository exists only because the
    launch path created it for one task's feature branch, so tidying it (for
    example stashing leftovers from a previous attempt) cannot touch work a
    person is doing. The primary working tree, or any other repository, is
    never in scope.

    Fails closed on every uncertainty: an unresolvable path, a non-repository, a
    different repository, or an undeterminable worktree kind all return False.
    """
    if not repository_path:
        return False
    try:
        ws = _resolve(workspace)
        repo = _resolve(repository_path)
    except OSError:
        return False
    if not ws.is_dir() or _is_primary_worktree(ws):
        return False
    ws_common = _git_common_dir(ws)
    repo_common = _git_common_dir(repo)
    return (
        ws_common is not None and repo_common is not None and ws_common == repo_common
    )


def _conflicting_worktree(
    repo: Path, branch: str, workspace_resolved: Path
) -> str | None:
    """Path of another worktree of `repo` that already has `branch` checked
    out (a different path than `workspace_resolved`), or `None`."""
    for entry in git_info.get_worktrees(repo):
        if entry.get("branch") != branch:
            continue
        entry_path = entry.get("path")
        if not entry_path:
            continue
        try:
            if _resolve(entry_path) != workspace_resolved:
                return entry_path
        except OSError:
            continue
    return None


def _remote_branch_ref(repo: Path, branch: str) -> str | None:
    """Return the unique local remote-tracking ref for ``branch``.

    Provisioning is intentionally offline: it uses already-fetched refs and
    never performs an implicit network fetch. Ambiguous matches across multiple
    remotes fail closed instead of guessing which history to launch.
    """
    result = git_info.run_git_command(
        repo,
        ["for-each-ref", "--format=%(refname:short)", "refs/remotes"],
    )
    if result is None or result.returncode != 0:
        return None
    suffix = f"/{branch}"
    matches = sorted(
        ref.strip()
        for ref in result.stdout.splitlines()
        if ref.strip().endswith(suffix) and not ref.strip().endswith("/HEAD")
    )
    if len(matches) > 1:
        raise WorkspaceVerificationError(
            failed_step="remote_branch_unambiguous",
            remediation=f"Create a local {branch!r} branch from the intended remote explicitly.",
            expected_workspace=None,
            expected_branch=branch,
            detail=f"multiple remote-tracking branches match: {matches}",
        )
    return matches[0] if matches else None


def is_feature_task(
    expected_branch: str | None, base_branch: str | None, task_type: str | None = None
) -> bool:
    """Whether this launch is isolated feature/audit work (must run in a
    linked worktree, never the primary repository working tree) rather than
    main-line work. True only when there is an expected branch that is neither
    a main-line name nor equal to its own base branch — the conservative rule
    that catches the observed `audit/execution-queue` case without
    misclassifying a legitimate main/master/`base == expected` launch."""
    del task_type  # reserved for future policy; branch semantics decide today
    if not expected_branch:
        return False
    if expected_branch in MAIN_BRANCH_NAMES:
        return False
    return not (base_branch and expected_branch == base_branch)


# --------------------------------------------------------------------------
# Provisioning (the only git-write path: `git worktree add`)
# --------------------------------------------------------------------------


def _run_provision_git(
    argv: list[str],
    *,
    cwd: Path,
    spec: WorkspaceSpec,
    failed_step: str,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *argv],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except _GIT_OPERATION_ERRORS as exc:
        raise WorkspaceVerificationError(
            failed_step=failed_step,
            remediation="Resolve the standalone Git workspace failure and retry the launch.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail=f"git {' '.join(argv[:2])} could not be executed: {exc}",
        ) from exc
    if result.returncode != 0:
        raise WorkspaceVerificationError(
            failed_step=failed_step,
            remediation="Resolve the standalone Git workspace failure and retry the launch.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail=(result.stderr or result.stdout).strip()
            or f"git {' '.join(argv[:2])} failed",
        )
    return result


def _source_remote_url(repo: Path, spec: WorkspaceSpec) -> str:
    remote = git_info.run_git_command(repo, ["remote", "get-url", "origin"])
    if remote is not None and remote.returncode == 0 and remote.stdout.strip():
        value = remote.stdout.strip()
        # Never persist or hand an agent an HTTPS URL containing user-info.
        # Canonical GitHub HTTPS and SSH URLs remain allowed.
        if (
            value.startswith(("http://", "https://"))
            and "@" in value.split("//", 1)[1].split("/", 1)[0]
        ):
            raise WorkspaceVerificationError(
                failed_step="canonical_remote_has_no_embedded_credentials",
                remediation="Replace origin with a credential-free canonical repository URL.",
                expected_workspace=spec.workspace_path,
                expected_branch=spec.expected_branch,
                detail="origin URL contains embedded user-info",
            )
        if (
            not value.startswith(("http://", "https://", "ssh://", "file://", "git@"))
            and ":" not in value.split("/", 1)[0]
        ):
            candidate = Path(value).expanduser()
            if not candidate.is_absolute():
                candidate = repo / candidate
            value = str(candidate.resolve())
        return value
    # Local repositories used by offline/test deployments may have no origin.
    # ``--no-local`` below still creates independent objects and metadata.
    return str(repo)


def _resolve_remote_base_sha(repo: Path, remote_url: str, spec: WorkspaceSpec) -> str:
    if spec.base_sha:
        candidate = spec.base_sha.strip().lower()
        if len(candidate) != 40 or any(
            char not in "0123456789abcdef" for char in candidate
        ):
            raise WorkspaceVerificationError(
                failed_step="base_sha_valid",
                remediation="Provide a full 40-character hexadecimal base SHA.",
                expected_workspace=spec.workspace_path,
                expected_branch=spec.expected_branch,
                detail=f"invalid base_sha {spec.base_sha!r}",
            )
        return candidate
    if not spec.base_branch:
        raise WorkspaceVerificationError(
            failed_step="base_branch_exists",
            remediation="Configure a base branch for the mutating task.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail="standalone provisioning requires base_branch",
        )
    remote = _run_provision_git(
        ["ls-remote", "--exit-code", remote_url, f"refs/heads/{spec.base_branch}"],
        cwd=repo,
        spec=spec,
        failed_step="resolve_remote_base_sha",
    )
    lines = [line.split() for line in remote.stdout.splitlines() if line.strip()]
    if len(lines) != 1 or len(lines[0]) != 2:
        raise WorkspaceVerificationError(
            failed_step="resolve_remote_base_sha",
            remediation="Make the canonical remote base branch resolve to one exact commit.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail=f"ambiguous base response for {spec.base_branch!r}",
        )
    candidate = lines[0][0].lower()
    if len(candidate) != 40 or any(
        char not in "0123456789abcdef" for char in candidate
    ):
        raise WorkspaceVerificationError(
            failed_step="resolve_remote_base_sha",
            remediation="Make the canonical remote return a full commit SHA.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail=f"malformed remote SHA {candidate!r}",
        )
    return candidate


def _resolve_remote_ref_sha(
    repo: Path, remote_url: str, ref: str, spec: WorkspaceSpec
) -> str | None:
    result = _run_provision_git(
        ["ls-remote", remote_url, ref],
        cwd=repo,
        spec=spec,
        failed_step="resolve_remote_ref_sha",
    )
    lines = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    if len(lines) != 1 or len(lines[0]) != 2 or lines[0][1] != ref:
        raise WorkspaceVerificationError(
            failed_step="resolve_remote_ref_sha",
            remediation="Make the canonical remote ref resolve unambiguously.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail=f"ambiguous remote response for {ref!r}",
        )
    candidate = lines[0][0].lower()
    if len(candidate) != 40 or any(
        char not in "0123456789abcdef" for char in candidate
    ):
        raise WorkspaceVerificationError(
            failed_step="resolve_remote_ref_sha",
            remediation="Make the canonical remote return a full SHA-1.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail=f"malformed remote SHA {candidate!r}",
        )
    return candidate


def _task_local_marker_path(workspace: Path) -> Path:
    return (
        workspace.parent
        / ".aicc-task-metadata"
        / f"{workspace.name}.{_TASK_LOCAL_MARKER}"
    )


def _workspace_authority_key() -> bytes | None:
    # Retry authority has its own stable cryptographic domain.  Reusing the
    # rotating database credential would invalidate every persisted task
    # checkpoint during routine lease-password rotation and couples a DB
    # secret to an unrelated signing purpose.
    #
    # Reading from the WORKER's own environment is safe against the same-UID
    # agent because the agent process never inherits it:
    # agent_runner.scrub_vcs_credentials() strips
    # AICC_WORKSPACE_AUTHORITY_KEY (with the lease/publish variables) from
    # every launch environment, and the systemd units source the key from a
    # root-owned 0640 EnvironmentFile the agent cannot read (reviewed on
    # 8a881d3: the forgery path claimed there requires env inheritance that
    # the launch path explicitly removes).
    return decode_workspace_authority_key(
        os.environ.get("AICC_WORKSPACE_AUTHORITY_KEY")
    )


def _atomic_write_private(path: Path, payload: bytes) -> None:
    """Replace one private authority file without following any symlink.

    The model process currently shares the worker UID, so both the final name
    and any predictable temporary name are attacker-controlled.  Operate
    relative to a verified directory fd, create a random O_EXCL/O_NOFOLLOW
    file, fsync it, rename it atomically, then fsync the parent so a reported
    checkpoint survives a crash.
    """
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "nt":
        # AICC workers are Linux; retain functional Windows CI/developer
        # support with the strongest primitives Python exposes there.
        file_fd, temporary_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(file_fd, view)
                if written <= 0:
                    raise OSError("private authority write made no progress")
                view = view[written:]
            os.fsync(file_fd)
            os.close(file_fd)
            file_fd = -1
            os.replace(temporary_path, path)
        except BaseException:
            if file_fd >= 0:
                os.close(file_fd)
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
            raise
        return
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    temporary = f".{path.name}.{secrets.token_hex(16)}.tmp"
    file_fd: int | None = None
    try:
        file_fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        view = memoryview(payload)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise OSError("private authority write made no progress")
            view = view[written:]
        os.fchmod(file_fd, 0o600)
        os.fsync(file_fd)
        os.close(file_fd)
        file_fd = None
        os.replace(
            temporary,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        final = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISREG(final.st_mode):
            raise OSError("private authority destination is not a regular file")
        os.fsync(directory_fd)
    except BaseException:
        if file_fd is not None:
            os.close(file_fd)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(directory_fd)


def _read_private_file(path: Path) -> bytes:
    if os.name == "nt":
        return path.read_bytes()
    directory_fd = os.open(
        path.parent,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    file_fd: int | None = None
    try:
        file_fd = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("private authority source is not a regular file")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(file_fd, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory_fd)


def _marker_signature(value: dict) -> str | None:
    key = _workspace_authority_key()
    if key is None:
        return None
    unsigned = {k: v for k, v in value.items() if k != "authority_hmac"}
    payload = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(key, payload, "sha256").hexdigest()


def _read_task_local_marker(workspace: Path) -> dict | None:
    marker = _task_local_marker_path(workspace)
    try:
        value = json.loads(_read_private_file(marker).decode("utf-8"))
    except _MARKER_READ_ERRORS:
        return None
    if not isinstance(value, dict):
        return None
    expected = _marker_signature(value)
    actual = value.get("authority_hmac")
    if (
        expected is None
        or not isinstance(actual, str)
        or not hmac.compare_digest(actual, expected)
    ):
        return None
    return value


def _provision_task_local_clone(
    spec: WorkspaceSpec, repo: Path, workspace: Path
) -> str:
    """Create an independent clone whose entire Git write surface is local.

    The agent receives no remote at all. The guarded publisher later imports
    only content-addressed objects into a fresh trusted clone.
    """
    raw_workspace = Path(spec.workspace_path).expanduser()
    expected_path = task_workspace_path(
        repo, spec.expected_branch or "", clone_root=spec.task_clone_root
    )
    if (
        Path(os.path.abspath(raw_workspace)) != expected_path
        or raw_workspace.is_symlink()
    ):
        raise WorkspaceVerificationError(
            failed_step="task_clone_path_safe",
            remediation="Use task_workspace_path() below the repository task-clone root.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail="task clone path is outside its trusted root or is a symlink",
        )
    if workspace.exists():
        return "reused"
    if not (spec.expected_branch and spec.base_branch):
        return "skipped"

    remote_url = _source_remote_url(repo, spec)
    base_sha = _resolve_remote_base_sha(repo, remote_url, spec)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    if workspace.parent.is_symlink():
        raise WorkspaceVerificationError(
            failed_step="task_clone_parent_safe",
            remediation="Replace the task-clone parent symlink with a real directory.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail="task clone parent must not be a symlink",
        )
    target = workspace
    created = False
    try:
        _run_provision_git(
            [
                "clone",
                "--no-local",
                "--no-checkout",
                "--origin",
                "origin",
                "--config",
                "core.hooksPath=.git/hooks",
                remote_url,
                str(target),
            ],
            cwd=repo,
            spec=spec,
            failed_step="provision_standalone_clone",
            timeout=180,
        )
        created = True
        present = git_info.run_git_command(
            target, ["cat-file", "-e", f"{base_sha}^{{commit}}"]
        )
        if present is None or present.returncode != 0:
            raise WorkspaceVerificationError(
                failed_step="base_sha_present",
                remediation="Fetch the pinned base SHA into the standalone clone and retry.",
                expected_workspace=spec.workspace_path,
                expected_branch=spec.expected_branch,
                detail=f"pinned base SHA {base_sha} is absent from clone",
            )

        remote_branch = git_info.run_git_command(
            target,
            ["rev-parse", "--verify", f"origin/{spec.expected_branch}^{{commit}}"],
        )
        start_ref = (
            f"origin/{spec.expected_branch}"
            if remote_branch is not None and remote_branch.returncode == 0
            else base_sha
        )
        _run_provision_git(
            ["switch", "-c", spec.expected_branch, start_ref],
            cwd=target,
            spec=spec,
            failed_step="checkout_task_branch",
        )
        # A deterministic identity permits local commits without granting any
        # remote identity or credential.
        _run_provision_git(
            ["config", "--local", "user.name", "AICC Task Agent"],
            cwd=target,
            spec=spec,
            failed_step="configure_task_identity",
        )
        _run_provision_git(
            ["config", "--local", "user.email", "aicc-task-agent@localhost"],
            cwd=target,
            spec=spec,
            failed_step="configure_task_identity",
        )
        # Remove the only publish destination before the model process starts.
        _run_provision_git(
            ["remote", "remove", "origin"],
            cwd=target,
            spec=spec,
            failed_step="remove_agent_remote",
        )
        start_sha_result = git_info.run_git_command(target, ["rev-parse", "HEAD"])
        if start_sha_result is None or start_sha_result.returncode != 0:
            raise WorkspaceVerificationError(
                failed_step="resolve_workspace_start_sha",
                remediation="Re-provision the standalone task clone.",
                expected_workspace=spec.workspace_path,
                expected_branch=spec.expected_branch,
                detail="cannot resolve the task branch start SHA",
            )
        start_sha = start_sha_result.stdout.strip()
        trusted_base_result = git_info.run_git_command(
            target, ["merge-base", base_sha, start_sha]
        )
        if trusted_base_result is None or trusted_base_result.returncode != 0:
            raise WorkspaceVerificationError(
                failed_step="resolve_trusted_task_base",
                remediation="Rebase the task branch onto the canonical repository history.",
                expected_workspace=spec.workspace_path,
                expected_branch=spec.expected_branch,
                detail="task branch has no trusted merge-base with the canonical base",
            )
        trusted_base_sha = trusted_base_result.stdout.strip()
        marker = {
            "version": 1,
            "source_repository": str(repo),
            "remote_url": remote_url,
            "expected_branch": spec.expected_branch,
            "base_branch": spec.base_branch,
            "base_sha": trusted_base_sha,
            "start_sha": start_sha,
        }
        signature = _marker_signature(marker)
        if signature is None:
            raise WorkspaceVerificationError(
                failed_step="workspace_authority_key",
                remediation="Configure AICC_WORKSPACE_AUTHORITY_KEY on the worker.",
                expected_workspace=spec.workspace_path,
                expected_branch=spec.expected_branch,
                detail="task-local marker authority is not configured",
            )
        marker["authority_hmac"] = signature
        marker_path = _task_local_marker_path(target)
        _atomic_write_private(
            marker_path,
            (json.dumps(marker, sort_keys=True) + "\n").encode("utf-8"),
        )
        return "cloned"
    except BaseException:
        # The path did not exist before this function and cannot contain user
        # work.  Do not leave an unmarked half-clone that later retries might
        # mistake for a verified workspace.
        if created and target.exists() and not target.is_symlink():
            shutil.rmtree(target, ignore_errors=True)
        raise


def _worktree_add(
    repo: Path, workspace: Path, extra_args: list[str], spec: WorkspaceSpec
) -> None:
    workspace.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            ["git", "worktree", "add", *extra_args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except _GIT_OPERATION_ERRORS as exc:
        raise WorkspaceVerificationError(
            failed_step="provision_worktree",
            remediation="Resolve the git worktree creation failure and retry the launch.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail=f"git worktree add could not be executed: {exc}",
        ) from exc
    if result.returncode != 0:
        raise WorkspaceVerificationError(
            failed_step="provision_worktree",
            remediation="Resolve the git worktree creation failure and retry the launch.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail=(result.stderr or result.stdout).strip()
            or "git worktree add failed",
        )


def provision_workspace(spec: WorkspaceSpec) -> str:
    """Ensure `spec.workspace_path` exists as a worktree. Idempotent and
    safe to call before every launch.

    Returns one of: `"reused"` (path already existed — left untouched, to be
    validated by `verify_workspace`), `"attached"` (created a new worktree for
    an already-existing branch), `"created"` (created a new branch *and*
    worktree from `base_branch`), or `"skipped"` (could not provision — e.g.
    provisioning disabled, or missing repository/branch/base information — in
    which case `verify_workspace` will fail closed on the missing workspace).

    Never rewrites `workspace_path` to the source repository: an unprovisioned
    workspace becomes a hard verification failure, never a silent fallback."""
    workspace = Path(spec.workspace_path).expanduser()
    if not spec.task_local_git_metadata:
        # The legacy contract, unchanged: an already-existing workspace is
        # "reused" REGARDLESS of whether the authority fields are complete
        # (verify_workspace judges it), and disabled provisioning
        # short-circuits before any repository access. Splicing the
        # task-local branch in must not reorder this pre-existing path
        # (independent-review finding on d661d8f).
        if workspace.exists():
            return "reused"
        if not spec.allow_provision:
            return "skipped"
    if not (spec.repository_path and spec.expected_branch and spec.base_branch):
        return "skipped"

    repo = _resolve(spec.repository_path)
    if not git_info.get_status(repo).get("is_repo"):
        return "skipped"

    if spec.task_local_git_metadata:
        if not spec.allow_provision and not workspace.exists():
            return "skipped"
        return _provision_task_local_clone(spec, repo, workspace)

    workspace_target = workspace.resolve()
    branch_exists = spec.expected_branch in git_info.get_branches(repo)
    if branch_exists:
        conflict = _conflicting_worktree(
            repo, spec.expected_branch, _resolve(workspace)
        )
        if conflict is not None:
            raise WorkspaceVerificationError(
                failed_step="no_conflicting_worktree",
                remediation=(
                    f"Branch {spec.expected_branch!r} is already checked out in another worktree "
                    f"({conflict}). Launch against it, or remove that worktree first."
                ),
                expected_workspace=spec.workspace_path,
                expected_branch=spec.expected_branch,
                detail="cannot attach an already-checked-out branch to a second worktree",
            )
        _worktree_add(
            repo, workspace, [str(workspace_target), spec.expected_branch], spec
        )
        return "attached"

    remote_branch = _remote_branch_ref(repo, spec.expected_branch)
    if remote_branch is not None:
        _worktree_add(
            repo,
            workspace,
            [
                "--track",
                "-b",
                spec.expected_branch,
                str(workspace_target),
                remote_branch,
            ],
            spec,
        )
        return "attached"

    verify = git_info.run_git_command(
        repo, ["rev-parse", "--verify", f"{spec.base_branch}^{{commit}}"]
    )
    if verify is None or verify.returncode != 0:
        # Base branch missing — let `verify_workspace` report it as the
        # structured `base_branch_exists` / `workspace_exists` failure rather
        # than raising a less specific error here.
        return "skipped"
    _worktree_add(
        repo,
        workspace,
        ["-b", spec.expected_branch, str(workspace_target), spec.base_branch],
        spec,
    )
    return "created"


# --------------------------------------------------------------------------
# Verification (the mandatory, fail-closed gate)
# --------------------------------------------------------------------------


_FULL_SHA1 = re.compile(r"^[0-9a-f]{40}$")


def _verify_task_local_workspace(spec: WorkspaceSpec) -> VerificationEvidence:
    """Verify a reused agent clone without executing its Git configuration."""
    if not (spec.repository_path and spec.expected_branch and spec.base_branch):
        raise WorkspaceVerificationError(
            failed_step="task_local_authority_complete",
            remediation="Provide repository_path, expected_branch and base_branch.",
            expected_workspace=spec.workspace_path,
            expected_branch=spec.expected_branch,
            detail="task-local authority is incomplete",
        )
    repo = _resolve(spec.repository_path)
    raw = Path(spec.workspace_path).expanduser()
    absolute = Path(os.path.abspath(raw))
    expected_path = task_workspace_path(
        repo, spec.expected_branch, clone_root=spec.task_clone_root
    )
    if absolute != expected_path or raw.is_symlink():
        raise WorkspaceVerificationError(
            failed_step="task_clone_path_safe",
            remediation="Use the deterministic task_workspace_path() clone root.",
            expected_workspace=str(expected_path),
            actual_workspace=str(absolute),
            expected_branch=spec.expected_branch,
            detail="task clone path differs from its trusted path or is a symlink",
        )
    try:
        workspace_stat = absolute.lstat()
    except OSError as exc:
        raise WorkspaceVerificationError(
            failed_step="workspace_exists",
            remediation="Provision the standalone task clone.",
            expected_workspace=str(expected_path),
            actual_workspace=str(absolute),
            expected_branch=spec.expected_branch,
            detail=f"workspace is unavailable: {exc}",
        ) from exc
    if not stat.S_ISDIR(workspace_stat.st_mode):
        raise WorkspaceVerificationError(
            failed_step="workspace_is_directory",
            remediation="Replace the task path with a real standalone clone directory.",
            expected_workspace=str(expected_path),
            actual_workspace=str(absolute),
            expected_branch=spec.expected_branch,
            detail="task clone root is not a real directory",
        )
    marker = _read_task_local_marker(absolute)
    if marker is None:
        raise WorkspaceVerificationError(
            failed_step="task_local_workspace_marker",
            remediation="Preserve the workspace and re-provision from trusted authority.",
            expected_workspace=str(expected_path),
            actual_workspace=str(absolute),
            expected_branch=spec.expected_branch,
            detail="standalone workspace ownership marker is missing or invalid",
        )
    remote_url = _source_remote_url(repo, spec)
    static_expected = {
        "source_repository": str(repo),
        "remote_url": remote_url,
        "expected_branch": spec.expected_branch,
        "base_branch": spec.base_branch,
    }
    static_failed = {
        key: (marker.get(key), expected)
        for key, expected in static_expected.items()
        if marker.get(key) != expected
    }
    marker_base = str(marker.get("base_sha") or "").lower()
    marker_start = str(marker.get("start_sha") or "").lower()
    if (
        static_failed
        or not _FULL_SHA1.fullmatch(marker_base)
        or not _FULL_SHA1.fullmatch(marker_start)
    ):
        raise WorkspaceVerificationError(
            failed_step="workspace_belongs_to_repository",
            remediation="Preserve the clone and recover it through trusted repository authority.",
            expected_workspace=str(expected_path),
            actual_workspace=str(absolute),
            expected_branch=spec.expected_branch,
            detail=f"task-local ownership mismatch: {static_failed or 'invalid base SHA'}",
        )
    candidate = _read_agent_head(absolute, spec.expected_branch)
    if candidate != marker_start:
        raise WorkspaceVerificationError(
            failed_step="task_workspace_checkpoint",
            remediation="Recover the uncheckpointed branch through trusted operator review.",
            expected_workspace=str(expected_path),
            actual_workspace=str(absolute),
            expected_branch=spec.expected_branch,
            detail=f"saved HEAD {candidate} differs from signed checkpoint {marker_start}",
        )
    current_base = _resolve_remote_ref_sha(
        repo, remote_url, f"refs/heads/{spec.base_branch}", spec
    )
    if current_base is None:
        raise WorkspaceVerificationError(
            failed_step="remote_base_exists",
            remediation=f"Restore remote base branch {spec.base_branch!r}.",
            expected_workspace=str(expected_path),
            actual_workspace=str(absolute),
            expected_branch=spec.expected_branch,
            detail="canonical remote base branch is missing",
        )
    remote_task = _resolve_remote_ref_sha(
        repo, remote_url, f"refs/heads/{spec.expected_branch}", spec
    )
    # This disposable clone proves the marker's original base is a real
    # ancestor of today's canonical base and that the saved candidate is a
    # clean descendant. It never reads agent-controlled Git config.
    with trusted_publish_clone(
        absolute,
        expected_branch=spec.expected_branch,
        remote_url=remote_url,
        start_sha=marker_base,
        trusted_base_sha=marker_base,
        current_base_sha=current_base,
        expected_remote_sha=remote_task,
        expected_inode=(workspace_stat.st_dev, workspace_stat.st_ino),
        require_clean=spec.status_policy != STATUS_POLICY_ALLOW_DIRTY,
    ):
        pass
    evidence = VerificationEvidence(
        workspace_path=str(absolute),
        repository_path=str(repo),
        expected_branch=spec.expected_branch,
        actual_branch=spec.expected_branch,
        is_worktree=True,
        is_isolated_worktree=True,
        provision_outcome=spec.provision_outcome,
        git_dir=str(absolute / ".git"),
        git_common_dir=str(absolute / ".git"),
        base_sha=marker_base,
        start_sha=candidate,
        remote_task_sha=remote_task,
        remote_url=remote_url,
        workspace_device=workspace_stat.st_dev,
        workspace_inode=workspace_stat.st_ino,
    )
    for step, detail in (
        ("workspace_exists", str(absolute)),
        ("task_local_git_metadata", str(absolute / ".git")),
        ("workspace_belongs_to_repository", str(repo)),
        ("branch_matches", spec.expected_branch),
        ("isolated_worktree_required", "is_isolated=True"),
        ("base_branch_exists", current_base),
        ("status_policy_satisfied", f"policy={spec.status_policy}"),
    ):
        evidence.record(step, True, detail)
    return evidence


def verify_workspace(spec: WorkspaceSpec) -> VerificationEvidence:
    """Prove every launch invariant or raise `WorkspaceVerificationError`.

    Returns `VerificationEvidence` only when *all* checks pass — this return
    is the sole authorization for emitting "Workspace Verified". Never mutates
    anything (read-only git + filesystem stat)."""
    if spec.status_policy not in STATUS_POLICIES:
        raise ValueError(f"Unknown status_policy: {spec.status_policy!r}")
    if spec.provision_outcome not in PROVISION_OUTCOMES:
        raise ValueError(f"Unknown provision_outcome: {spec.provision_outcome!r}")
    if spec.task_local_git_metadata:
        return _verify_task_local_workspace(spec)

    workspace_input = spec.workspace_path
    workspace_resolved = _resolve(workspace_input)
    repo_resolved = _resolve(spec.repository_path) if spec.repository_path else None

    evidence = VerificationEvidence(
        workspace_path=str(workspace_resolved),
        repository_path=str(repo_resolved) if repo_resolved else None,
        expected_branch=spec.expected_branch,
        actual_branch=None,
    )

    def fail(
        step: str, remediation: str, detail: str, actual_branch: str | None = None
    ) -> None:
        raise WorkspaceVerificationError(
            failed_step=step,
            remediation=remediation,
            expected_workspace=workspace_input,
            actual_workspace=str(workspace_resolved),
            expected_branch=spec.expected_branch,
            actual_branch=actual_branch,
            detail=detail,
        )

    evidence.provision_outcome = spec.provision_outcome

    # 1. Workspace exists.
    if (
        not Path(workspace_input).expanduser().exists()
        or not workspace_resolved.is_dir()
    ):
        fail(
            "workspace_exists",
            "Provision the worktree (git worktree add) or fix the task's workspace_path.",
            f"workspace path does not exist or is not a directory: {workspace_input}",
        )
    evidence.record("workspace_exists", True, str(workspace_resolved))

    # 2. Workspace is a git worktree.
    status = git_info.get_status(workspace_resolved)
    if not status.get("is_repo"):
        fail(
            "workspace_is_git_worktree",
            "Point workspace_path at a real git worktree, or provision one from base_branch.",
            f"{workspace_input} is not inside a git work tree",
        )
    evidence.is_worktree = True
    evidence.record("workspace_is_git_worktree", True)

    actual_branch = status.get("branch")
    evidence.actual_branch = actual_branch

    git_dir = _git_rev_parse_path(workspace_resolved, "--git-dir")
    git_common_dir = _git_common_dir(workspace_resolved)
    evidence.git_dir = str(git_dir) if git_dir else None
    evidence.git_common_dir = str(git_common_dir) if git_common_dir else None

    # 3. Workspace belongs to the expected repository.
    if repo_resolved is not None and git_info.get_status(repo_resolved).get("is_repo"):
        workspace_common = _git_common_dir(workspace_resolved)
        repo_common = _git_common_dir(repo_resolved)
        if (
            workspace_common is None
            or repo_common is None
            or workspace_common != repo_common
        ):
            fail(
                "workspace_belongs_to_repository",
                "Use a worktree of the configured repository, not a different repository.",
                f"workspace belongs to a different repository than {spec.repository_path}",
                actual_branch=actual_branch,
            )
        evidence.record("workspace_belongs_to_repository", True, str(workspace_common))
    else:
        evidence.record(
            "workspace_belongs_to_repository",
            True,
            "source repository not provided; skipped",
        )

    # 4. Current branch equals the expected branch.
    if spec.expected_branch:
        if actual_branch == _DETACHED_HEAD:
            fail(
                "branch_matches",
                f"Check out {spec.expected_branch!r} in the worktree before launching.",
                "workspace is in detached HEAD state, no branch checked out",
                actual_branch=actual_branch,
            )
        if actual_branch != spec.expected_branch:
            fail(
                "branch_matches",
                (
                    f"Launch against the worktree that has {spec.expected_branch!r} checked out, "
                    "or switch this workspace to that branch. Do not run in the main repository."
                ),
                f"workspace is on branch {actual_branch!r}, expected {spec.expected_branch!r}",
                actual_branch=actual_branch,
            )
    evidence.record("branch_matches", True, f"branch={actual_branch}")

    # 5. Expected branch is not checked out in a conflicting worktree.
    if spec.expected_branch and repo_resolved is not None:
        conflict = _conflicting_worktree(
            repo_resolved, spec.expected_branch, workspace_resolved
        )
        if conflict is not None:
            fail(
                "no_conflicting_worktree",
                "Remove or relocate the other worktree, or launch against it instead.",
                f"branch {spec.expected_branch!r} is already checked out in another worktree: {conflict}",
                actual_branch=actual_branch,
            )
    evidence.record("no_conflicting_worktree", True)

    # 6. The main repository is not used for a feature/audit task.
    feature = is_feature_task(spec.expected_branch, spec.base_branch, spec.task_type)
    is_primary = _is_primary_worktree(workspace_resolved)
    isolated = not is_primary
    evidence.is_isolated_worktree = isolated
    if feature and not isolated:
        fail(
            "isolated_worktree_required",
            (
                "Provision an isolated worktree for this branch (git worktree add) instead of "
                "running the task in the primary repository working tree."
            ),
            (
                f"feature/audit branch {spec.expected_branch!r} must run in an isolated linked "
                "worktree, not the primary repository working tree"
            ),
            actual_branch=actual_branch,
        )
    evidence.record("isolated_worktree_required", True, f"is_isolated={isolated}")

    # 7. Base branch exists (when one is configured).
    if spec.base_branch and repo_resolved is not None:
        base_ok = git_info.run_git_command(
            repo_resolved, ["rev-parse", "--verify", f"{spec.base_branch}^{{commit}}"]
        )
        if base_ok is None or base_ok.returncode != 0:
            fail(
                "base_branch_exists",
                f"Create or correct base_branch {spec.base_branch!r} in the repository.",
                f"base branch not found in repository: {spec.base_branch}",
                actual_branch=actual_branch,
            )
    evidence.record("base_branch_exists", True)

    # 8. Working-tree status satisfies the configured policy.
    _check_status_policy(spec, status, fail)
    evidence.record("status_policy_satisfied", True, f"policy={spec.status_policy}")

    return evidence


def _check_status_policy(spec: WorkspaceSpec, status: dict, fail) -> None:
    if spec.status_policy == STATUS_POLICY_ALLOW_DIRTY:
        return
    untracked = int(status.get("untracked_count", 0) or 0)
    modified = int(status.get("modified_count", 0) or 0)
    if spec.status_policy == STATUS_POLICY_NO_UNTRACKED and untracked > 0:
        fail(
            "status_policy_satisfied",
            "Commit, remove, or ignore the untracked files, or relax the status policy.",
            f"workspace has {untracked} untracked file(s) (policy={STATUS_POLICY_NO_UNTRACKED})",
            actual_branch=status.get("branch"),
        )
    if spec.status_policy == STATUS_POLICY_CLEAN and (untracked > 0 or modified > 0):
        fail(
            "status_policy_satisfied",
            "Commit or stash all changes so the worktree is clean, or relax the status policy.",
            f"workspace not clean: {modified} modified, {untracked} untracked (policy={STATUS_POLICY_CLEAN})",
            actual_branch=status.get("branch"),
        )


def provision_and_verify(spec: WorkspaceSpec) -> VerificationEvidence:
    """Provision (if needed) then verify. The single call task-launch
    orchestration uses to guarantee a safe workspace before touching any task
    state or spawning any process."""
    outcome = provision_workspace(spec)
    evidence = verify_workspace(replace(spec, provision_outcome=outcome))
    evidence.provision_outcome = outcome
    return evidence


def _read_agent_head(workspace: Path, expected_branch: str) -> str:
    """Resolve HEAD without invoking Git against agent-controlled config."""
    git_dir = workspace / ".git"
    head_path = git_dir / "HEAD"
    try:
        git_info_stat = git_dir.lstat()
        head_stat = head_path.lstat()
        head_text = head_path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise WorkspaceVerificationError(
            failed_step="agent_head_readable",
            remediation="Preserve the workspace for inspection and retry on a clean clone.",
            expected_workspace=str(workspace),
            actual_workspace=str(workspace),
            detail=f"cannot read task-local HEAD safely: {exc}",
        ) from exc
    if not stat.S_ISDIR(git_info_stat.st_mode) or not stat.S_ISREG(head_stat.st_mode):
        raise WorkspaceVerificationError(
            failed_step="agent_head_regular",
            remediation="Preserve the workspace for inspection and retry on a clean clone.",
            expected_workspace=str(workspace),
            actual_workspace=str(workspace),
            detail=".git must be a directory and HEAD a regular file",
        )
    expected_ref = f"refs/heads/{expected_branch}"
    if any(
        part in {"", ".", ".."} for part in expected_branch.split("/")
    ):
        raise WorkspaceVerificationError(
            failed_step="agent_head_branch",
            remediation="Use a branch name without empty or dot path components.",
            expected_workspace=str(workspace),
            actual_workspace=str(workspace),
            expected_branch=expected_branch,
            detail="branch name would escape .git/refs/heads",
        )
    if head_text != f"ref: {expected_ref}":
        raise WorkspaceVerificationError(
            failed_step="agent_head_branch",
            remediation="Keep the task on its exact expected branch.",
            expected_workspace=str(workspace),
            actual_workspace=str(workspace),
            expected_branch=expected_branch,
            detail=f"unexpected HEAD contents: {head_text!r}",
        )
    ref_path = git_dir.joinpath(*expected_ref.split("/"))
    candidate: str | None = None
    try:
        if ref_path.exists():
            if not stat.S_ISREG(ref_path.lstat().st_mode):
                raise OSError("branch ref is not a regular file")
            candidate = ref_path.read_text(encoding="ascii").strip()
        else:
            packed_path = git_dir / "packed-refs"
            if packed_path.exists() and stat.S_ISREG(packed_path.lstat().st_mode):
                for line in packed_path.read_text(encoding="ascii").splitlines():
                    if not line or line.startswith(("#", "^")):
                        continue
                    sha, _, ref = line.partition(" ")
                    if ref == expected_ref:
                        candidate = sha
                        break
    except OSError as exc:
        raise WorkspaceVerificationError(
            failed_step="agent_head_ref",
            remediation="Preserve the workspace for inspection and retry on a clean clone.",
            expected_workspace=str(workspace),
            actual_workspace=str(workspace),
            expected_branch=expected_branch,
            detail=f"cannot read branch ref safely: {exc}",
        ) from exc
    if (
        candidate is None
        or len(candidate) != 40
        or any(char not in "0123456789abcdefABCDEF" for char in candidate)
    ):
        raise WorkspaceVerificationError(
            failed_step="agent_head_sha",
            remediation="Preserve the workspace for inspection and retry on a clean clone.",
            expected_workspace=str(workspace),
            actual_workspace=str(workspace),
            expected_branch=expected_branch,
            detail="branch ref does not contain one full SHA-1",
        )
    return candidate.lower()


_LOOSE_OBJECT = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{38}$")
_PACK_OBJECT = re.compile(r"^pack/pack-[0-9a-f]{40}\.(?:pack|idx|rev|bitmap)$")
_MAX_OBJECT_TRANSFER_BYTES = 1_073_741_824


def _copy_agent_objects(workspace: Path, publisher: Path) -> None:
    """Copy only content-addressed object files, never config/hooks/remotes."""
    source = workspace / ".git" / "objects"
    destination = publisher / ".git" / "objects"
    try:
        if not stat.S_ISDIR(source.lstat().st_mode):
            raise OSError("objects is not a directory")
    except OSError as exc:
        raise WorkspaceVerificationError(
            failed_step="agent_objects_safe",
            remediation="Preserve the task clone and retry after operator inspection.",
            expected_workspace=str(workspace),
            actual_workspace=str(workspace),
            detail=f"cannot inspect agent object database: {exc}",
        ) from exc
    total = 0
    for root, directories, files in os.walk(source, followlinks=False):
        root_path = Path(root)
        for directory in list(directories):
            child = root_path / directory
            if child.is_symlink():
                raise WorkspaceVerificationError(
                    failed_step="agent_objects_safe",
                    remediation="Remove symlinks from the task object database.",
                    expected_workspace=str(workspace),
                    actual_workspace=str(workspace),
                    detail=f"symlinked object directory: {child.relative_to(source)}",
                )
        for filename in files:
            candidate = root_path / filename
            relative = candidate.relative_to(source).as_posix()
            if not (
                _LOOSE_OBJECT.fullmatch(relative) or _PACK_OBJECT.fullmatch(relative)
            ):
                continue
            try:
                descriptor = os.open(
                    candidate,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                )
                info = os.fstat(descriptor)
            except OSError as exc:
                raise WorkspaceVerificationError(
                    failed_step="agent_objects_safe",
                    remediation="Preserve the task clone and retry after operator inspection.",
                    expected_workspace=str(workspace),
                    actual_workspace=str(workspace),
                    detail=f"cannot stat object {relative}: {exc}",
                ) from exc
            if not stat.S_ISREG(info.st_mode):
                os.close(descriptor)
                raise WorkspaceVerificationError(
                    failed_step="agent_objects_safe",
                    remediation="Only regular content-addressed Git objects may cross the boundary.",
                    expected_workspace=str(workspace),
                    actual_workspace=str(workspace),
                    detail=f"non-regular object: {relative}",
                )
            try:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with os.fdopen(descriptor, "rb", closefd=False) as source_file:
                    if target.exists():
                        target_file = target.open("rb")
                        writing = False
                    else:
                        target_file = target.open("xb")
                        writing = True
                    with target_file:
                        copied = 0
                        while True:
                            chunk = source_file.read(1024 * 1024)
                            if not chunk:
                                break
                            copied += len(chunk)
                            total += len(chunk)
                            if total > _MAX_OBJECT_TRANSFER_BYTES:
                                raise WorkspaceVerificationError(
                                    failed_step="agent_objects_bounded",
                                    remediation="Split the task or inspect the oversized object transfer.",
                                    expected_workspace=str(workspace),
                                    actual_workspace=str(workspace),
                                    detail="agent object transfer exceeds 1 GiB",
                                )
                            if writing:
                                target_file.write(chunk)
                            elif target_file.read(len(chunk)) != chunk:
                                raise WorkspaceVerificationError(
                                    failed_step="agent_object_collision",
                                    remediation="Preserve both repositories for object-integrity inspection.",
                                    expected_workspace=str(workspace),
                                    actual_workspace=str(workspace),
                                    detail=f"object collision for {relative}",
                                )
                        if copied != info.st_size or (
                            not writing and target_file.read(1)
                        ):
                            raise OSError("object size changed during transfer")
            finally:
                os.close(descriptor)


@contextmanager
def trusted_publish_clone(
    workspace_path: str | Path,
    *,
    expected_branch: str,
    remote_url: str,
    start_sha: str,
    trusted_base_sha: str | None = None,
    current_base_sha: str | None = None,
    expected_remote_sha: str | None = None,
    expected_inode: tuple[int, int] | None = None,
    expected_candidate_sha: str | None = None,
    require_clean: bool = True,
):
    """Yield a fresh publisher-owned clone containing only verified objects.

    No Git command is ever executed with the agent-controlled ``.git`` as its
    repository.  Config, hooks, remotes and credential helpers therefore do
    not cross into the process that owns publish credentials.
    """
    raw_workspace = Path(workspace_path).expanduser()
    if raw_workspace.is_symlink():
        raise WorkspaceVerificationError(
            failed_step="agent_workspace_safe",
            remediation="Preserve the path and re-provision a real task clone.",
            expected_workspace=str(raw_workspace),
            actual_workspace=str(raw_workspace),
            expected_branch=expected_branch,
            detail="agent workspace must not be a symlink",
        )
    workspace = raw_workspace.resolve()
    try:
        workspace_stat = raw_workspace.lstat()
    except OSError as exc:
        raise WorkspaceVerificationError(
            failed_step="agent_workspace_safe",
            remediation="Preserve the path and re-provision a real task clone.",
            expected_workspace=str(raw_workspace),
            actual_workspace=str(raw_workspace),
            expected_branch=expected_branch,
            detail=f"cannot inspect agent workspace: {exc}",
        ) from exc
    actual_inode = (workspace_stat.st_dev, workspace_stat.st_ino)
    if not stat.S_ISDIR(workspace_stat.st_mode) or (
        expected_inode is not None and actual_inode != expected_inode
    ):
        raise WorkspaceVerificationError(
            failed_step="agent_workspace_identity",
            remediation="Preserve the substituted path and recover the verified clone.",
            expected_workspace=str(raw_workspace),
            actual_workspace=str(raw_workspace),
            expected_branch=expected_branch,
            detail=f"workspace inode mismatch: expected={expected_inode}, actual={actual_inode}",
        )
    candidate_sha = _read_agent_head(workspace, expected_branch)
    if expected_candidate_sha is not None and candidate_sha != expected_candidate_sha:
        raise WorkspaceVerificationError(
            failed_step="agent_candidate_changed_before_validation",
            remediation="Stop the remaining writer and retry from a stable task clone.",
            expected_workspace=str(raw_workspace),
            actual_workspace=str(raw_workspace),
            expected_branch=expected_branch,
            detail=(
                f"candidate changed: expected={expected_candidate_sha}, "
                f"actual={candidate_sha}"
            ),
        )
    with tempfile.TemporaryDirectory(prefix="aicc-trusted-publisher-") as raw:
        publisher = Path(raw) / "repo"
        spec = WorkspaceSpec(
            workspace_path=str(publisher), expected_branch=expected_branch
        )
        _run_provision_git(
            [
                "clone",
                "--no-checkout",
                "--origin",
                "origin",
                remote_url,
                str(publisher),
            ],
            cwd=Path(raw),
            spec=spec,
            failed_step="provision_trusted_publisher_clone",
            timeout=180,
        )
        if trusted_base_sha is not None:
            _run_provision_git(
                ["cat-file", "-e", f"{trusted_base_sha}^{{commit}}"],
                cwd=publisher,
                spec=spec,
                failed_step="trusted_base_present",
            )
        if current_base_sha is not None:
            _run_provision_git(
                ["cat-file", "-e", f"{current_base_sha}^{{commit}}"],
                cwd=publisher,
                spec=spec,
                failed_step="current_base_present",
            )
        if trusted_base_sha is not None and current_base_sha is not None:
            _run_provision_git(
                ["merge-base", "--is-ancestor", trusted_base_sha, current_base_sha],
                cwd=publisher,
                spec=spec,
                failed_step="trusted_base_ancestry",
            )
        if expected_remote_sha is not None:
            _run_provision_git(
                ["cat-file", "-e", f"{expected_remote_sha}^{{commit}}"],
                cwd=publisher,
                spec=spec,
                failed_step="expected_remote_present",
            )
        _copy_agent_objects(workspace, publisher)
        checks = [
            (["cat-file", "-e", f"{candidate_sha}^{{commit}}"], "agent_commit_valid"),
            (
                ["merge-base", "--is-ancestor", start_sha, candidate_sha],
                "agent_commit_ancestry",
            ),
            (
                ["fsck", "--strict", "--no-reflogs", candidate_sha],
                "agent_objects_valid",
            ),
        ]
        if trusted_base_sha is not None:
            checks.append(
                (
                    ["merge-base", "--is-ancestor", trusted_base_sha, candidate_sha],
                    "candidate_base_ancestry",
                )
            )
        if expected_remote_sha is not None:
            checks.append(
                (
                    ["merge-base", "--is-ancestor", expected_remote_sha, candidate_sha],
                    "candidate_remote_ancestry",
                )
            )
        for argv, step in checks:
            _run_provision_git(argv, cwd=publisher, spec=spec, failed_step=step)
        _run_provision_git(
            ["switch", "-c", expected_branch, candidate_sha],
            cwd=publisher,
            spec=spec,
            failed_step="checkout_trusted_candidate",
        )
        safe_env = dict(os.environ)
        safe_env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        clean = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                f"--git-dir={publisher / '.git'}",
                f"--work-tree={workspace}",
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            cwd=publisher,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            env=safe_env,
        )
        if clean.returncode != 0 or (require_clean and clean.stdout.strip()):
            detail = (
                f"uncommitted_changes: {clean.stdout.strip()}"
                if clean.stdout.strip()
                else clean.stderr.strip() or "cannot verify agent worktree status"
            )
            raise WorkspaceVerificationError(
                failed_step="agent_worktree_clean",
                remediation="Commit all task changes before guarded publish; preserve the clone.",
                expected_workspace=str(workspace),
                actual_workspace=str(workspace),
                expected_branch=expected_branch,
                detail=detail,
            )
        yield publisher


def task_workspace_candidate_sha(
    workspace_path: str | Path,
    *,
    expected_branch: str,
    expected_inode: tuple[int, int],
) -> str:
    """Read one exact candidate from the task clone without invoking Git."""
    raw_workspace = Path(workspace_path).expanduser()
    if raw_workspace.is_symlink():
        raise WorkspaceVerificationError(
            failed_step="agent_workspace_safe",
            remediation="Preserve the path and re-provision a real task clone.",
            expected_workspace=str(raw_workspace),
            actual_workspace=str(raw_workspace),
            expected_branch=expected_branch,
            detail="agent workspace must not be a symlink",
        )
    try:
        workspace_stat = raw_workspace.lstat()
    except OSError as exc:
        raise WorkspaceVerificationError(
            failed_step="agent_workspace_safe",
            remediation="Preserve the path and re-provision a real task clone.",
            expected_workspace=str(raw_workspace),
            actual_workspace=str(raw_workspace),
            expected_branch=expected_branch,
            detail=f"cannot inspect agent workspace: {exc}",
        ) from exc
    actual_inode = (workspace_stat.st_dev, workspace_stat.st_ino)
    if not stat.S_ISDIR(workspace_stat.st_mode) or actual_inode != expected_inode:
        raise WorkspaceVerificationError(
            failed_step="agent_workspace_identity",
            remediation="Preserve the substituted path and recover the verified clone.",
            expected_workspace=str(raw_workspace),
            actual_workspace=str(raw_workspace),
            expected_branch=expected_branch,
            detail=(
                f"workspace inode mismatch: expected={expected_inode}, "
                f"actual={actual_inode}"
            ),
        )
    return _read_agent_head(raw_workspace.resolve(), expected_branch)


def task_workspace_is_unchanged(
    workspace_path: str | Path,
    *,
    expected_branch: str,
    remote_url: str,
    start_sha: str,
    trusted_base_sha: str,
    expected_remote_sha: str | None,
    expected_inode: tuple[int, int],
) -> bool:
    """Prove a failed executor made no filesystem or commit change."""
    try:
        if _read_agent_head(Path(workspace_path), expected_branch) != start_sha:
            return False
        with trusted_publish_clone(
            workspace_path,
            expected_branch=expected_branch,
            remote_url=remote_url,
            start_sha=start_sha,
            trusted_base_sha=trusted_base_sha,
            expected_remote_sha=expected_remote_sha,
            expected_inode=expected_inode,
        ):
            pass
    except WorkspaceVerificationError:
        return False
    return True


def checkpoint_task_workspace(
    workspace_path: str | Path,
    *,
    expected_branch: str,
    previous_start_sha: str,
    expected_candidate_sha: str,
    expected_inode: tuple[int, int],
) -> str:
    """Atomically advance signed retry authority after trusted validation."""
    workspace = Path(os.path.abspath(Path(workspace_path).expanduser()))
    info = workspace.lstat()
    actual_inode = (info.st_dev, info.st_ino)
    if workspace.is_symlink() or actual_inode != expected_inode:
        raise WorkspaceVerificationError(
            failed_step="task_workspace_checkpoint_identity",
            remediation="Preserve the substituted path for operator inspection.",
            expected_workspace=str(workspace),
            actual_workspace=str(workspace),
            expected_branch=expected_branch,
            detail=f"workspace inode mismatch: {actual_inode}",
        )
    marker = _read_task_local_marker(workspace)
    if marker is None or marker.get("start_sha") != previous_start_sha:
        raise WorkspaceVerificationError(
            failed_step="task_workspace_checkpoint_authority",
            remediation="Preserve the clone; its signed authority changed during execution.",
            expected_workspace=str(workspace),
            actual_workspace=str(workspace),
            expected_branch=expected_branch,
            detail="signed marker is missing or no longer matches the pre-agent checkpoint",
        )
    candidate = _read_agent_head(workspace, expected_branch)
    if candidate != expected_candidate_sha:
        raise WorkspaceVerificationError(
            failed_step="task_workspace_checkpoint_candidate",
            remediation="Stop the remaining writer and retry from a stable task clone.",
            expected_workspace=str(workspace),
            actual_workspace=str(workspace),
            expected_branch=expected_branch,
            detail=(
                f"candidate changed after validation: "
                f"expected={expected_candidate_sha}, actual={candidate}"
            ),
        )
    updated = dict(marker)
    updated["start_sha"] = candidate
    signature = _marker_signature(updated)
    if signature is None:
        raise WorkspaceVerificationError(
            failed_step="workspace_authority_key",
            remediation="Configure AICC_WORKSPACE_AUTHORITY_KEY on the worker.",
            expected_workspace=str(workspace),
            actual_workspace=str(workspace),
            expected_branch=expected_branch,
            detail="task-local marker authority is not configured",
        )
    updated["authority_hmac"] = signature
    marker_path = _task_local_marker_path(workspace)
    _atomic_write_private(
        marker_path,
        (json.dumps(updated, sort_keys=True) + "\n").encode("utf-8"),
    )
    return candidate


def _is_pipeline_owned_standalone_clone(
    workspace: Path,
    repository_path: str | Path,
    verified_inode: tuple[int, int] | None,
) -> bool:
    repo = _resolve(repository_path)
    if verified_inode is None:
        return False
    if (
        workspace.is_symlink()
        or workspace.parent != repo.parent / f"{repo.name}{_TASK_CLONE_PARENT_SUFFIX}"
    ):
        return False
    git_dir = workspace / ".git"
    try:
        root_stat = workspace.lstat()
        return (
            stat.S_ISDIR(root_stat.st_mode)
            and (root_stat.st_dev, root_stat.st_ino) == verified_inode
            and stat.S_ISDIR(git_dir.lstat().st_mode)
            and not git_dir.is_symlink()
        )
    except OSError:
        return False


# --------------------------------------------------------------------------
# Teardown (the other mutating git subcommands: `worktree remove` / `prune`)
# --------------------------------------------------------------------------


def remove_workspace(
    workspace_path: str | Path,
    repository_path: str | Path,
    *,
    verified_clean: bool = False,
    verified_inode: tuple[int | None, int | None] | None = None,
) -> str:
    """Best-effort teardown of a worktree this module (or a caller using the
    same convention) provisioned. Never raises: this runs after a task's
    outcome is already decided, so a cleanup failure must turn into a log
    line for the next sweep, never into a retried or lost report.

    Scoped by `is_pipeline_owned_worktree` — it removes only a linked
    worktree of `repository_path`, never the primary working tree, a
    different repository, or a path it cannot prove it owns — so a caller may
    invoke this unconditionally on every attempt without risking a directory
    it did not create.

    Returns one of:
    - `"removed"` — the worktree and its `.git/worktrees/<name>` metadata are
      gone.
    - `"not_found"` — nothing was there to remove (already cleaned by a prior
      attempt, or provisioning never got far enough to create it).
    - `"not_owned"` — fails closed rather than delete something unproven.
    - `"remove_failed"` — `git worktree remove` refused, most commonly a
      dirty tree (uncommitted or untracked leftovers). Deliberately not
      retried with `--force`: force-removing would destroy whatever an agent
      or an operator might still want to inspect after a failure. The path is
      simply left in place and reused (`"reused"`) by the next
      `provision_workspace` call for the same branch.
    """
    raw_workspace = Path(os.path.abspath(Path(workspace_path).expanduser()))
    if raw_workspace.is_symlink():
        return "not_owned"
    if not raw_workspace.exists():
        return "not_found"
    inode = (
        (int(verified_inode[0]), int(verified_inode[1]))
        if verified_inode is not None and None not in verified_inode
        else None
    )
    if _is_pipeline_owned_standalone_clone(raw_workspace, repository_path, inode):
        # Never execute Git against agent-controlled config during teardown.
        # The caller may set this only after the trusted publisher proved the
        # candidate clean and durable, or when the executor never started.
        if not verified_clean:
            return "remove_failed"
        quarantine_root = raw_workspace.parent / ".aicc-quarantine"
        # An agent-writable parent means .aicc-quarantine could be a symlink
        # to an attacker directory; mkdir(exist_ok=True) would follow it and
        # os.replace would move the (credential-bearing) clone there with
        # this privileged process's rights. Reject any non-directory /
        # symlink and pin the mode explicitly (review finding on 6218a21).
        try:
            quarantine_root.mkdir(mode=0o700, exist_ok=True)
        except OSError:
            return "remove_failed"
        root_stat = quarantine_root.lstat()
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            return "remove_failed"
        if stat.S_IMODE(root_stat.st_mode) != 0o700:
            os.chmod(quarantine_root, 0o700)
        quarantine = (
            quarantine_root / f"{raw_workspace.name}.{os.getpid()}.{time.time_ns()}"
        )
        try:
            os.replace(raw_workspace, quarantine)
            quarantine_stat = quarantine.lstat()
            if (quarantine_stat.st_dev, quarantine_stat.st_ino) != inode:
                return "remove_failed"
            _task_local_marker_path(raw_workspace).unlink(missing_ok=True)
            shutil.rmtree(quarantine)
        except OSError:
            return "remove_failed"
        return "removed"
    workspace = raw_workspace.resolve()
    if not is_pipeline_owned_worktree(workspace, repository_path):
        return "not_owned"
    repo = _resolve(repository_path)
    try:
        result = subprocess.run(
            ["git", "worktree", "remove", str(workspace)],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except _GIT_OPERATION_ERRORS:
        return "remove_failed"
    if result.returncode != 0:
        return "remove_failed"
    # Reconciles this worktree's own `.git/worktrees/<name>` entry
    # immediately, on the success path. `prune_repository` below covers every
    # other path (crash, "not_owned", "remove_failed") via the periodic sweep.
    prune_repository(repo)
    return "removed"


def prune_repository(repository_path: str | Path) -> str:
    """Reconcile dangling `.git/worktrees/<name>` metadata for
    `repository_path` via `git worktree prune`.

    Read-mostly and safe: it only removes bookkeeping for a worktree whose
    directory is already gone, never a worktree directory itself, and never
    touches this repository's working tree or any other worktree's files.

    Exists for the sweep `remove_workspace`'s inline prune cannot cover: a
    worker crash between `provision_workspace` and any cleanup attempt, or a
    `remove_workspace` call that returned `"not_owned"`/`"remove_failed"`,
    leaves dangling metadata that only a later, independent pass ever
    revisits. `command_center.worktree_sweep` is that pass — it calls this
    once per repository this host has configured, on a periodic cadence.

    Returns `"pruned"` on success, `"not_a_repository"` if `repository_path`
    is not a git repository on this host, or `"prune_failed"` on any other
    git/OS error. Never raises, so a sweep over many repositories cannot be
    aborted by one bad path — same contract as `remove_workspace`."""
    repo = _resolve(repository_path)
    if not repo.is_dir() or not git_info.get_status(repo).get("is_repo"):
        return "not_a_repository"
    try:
        result = subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except _GIT_OPERATION_ERRORS:
        return "prune_failed"
    return "pruned" if result.returncode == 0 else "prune_failed"
