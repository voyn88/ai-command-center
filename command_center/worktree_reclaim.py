"""One-time, audited sweep to reclaim git worktrees this pipeline leaked
before any owner/task/deadline was ever recorded for them
(VOYN-W0-AICC-WORKTREE-LEAK-REM).

Eight different mechanisms create a linked worktree on behalf of a task
(`portfolio_launch.py`, `workspace_provisioning.py`,
`roadmap/program/ready_tasks.py --prepare-worktrees`, ...), but exactly one
site ever calls `git worktree remove` -- `portfolio_launch.remove_worktree`,
and only on the rollback of a launch attempt that failed *before* the agent
ever started. A worktree whose agent finished, crashed, or was never picked
up has no removal path at all; over time these accumulate across every
configured repository. `worktree_sweep.py` does not help here: it only
reconciles `.git/worktrees/<name>` bookkeeping for a worktree *directory*
that is already gone, never a worktree that is still sitting on disk.

This module is the one-off, human-invoked remediation for the ones that
already leaked. It is not a periodic timer: unlike a dangling
`.git/worktrees/<name>` entry (harmless bookkeeping), removing a worktree
that is still in use is destructive, so every classification is logged and
nothing is removed unless `--apply` is passed.

Follow-up on an earlier version of this sweep, rejected by adversarial
review (independent-review findings on db95d63):

- The HEAD commit's authored/committer timestamp is not "worktree activity".
  A worktree freshly created by `ready_tasks.py --prepare-worktrees` against
  an old `origin/main` looks ancient by that measure despite having been
  prepared moments ago and awaiting pickup. `worktree_metadata_age_days`
  below measures time since this *worktree* (not the commit it happens to
  have checked out) was last touched by git -- creation included, since
  `git worktree add` populates its admin directory at that moment.
- A worktree's safety conditions (lease, cleanliness, HEAD, age) were
  evaluated once during an initial sweep pass and then replayed at deletion
  time from a cached decision, so a lease taken -- or a commit landed, or
  the tree gone dirty -- in the interim went unnoticed. `reclaim_worktree`
  now re-evaluates a candidate from scratch immediately before removing it;
  a cached `Candidate` from `discover_candidates`/`sweep` is used only to
  decide what to *attempt*, never to authorize a removal directly.
- `--min-age-days` rejected only "not a number", not a negative value or
  `NaN`/`inf` -- and a `NaN` threshold makes every age comparison false,
  silently treating every worktree as old enough to reclaim. `_min_age_days`
  now rejects both explicitly.

Git-write surface: the only mutating call this module reaches is
`workspace_provisioning.remove_workspace`'s already-reviewed `git worktree
remove`, invoked only for a path `is_pipeline_owned_worktree` has itself
proven -- never a human's primary working tree, a different repository, or a
directory this pipeline did not create.
"""

from __future__ import annotations

import argparse
import logging
import math
import time
from dataclasses import dataclass, replace
from pathlib import Path

from command_center import git_info, project_config, workspace_provisioning
from command_center.worker.worktree_lease import blocking_lease

logger = logging.getLogger(__name__)

__all__ = [
    "Candidate",
    "DEFAULT_MIN_AGE_DAYS",
    "discover_candidates",
    "evaluate_worktree",
    "main",
    "reclaim_worktree",
    "sweep",
    "worktree_metadata_age_days",
]

DEFAULT_MIN_AGE_DAYS = 7.0

# Why a candidate is (not) reclaimable -- the exact string logged for every
# decision, so the sweep's own output is the audit trail.
REASON_NOT_OWNED = "not_pipeline_owned"
REASON_DIRTY = "dirty_working_tree"
REASON_LEASED = "leased"
REASON_TOO_YOUNG = "younger_than_min_age"
REASON_AGE_UNKNOWN = "age_unknown"
REASON_RECLAIMABLE = "reclaimable"


@dataclass(frozen=True)
class Candidate:
    """One worktree's classification. `reclaimable=True` only ever comes out
    of `evaluate_worktree` -- it is a snapshot, never itself an authorization
    to delete anything (see `reclaim_worktree`)."""

    repository_path: str
    worktree_path: str
    branch: str | None
    age_days: float | None
    reason: str
    reclaimable: bool


def _admin_dir(worktree: Path) -> Path | None:
    """`<git-common-dir>/worktrees/<name>` for a linked worktree, or `None`
    if it cannot be determined -- mirrors
    `worktree_launcher.git_metadata_accessible`'s own `--git-dir` lookup."""
    result = git_info.run_git_command(worktree, ["rev-parse", "--git-dir"])
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return None
    candidate = Path(result.stdout.strip())
    if not candidate.is_absolute():
        candidate = worktree / candidate
    try:
        return candidate.resolve()
    except OSError:
        return None


def worktree_metadata_age_days(worktree: str | Path) -> float | None:
    """Days since anything git-observable last happened *in this worktree*
    -- created, checked out, committed, merged, or reset -- deliberately not
    the HEAD commit's own authored/committer timestamp (see module
    docstring). Reads the admin files a linked worktree keeps outside the
    work tree itself (`HEAD`, `ORIG_HEAD`, `logs/HEAD`), which git
    (re)writes at `worktree add` time and on every later ref update
    performed from inside this specific worktree, and takes the most recent
    of their mtimes.

    Deliberately excludes `index` and the admin directory's own mtime: a
    plain `git status` rewrites the index (via a lock-file rename, even when
    nothing changed) as a side effect, and that rename touches both the
    index file's mtime *and* its parent directory's mtime. `evaluate_worktree`
    runs exactly that status check just before calling this function, so
    counting either would reset the reported age to ~0 on every single
    evaluation. Staged-but-uncommitted changes are already caught by the
    dirty-tree check that runs before age is ever consulted, so no
    abandonment signal is lost by leaving them out; the admin directory's
    own mtime is used only as a last-resort fallback, when none of the
    per-file signals exist at all.

    Returns `None` when no age signal is available at all -- callers must
    treat that as "cannot prove abandonment", never as "infinitely old".
    """
    admin_dir = _admin_dir(Path(worktree))
    if admin_dir is None or not admin_dir.is_dir():
        return None
    mtimes: list[float] = []
    for name in ("HEAD", "ORIG_HEAD", "logs/HEAD"):
        try:
            mtimes.append((admin_dir / name).stat().st_mtime)
        except OSError:
            continue
    if not mtimes:
        try:
            mtimes.append(admin_dir.stat().st_mtime)
        except OSError:
            pass
    if not mtimes:
        return None
    newest = max(mtimes)
    now = time.time()
    if newest >= now:
        # Clock skew, or activity in the same instant as this read -- never
        # report a negative age; treat it as freshly active.
        return 0.0
    return (now - newest) / 86400.0


def evaluate_worktree(
    repository_path: str | Path,
    worktree_path: str | Path,
    *,
    min_age_days: float,
    branch: str | None = None,
) -> Candidate:
    """Classify one worktree, fresh, right now. Every check here reads live
    state (git status, the lease authority, filesystem mtimes) -- nothing is
    cached across calls, so calling this twice in a row can legitimately
    return two different answers if the world changed in between. That is
    the point: `reclaim_worktree` relies on exactly this to re-check a
    candidate immediately before deleting it."""
    repo_str = str(repository_path)
    worktree_str = str(worktree_path)
    worktree = Path(worktree_path)

    if not workspace_provisioning.is_pipeline_owned_worktree(worktree, repository_path):
        return Candidate(repo_str, worktree_str, branch, None, REASON_NOT_OWNED, False)

    status = git_info.get_status(worktree)
    if status.get("dirty"):
        return Candidate(repo_str, worktree_str, branch, None, REASON_DIRTY, False)

    if blocking_lease(worktree) is not None:
        return Candidate(repo_str, worktree_str, branch, None, REASON_LEASED, False)

    age_days = worktree_metadata_age_days(worktree)
    if age_days is None:
        return Candidate(repo_str, worktree_str, branch, None, REASON_AGE_UNKNOWN, False)
    if age_days < min_age_days:
        return Candidate(repo_str, worktree_str, branch, age_days, REASON_TOO_YOUNG, False)

    return Candidate(repo_str, worktree_str, branch, age_days, REASON_RECLAIMABLE, True)


def discover_candidates(
    repository_path: str | Path, *, min_age_days: float
) -> list[Candidate]:
    """Classify every worktree `git worktree list` reports for
    `repository_path`, excluding the primary working tree itself."""
    repo = Path(repository_path)
    if not git_info.get_status(repo).get("is_repo"):
        return []
    try:
        repo_resolved = repo.resolve()
    except OSError:
        return []

    candidates: list[Candidate] = []
    for entry in git_info.get_worktrees(repo):
        raw_path = entry.get("path")
        if not raw_path:
            continue
        try:
            resolved = Path(raw_path).resolve()
        except OSError:
            continue
        if resolved == repo_resolved:
            continue  # the primary working tree, never a reclaim candidate
        candidates.append(
            evaluate_worktree(
                repo,
                resolved,
                min_age_days=min_age_days,
                branch=entry.get("branch"),
            )
        )
    return candidates


def reclaim_worktree(
    repository_path: str | Path,
    worktree_path: str | Path,
    *,
    min_age_days: float,
    branch: str | None = None,
) -> str:
    """Re-evaluate `worktree_path` from scratch -- fresh lease, fresh HEAD,
    fresh cleanliness, fresh age -- and remove it only if that fresh read
    still says it is reclaimable. Never trusts a `Candidate` computed by an
    earlier `discover_candidates`/`sweep` pass: this is the only function in
    this module that is allowed to call `remove_workspace`, and it always
    re-derives its own authorization first."""
    fresh = evaluate_worktree(
        repository_path, worktree_path, min_age_days=min_age_days, branch=branch
    )
    if not fresh.reclaimable:
        return fresh.reason
    return workspace_provisioning.remove_workspace(worktree_path, repository_path)


def sweep(*, min_age_days: float, apply: bool = False) -> list[Candidate]:
    """Classify every worktree of every locally configured repository.

    `apply=False` (the default) only classifies -- nothing is removed, and
    every `Candidate.reason` reflects the classification pass itself.
    `apply=True` additionally attempts `reclaim_worktree` for every
    candidate that classified as reclaimable, and the returned `Candidate`s
    carry the *outcome* of that fresh, re-checked attempt instead."""
    repository_paths = sorted(
        {
            cfg["repository_path"]
            for cfg in project_config.load_project_configs().values()
            if cfg.get("repository_path")
        }
    )
    candidates: list[Candidate] = []
    for repository_path in repository_paths:
        repo = Path(repository_path)
        if not repo.is_dir():
            continue
        candidates.extend(discover_candidates(repo, min_age_days=min_age_days))

    if not apply:
        return candidates

    outcomes: list[Candidate] = []
    for candidate in candidates:
        if not candidate.reclaimable:
            outcomes.append(candidate)
            continue
        outcome = reclaim_worktree(
            candidate.repository_path,
            candidate.worktree_path,
            min_age_days=min_age_days,
            branch=candidate.branch,
        )
        outcomes.append(replace(candidate, reason=outcome, reclaimable=outcome == "removed"))
    return outcomes


def _min_age_days(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--min-age-days must be a number: {raw!r}") from exc
    if not math.isfinite(value):
        raise argparse.ArgumentTypeError(f"--min-age-days must be finite: {raw!r}")
    if value < 0:
        raise argparse.ArgumentTypeError(f"--min-age-days must not be negative: {raw!r}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audited, one-time sweep to reclaim leaked pipeline-owned git worktrees."
    )
    parser.add_argument(
        "--min-age-days",
        type=_min_age_days,
        default=DEFAULT_MIN_AGE_DAYS,
        help=(
            "Minimum worktree metadata age, in days, before a clean, "
            "unleased, pipeline-owned worktree is reclaimable "
            "(default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually remove reclaimable worktrees. Without this flag the sweep only reports what it would do.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    candidates = sweep(min_age_days=args.min_age_days, apply=args.apply)
    if not candidates:
        print("no worktrees found across configured repositories")
        return 0

    mode = "APPLY" if args.apply else "DRY-RUN"
    removed = 0
    for candidate in candidates:
        age = f"{candidate.age_days:.1f}d" if candidate.age_days is not None else "unknown"
        print(
            f"{mode:8s} {candidate.reason:24s} age={age:>8s} "
            f"{candidate.worktree_path} ({candidate.branch})"
        )
        if args.apply and candidate.reason == "removed":
            removed += 1

    if args.apply:
        print(f"removed {removed} of {len(candidates)} worktree(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
