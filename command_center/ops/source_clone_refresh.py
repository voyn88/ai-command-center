"""Update the local source clone used by isolated read-only review lanes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RefreshResult:
    ok: bool
    path: str
    head: str | None = None
    upstream: str | None = None
    error: str | None = None
    #: the clone is not on this host at all, so the periodic tick had nothing
    #: to do.  Only ``refresh_tick`` ever sets this -- see its docstring.
    skipped: bool = False


def _git(
    repo: Path, args: list[str], *, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )


def _failure(
    repo: Path, prefix: str, result: subprocess.CompletedProcess[str]
) -> RefreshResult:
    detail = (result.stderr or result.stdout or "git failed").strip()[:300]
    return RefreshResult(ok=False, path=str(repo), error=f"{prefix}: {detail}")


_READONLY_FETCH_MARKERS = (
    "read-only file system",
    "erofs",
)


def _fetch_failed_because_clone_is_read_only(
    result: subprocess.CompletedProcess[str],
) -> bool:
    """The isolated worker binds the Projects clone read-only. A fetch
    there cannot write FETCH_HEAD; the host-side refresh unit is the
    writer (VOYN-W0-AICC-ISOLATED-WORKER-FETCHES-READONLY-SOURCE-CLONE)."""
    detail = f"{result.stderr or ''}\n{result.stdout or ''}".lower()
    return any(marker in detail for marker in _READONLY_FETCH_MARKERS)


def _pr_ref_is_present(repo: Path, pr_number: str) -> bool:
    probed = _git(
        repo, ["rev-parse", "--verify", f"refs/remotes/origin/pr/{pr_number}/head"]
    )
    return probed.returncode == 0


def refresh_source_clone(
    repository: Path, *, pr_number: str | None = None
) -> RefreshResult:
    """Fetch and fast-forward the source clone before a reviewer clones from it.

    When the clone is mounted read-only, fetch/merge are skipped and the
    already-mirrored objects are used. A requested PR ref that is not
    already present stays a failure so the tick retries after the host
    mirror catches up -- it must not try to write the bound `.git`.
    """
    repo = repository.resolve()
    if not repo.is_dir():
        return RefreshResult(ok=False, path=str(repo), error="source clone is absent")

    # Two fetches: a command-line refspec replaces the remote's default
    # (`git fetch origin refs/pull/...` would not update origin/main).
    fetch = _git(repo, ["fetch", "--prune", "origin"])
    read_only = False
    if fetch.returncode != 0:
        if not _fetch_failed_because_clone_is_read_only(fetch):
            return _failure(repo, "source clone fetch failed", fetch)
        read_only = True
        if pr_number and not _pr_ref_is_present(repo, pr_number):
            return RefreshResult(
                ok=False,
                path=str(repo),
                error=(
                    "source clone is read-only and PR ref is absent: "
                    f"refs/remotes/origin/pr/{pr_number}/head"
                ),
            )
    else:
        pull_args = [
            "fetch",
            "origin",
            "+refs/pull/*/head:refs/remotes/origin/pr/*/head",
        ]
        if pr_number:
            pull_args.append(
                f"refs/pull/{pr_number}/head:refs/remotes/origin/pr/{pr_number}/head"
            )
        pull = _git(repo, pull_args)
        if pull.returncode != 0 and not _fetch_failed_because_clone_is_read_only(pull):
            return _failure(repo, "source clone fetch failed", pull)

    upstream_result = _git(
        repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    )
    upstream = (
        upstream_result.stdout.strip() if upstream_result.returncode == 0 else None
    )
    if upstream and not read_only:
        merge = _git(repo, ["merge", "--ff-only", upstream])
        if merge.returncode != 0:
            return _failure(repo, "source clone fast-forward failed", merge)

    head = _git(repo, ["rev-parse", "HEAD"])
    if head.returncode != 0:
        return _failure(repo, "source clone head read failed", head)
    return RefreshResult(
        ok=True, path=str(repo), head=head.stdout.strip(), upstream=upstream
    )


def refresh_tick(repository: Path, *, pr_number: str | None = None) -> RefreshResult:
    """The PERIODIC host refresh of one clone, whose contract differs from
    `refresh_source_clone` in exactly one case: a clone this host does not
    have is nothing a refresh can do, so it is a skip and not a failure.

    The fleet deliberately runs worker hosts without some of these clones --
    `voyn-aicc-worker-principal-isolation.conf` binds the aios clone with the
    missing-tolerant `-` prefix precisely so that "a host without that clone
    still serves ai-command-center tasks" -- and the refresh unit declares
    the same path missing-tolerant in its own `ReadWritePaths=`. Its
    ExecStart nevertheless asked for that clone unconditionally, so on every
    such host the `Type=oneshot` unit ended `failed`, every two minutes,
    forever. Nothing reaps a failed unit: the host unit-health probe counts
    units that are failed NOW, so one absent optional clone was a standing
    `failed_units` finding on worker-01 that no repair of anything else could
    clear (monitor_finding 2408, after 2051 and 2295 had removed the
    launcher's per-connection corpses).

    Absence is a provisioning fact, and it is not silent here: a REQUIRED
    clone that goes missing is refused by the lane's own
    `BindReadOnlyPaths=` with no `-`, so the lane does not start and the
    monitor reports `active_workers` -- a far better alarm than a refresh
    tick that cannot create a clone either way. A clone that IS present and
    cannot be refreshed stays a failure: that is a fault this tick measures.

    `refresh_source_clone` keeps reporting absence as a failure, because its
    other caller is the worker's pre-review refresh of the repository a task
    pins -- there, an absent clone means the review would run against
    nothing, and it must fail the attempt.
    """
    repo = repository.resolve()
    if not repo.is_dir():
        return RefreshResult(ok=True, path=str(repo), skipped=True)
    return refresh_source_clone(repo, pr_number=pr_number)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m command_center.ops.source_clone_refresh"
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("AICC_SOURCE_CLONE_REPO", "."),
        help=(
            "Local source clone to update. Defaults to $AICC_SOURCE_CLONE_REPO "
            "or cwd. A clone this host does not have is skipped, not failed "
            "(see refresh_tick)."
        ),
    )
    parser.add_argument(
        "--pr-number",
        default="",
        help="Optional PR number whose refs/pull/<n>/head must become locally reachable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = refresh_tick(Path(args.repo), pr_number=args.pr_number.strip() or None)
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
