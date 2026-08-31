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

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from command_center import git_info

# Branch names that denote a repository's main line rather than isolated
# feature/audit work. A task whose expected branch is one of these (or equals
# its own base branch) is treated as main-line work that may legitimately run
# in the primary working tree; anything else must run in an isolated worktree.
MAIN_BRANCH_NAMES = frozenset({"main", "master"})

# Working-tree cleanliness policies a caller can require before launch.
STATUS_POLICY_ALLOW_DIRTY = "allow_dirty"      # default — never blocks on dirtiness
STATUS_POLICY_NO_UNTRACKED = "no_untracked"    # block if there are untracked files
STATUS_POLICY_CLEAN = "clean"                  # block on any modified/untracked file
STATUS_POLICIES = frozenset(
    {STATUS_POLICY_ALLOW_DIRTY, STATUS_POLICY_NO_UNTRACKED, STATUS_POLICY_CLEAN}
)
PROVISION_OUTCOMES = frozenset({"skipped", "reused", "attached", "created"})

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
            "checks": list(self.checks),
        }


# --------------------------------------------------------------------------
# Small git helpers (read-only unless noted)
# --------------------------------------------------------------------------


def _resolve(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


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


def is_pipeline_owned_worktree(workspace: str | Path, repository_path: str | Path | None) -> bool:
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
    return ws_common is not None and repo_common is not None and ws_common == repo_common


def _conflicting_worktree(repo: Path, branch: str, workspace_resolved: Path) -> str | None:
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
    if base_branch and expected_branch == base_branch:
        return False
    return True


# --------------------------------------------------------------------------
# Provisioning (the only git-write path: `git worktree add`)
# --------------------------------------------------------------------------


def _worktree_add(repo: Path, workspace: Path, extra_args: list[str], spec: WorkspaceSpec) -> None:
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
    except (OSError, subprocess.SubprocessError) as exc:
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
            detail=(result.stderr or result.stdout).strip() or "git worktree add failed",
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
    if workspace.exists():
        return "reused"
    if not spec.allow_provision:
        return "skipped"
    if not (spec.repository_path and spec.expected_branch and spec.base_branch):
        return "skipped"

    repo = _resolve(spec.repository_path)
    if not git_info.get_status(repo).get("is_repo"):
        return "skipped"

    workspace_target = workspace.resolve()
    branch_exists = spec.expected_branch in git_info.get_branches(repo)
    if branch_exists:
        conflict = _conflicting_worktree(repo, spec.expected_branch, _resolve(workspace))
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
        _worktree_add(repo, workspace, [str(workspace_target), spec.expected_branch], spec)
        return "attached"

    remote_branch = _remote_branch_ref(repo, spec.expected_branch)
    if remote_branch is not None:
        _worktree_add(
            repo,
            workspace,
            ["--track", "-b", spec.expected_branch, str(workspace_target), remote_branch],
            spec,
        )
        return "attached"

    verify = git_info.run_git_command(repo, ["rev-parse", "--verify", f"{spec.base_branch}^{{commit}}"])
    if verify is None or verify.returncode != 0:
        # Base branch missing — let `verify_workspace` report it as the
        # structured `base_branch_exists` / `workspace_exists` failure rather
        # than raising a less specific error here.
        return "skipped"
    _worktree_add(repo, workspace, ["-b", spec.expected_branch, str(workspace_target), spec.base_branch], spec)
    return "created"


# --------------------------------------------------------------------------
# Verification (the mandatory, fail-closed gate)
# --------------------------------------------------------------------------


def verify_workspace(spec: WorkspaceSpec) -> VerificationEvidence:
    """Prove every launch invariant or raise `WorkspaceVerificationError`.

    Returns `VerificationEvidence` only when *all* checks pass — this return
    is the sole authorization for emitting "Workspace Verified". Never mutates
    anything (read-only git + filesystem stat)."""
    workspace_input = spec.workspace_path
    workspace_resolved = _resolve(workspace_input)
    repo_resolved = _resolve(spec.repository_path) if spec.repository_path else None

    evidence = VerificationEvidence(
        workspace_path=str(workspace_resolved),
        repository_path=str(repo_resolved) if repo_resolved else None,
        expected_branch=spec.expected_branch,
        actual_branch=None,
    )

    def fail(step: str, remediation: str, detail: str, actual_branch: str | None = None) -> None:
        raise WorkspaceVerificationError(
            failed_step=step,
            remediation=remediation,
            expected_workspace=workspace_input,
            actual_workspace=str(workspace_resolved),
            expected_branch=spec.expected_branch,
            actual_branch=actual_branch,
            detail=detail,
        )

    if spec.status_policy not in STATUS_POLICIES:
        raise ValueError(f"Unknown status_policy: {spec.status_policy!r}")
    if spec.provision_outcome not in PROVISION_OUTCOMES:
        raise ValueError(f"Unknown provision_outcome: {spec.provision_outcome!r}")
    evidence.provision_outcome = spec.provision_outcome

    # 1. Workspace exists.
    if not Path(workspace_input).expanduser().exists() or not workspace_resolved.is_dir():
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

    # 3. Workspace belongs to the expected repository.
    if repo_resolved is not None and git_info.get_status(repo_resolved).get("is_repo"):
        workspace_common = _git_common_dir(workspace_resolved)
        repo_common = _git_common_dir(repo_resolved)
        if workspace_common is None or repo_common is None or workspace_common != repo_common:
            fail(
                "workspace_belongs_to_repository",
                "Use a worktree of the configured repository, not a different repository.",
                f"workspace belongs to a different repository than {spec.repository_path}",
                actual_branch=actual_branch,
            )
        evidence.record("workspace_belongs_to_repository", True, str(workspace_common))
    else:
        evidence.record("workspace_belongs_to_repository", True, "source repository not provided; skipped")

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
        conflict = _conflicting_worktree(repo_resolved, spec.expected_branch, workspace_resolved)
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
    evidence.is_isolated_worktree = not is_primary
    if feature and is_primary:
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
    evidence.record("isolated_worktree_required", True, f"is_isolated={not is_primary}")

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
    evidence = verify_workspace(spec)
    evidence.provision_outcome = outcome
    return evidence


# --------------------------------------------------------------------------
# Teardown (the other mutating git subcommands: `worktree remove` / `prune`)
# --------------------------------------------------------------------------


def remove_workspace(workspace_path: str | Path, repository_path: str | Path) -> str:
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
    workspace = _resolve(workspace_path)
    if not workspace.exists():
        return "not_found"
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
    except (OSError, subprocess.SubprocessError):
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
    except (OSError, subprocess.SubprocessError):
        return "prune_failed"
    return "pruned" if result.returncode == 0 else "prune_failed"
