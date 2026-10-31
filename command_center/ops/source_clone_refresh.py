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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m command_center.ops.source_clone_refresh"
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("AICC_SOURCE_CLONE_REPO", "."),
        help="Local source clone to update. Defaults to $AICC_SOURCE_CLONE_REPO or cwd.",
    )
    parser.add_argument(
        "--pr-number",
        default="",
        help="Optional PR number whose refs/pull/<n>/head must become locally reachable.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = refresh_source_clone(
        Path(args.repo), pr_number=args.pr_number.strip() or None
    )
    print(json.dumps(asdict(result), sort_keys=True))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
