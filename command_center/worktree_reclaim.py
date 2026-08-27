"""One-time, auditable sweep that reclaims worktree *directories* leaked by
mechanisms nothing ever revisits (VOYN-W0-AICC-WORKTREE-LEAK).

Measured on a live host: dozens of linked worktrees, none recording an
owner, task or deadline. `git worktree remove` exists in a handful of
places, but each is scoped to its own mechanism's own lifecycle, and not
every mechanism has one at all:

- `workspace_provisioning.remove_workspace` is called on both the success
  and failure paths of the lease-worker daemon (`worker/handlers.py`) — that
  mechanism is self-cleaning.
- `portfolio_launch.py`'s own `git worktree remove` fires *only* on rollback
  of a launch that never actually started (queue-persist failure, or
  `launch_ready` reporting the run never started). Once a portfolio-launched
  run starts, nothing on that path — `runtime/supervisor.py`,
  `runtime/completion.py` — ever calls `remove_workspace` again, on success,
  on failure, or on abandonment. That worktree lives forever.
- `roadmap/program/ready_tasks.py --prepare-worktrees` creates a worktree
  ahead of a task being picked up and has no counterpart at all.
- `command_center.daily_audit_backend` is mostly self-cleaning but
  deliberately preserves the worktree on a completion timeout ("preserved
  for recovery") and silently skips cleanup on a dirty tree with no retry.

Patching every one of those call sites — and every future one — to remember
to clean up after itself is the wrong shape of fix: it is exactly the "N
creation sites, ad hoc cleanup" pattern that produced the leak. This module
instead sweeps the ground truth, `git worktree list`, for every configured
repository, the same way `worktree_sweep` already does for dangling
metadata. Unlike that sweep (which only reconciles a worktree whose
directory is *already gone*), this one looks at worktree directories that
still exist on disk and reclaims the ones provably safe to remove.

"Provably safe" reuses the same fail-closed primitives the rest of this
package already relies on, and adds nothing new that can itself delete
something it shouldn't:

- `workspace_provisioning.is_pipeline_owned_worktree` — never touches the
  repository's primary working tree, a different repository, or a directory
  that is not a git-registered linked worktree.
- A dirty working tree (`git status --porcelain` non-empty) is never
  touched — matches `remove_workspace`'s own "never force a dirty removal"
  contract.
- A worktree covered by a live writer lease (`worker.worktree_lease`'s
  `voyn-lease list` contract) is never touched, and — fail-closed, mirroring
  `worktree_lease.blocking_lease` — if a lease authority is configured but
  cannot be reached or returns unreadable output, nothing in that sweep run
  is reclaimed rather than guessing.
- A worktree whose HEAD commit is younger than `--min-age-days` (default 7)
  is never touched: still-active work is left alone regardless of which
  mechanism created it.
- A worktree whose HEAD commit age cannot be determined at all is never
  touched — an unreadable signal is not evidence of safety.

Deliberately DB-free, mirroring `worktree_sweep`'s own reasoning: this reads
`project_config.load_project_configs()` (host-local configuration) and runs
read-mostly git subcommands against each configured repository's worktrees.
The one write it performs, `workspace_provisioning.remove_workspace`, is the
exact same primitive every self-cleaning mechanism already uses — no new
git-write surface.

Auditable: every worktree this sweep looks at — reclaimed, skipped, or
failed — is emitted as one JSON line (`--audit-log`, or stdout) recording the
path, branch, HEAD, computed age and the specific reason for the decision.
Dry-run by default (`sweep_configured_repositories` alone never deletes
anything); `main()` only removes anything when invoked with `--apply`, so an
operator reviews the report before a single directory is touched — the "one-
off, auditable sweep" the existing backlog of leaked worktrees needs, as
opposed to `worktree_sweep`'s unattended periodic metadata prune.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from command_center import git_info, project_config, workspace_provisioning

logger = logging.getLogger(__name__)

__all__ = [
    "ReclaimDecision",
    "apply_decision",
    "evaluate_worktree",
    "main",
    "sweep_configured_repositories",
    "sweep_repository",
]

DECISION_RECLAIMABLE = "reclaimable"
DECISION_SKIP_DIRTY = "skip_dirty"
DECISION_SKIP_RECENT = "skip_recent"
DECISION_SKIP_LEASED = "skip_leased"
DECISION_SKIP_LEASE_UNKNOWN = "skip_lease_authority_unreachable"
DECISION_SKIP_NOT_OWNED = "skip_not_pipeline_owned"
DECISION_SKIP_NO_ACTIVITY_SIGNAL = "skip_head_commit_unknown"
DECISION_SKIP_METADATA_UNAVAILABLE = "skip_metadata_unavailable"

_DEFAULT_MIN_AGE_DAYS = 7.0
_LEASE_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class ReclaimDecision:
    """One worktree's audit record: what it is and why it was, or was not,
    reclaimed. `applied_outcome` is filled in only when `--apply` actually
    calls `remove_workspace` for a `reclaimable` decision."""

    repository_path: str
    worktree_path: str
    branch: str
    head_sha: str
    last_activity_at: str | None
    age_days: float | None
    decision: str
    reason: str
    applied_outcome: str | None = None

    @property
    def reclaimable(self) -> bool:
        return self.decision == DECISION_RECLAIMABLE


# --------------------------------------------------------------------------
# Writer-lease signal (same `voyn-lease list` contract as
# `worker.worktree_lease.blocking_lease`, read-only, no ancestry carve-out —
# this sweep is never itself the process that would hold a legitimate lease).
# --------------------------------------------------------------------------


def _lease_expiry(row: dict) -> datetime | None:
    raw = row.get("expires_at")
    if not isinstance(raw, str):
        return None
    try:
        moment = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def leased_worktree_paths() -> tuple[bool, frozenset[Path]] | None:
    """`(authority_reachable, resolved_leased_paths)`, or `None` if no lease
    authority is configured on this host at all (`VOYN_LEASE_DSN` unset) —
    that host simply has no lease signal to consult, not a failure.

    When an authority *is* configured, every failure to get a trustworthy
    listing from it (missing tool, non-zero exit, unparseable output,
    timeout) returns `(False, frozenset())`: fail-closed, so a caller that
    cannot confirm a worktree is lease-free treats every candidate as
    unconfirmed rather than assuming the coast is clear.
    """
    if not os.environ.get("VOYN_LEASE_DSN"):
        return None
    tool = os.environ.get("VOYN_LEASE_TOOL", "voyn-lease")
    try:
        listed = subprocess.run(
            [tool, "list"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_LEASE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return (False, frozenset())
    if listed.returncode != 0:
        return (False, frozenset())
    try:
        rows = json.loads(listed.stdout or "[]")
    except json.JSONDecodeError:
        return (False, frozenset())
    if not isinstance(rows, list):
        return (False, frozenset())

    now = datetime.now(timezone.utc)
    paths: set[Path] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        expires_at = _lease_expiry(row)
        if expires_at is not None and expires_at <= now:
            continue  # an expired lease holds nothing
        leased = row.get("worktree")
        if not isinstance(leased, str) or not leased:
            continue
        try:
            paths.add(Path(leased).resolve())
        except OSError:
            continue
    return (True, frozenset(paths))


def _leased(path: Path, leased_paths: frozenset[Path]) -> bool:
    return path in leased_paths or any(path.is_relative_to(p) for p in leased_paths)


# --------------------------------------------------------------------------
# Per-worktree classification
# --------------------------------------------------------------------------


def _head_commit_epoch(worktree_path: Path) -> int | None:
    result = git_info.run_git_command(worktree_path, ["log", "-1", "--format=%ct"])
    if result is None or result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def evaluate_worktree(
    entry: dict[str, str],
    repository_path: Path,
    *,
    min_age_days: float = _DEFAULT_MIN_AGE_DAYS,
    lease_state: tuple[bool, frozenset[Path]] | None,
    now: datetime | None = None,
) -> ReclaimDecision | None:
    """Classify one `git worktree list --porcelain` entry for
    `repository_path`. Returns `None` for the repository's own primary
    working tree — it is never a candidate and never worth an audit line."""
    raw_path = entry.get("path")
    if not raw_path:
        return None
    worktree_path = Path(raw_path)

    if not workspace_provisioning.is_pipeline_owned_worktree(worktree_path, repository_path):
        if not worktree_path.exists():
            return None  # not a live directory at all; worktree_sweep's job
        try:
            resolved = worktree_path.resolve()
        except OSError:
            resolved = worktree_path
        if resolved == Path(repository_path).expanduser().resolve():
            return None  # the primary working tree — never a candidate
        return ReclaimDecision(
            repository_path=str(repository_path),
            worktree_path=str(worktree_path),
            branch=entry.get("branch", ""),
            head_sha=entry.get("head", ""),
            last_activity_at=None,
            age_days=None,
            decision=DECISION_SKIP_NOT_OWNED,
            reason="not a registered linked worktree of this repository",
        )

    branch = entry.get("branch", "")
    head_sha = entry.get("head", "")

    status = git_info.get_status(worktree_path)
    if not status.get("is_repo"):
        return ReclaimDecision(
            repository_path=str(repository_path),
            worktree_path=str(worktree_path),
            branch=branch,
            head_sha=head_sha,
            last_activity_at=None,
            age_days=None,
            decision=DECISION_SKIP_METADATA_UNAVAILABLE,
            reason="git metadata unreadable at this path — leave for the metadata prune sweep",
        )
    if status.get("dirty"):
        modified = status.get("modified_count", 0)
        untracked = status.get("untracked_count", 0)
        return ReclaimDecision(
            repository_path=str(repository_path),
            worktree_path=str(worktree_path),
            branch=branch,
            head_sha=head_sha,
            last_activity_at=None,
            age_days=None,
            decision=DECISION_SKIP_DIRTY,
            reason=f"working tree not clean ({modified} modified, {untracked} untracked)",
        )

    if lease_state is not None:
        authority_reachable, leased_paths = lease_state
        if not authority_reachable:
            return ReclaimDecision(
                repository_path=str(repository_path),
                worktree_path=str(worktree_path),
                branch=branch,
                head_sha=head_sha,
                last_activity_at=None,
                age_days=None,
                decision=DECISION_SKIP_LEASE_UNKNOWN,
                reason="writer-lease authority configured but unreachable — refusing to guess",
            )
        try:
            resolved = worktree_path.resolve()
        except OSError:
            resolved = worktree_path
        if _leased(resolved, leased_paths):
            return ReclaimDecision(
                repository_path=str(repository_path),
                worktree_path=str(worktree_path),
                branch=branch,
                head_sha=head_sha,
                last_activity_at=None,
                age_days=None,
                decision=DECISION_SKIP_LEASED,
                reason="covered by a live writer lease",
            )

    epoch = _head_commit_epoch(worktree_path)
    if epoch is None:
        return ReclaimDecision(
            repository_path=str(repository_path),
            worktree_path=str(worktree_path),
            branch=branch,
            head_sha=head_sha,
            last_activity_at=None,
            age_days=None,
            decision=DECISION_SKIP_NO_ACTIVITY_SIGNAL,
            reason="HEAD commit time could not be determined",
        )
    last_activity = datetime.fromtimestamp(epoch, tz=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    age_days = (moment - last_activity).total_seconds() / 86400.0

    if age_days < min_age_days:
        return ReclaimDecision(
            repository_path=str(repository_path),
            worktree_path=str(worktree_path),
            branch=branch,
            head_sha=head_sha,
            last_activity_at=last_activity.isoformat(),
            age_days=round(age_days, 2),
            decision=DECISION_SKIP_RECENT,
            reason=f"HEAD commit is {age_days:.1f}d old, below the {min_age_days}d grace period",
        )

    return ReclaimDecision(
        repository_path=str(repository_path),
        worktree_path=str(worktree_path),
        branch=branch,
        head_sha=head_sha,
        last_activity_at=last_activity.isoformat(),
        age_days=round(age_days, 2),
        decision=DECISION_RECLAIMABLE,
        reason=f"clean, unleased, HEAD commit is {age_days:.1f}d old",
    )


def sweep_repository(
    repository_path: str | Path,
    *,
    min_age_days: float = _DEFAULT_MIN_AGE_DAYS,
    lease_state: tuple[bool, frozenset[Path]] | None = None,
) -> list[ReclaimDecision]:
    """Classify every linked worktree `git worktree list` reports for
    `repository_path`. Never removes anything itself. `lease_state` is
    threaded in by the caller so a multi-repository sweep queries the lease
    authority once, not once per repository."""
    repo = Path(repository_path).expanduser()
    if not repo.is_dir() or not git_info.get_status(repo).get("is_repo"):
        return []
    decisions: list[ReclaimDecision] = []
    for entry in git_info.get_worktrees(repo):
        decision = evaluate_worktree(
            entry, repo, min_age_days=min_age_days, lease_state=lease_state
        )
        if decision is not None:
            decisions.append(decision)
    return decisions


def sweep_configured_repositories(
    *, min_age_days: float = _DEFAULT_MIN_AGE_DAYS
) -> list[ReclaimDecision]:
    """`sweep_repository` over every distinct, non-empty `repository_path`
    configured on this host — a `repository_path` shared by two projects is
    swept once. Dry-run: classification only, nothing is removed."""
    repository_paths = sorted(
        {
            cfg["repository_path"]
            for cfg in project_config.load_project_configs().values()
            if cfg.get("repository_path")
        }
    )
    lease_state = leased_worktree_paths()
    decisions: list[ReclaimDecision] = []
    for repository_path in repository_paths:
        decisions.extend(
            sweep_repository(repository_path, min_age_days=min_age_days, lease_state=lease_state)
        )
    return decisions


# --------------------------------------------------------------------------
# Apply (the one write path: `workspace_provisioning.remove_workspace`)
# --------------------------------------------------------------------------


def apply_decision(decision: ReclaimDecision) -> ReclaimDecision:
    """Remove the worktree a `reclaimable` decision names, via the same
    `remove_workspace` primitive every self-cleaning mechanism already uses.
    Returns a copy of `decision` with `applied_outcome` filled in. Calling
    this on a non-`reclaimable` decision is a caller bug — it raises rather
    than silently doing nothing."""
    if decision.decision != DECISION_RECLAIMABLE:
        raise ValueError(f"refusing to apply a non-reclaimable decision: {decision.decision!r}")
    outcome = workspace_provisioning.remove_workspace(
        decision.worktree_path, decision.repository_path
    )
    return replace(decision, applied_outcome=outcome)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _emit(decision: ReclaimDecision, audit_log) -> None:
    line = json.dumps(asdict(decision), sort_keys=True)
    print(line, file=audit_log)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="One-time, auditable sweep to reclaim leaked worktree directories. "
        "Dry-run (report only) unless --apply is given."
    )
    parser.add_argument(
        "--min-age-days",
        type=float,
        default=_DEFAULT_MIN_AGE_DAYS,
        help=f"skip any worktree whose HEAD commit is younger than this many days "
        f"(default: {_DEFAULT_MIN_AGE_DAYS})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually remove worktrees classified as reclaimable (default: report only)",
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help="append the JSONL audit record to this file instead of stdout",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    decisions = sweep_configured_repositories(min_age_days=args.min_age_days)

    audit_stream = args.audit_log.open("a", encoding="utf-8") if args.audit_log else sys.stdout
    try:
        failures = 0
        reclaimable = [d for d in decisions if d.reclaimable]
        for decision in decisions:
            if args.apply and decision.reclaimable:
                decision = apply_decision(decision)
                if decision.applied_outcome == "remove_failed":
                    failures += 1
            _emit(decision, audit_stream)
    finally:
        if audit_stream is not sys.stdout:
            audit_stream.close()

    mode = "applied" if args.apply else "dry-run"
    print(
        f"{mode}: {len(decisions)} worktree(s) evaluated, "
        f"{len(reclaimable)} reclaimable across "
        f"{len({d.repository_path for d in decisions})} repositories",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
