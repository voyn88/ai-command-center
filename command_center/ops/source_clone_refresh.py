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


def refresh_source_clone(
    repository: Path, *, pr_number: str | None = None
) -> RefreshResult:
    """Fetch and fast-forward the source clone before a reviewer clones from it."""
    repo = repository.resolve()
    if not repo.is_dir():
        return RefreshResult(ok=False, path=str(repo), error="source clone is absent")

    fetch_args = ["fetch", "--prune", "origin"]
    if pr_number:
        fetch_args.append(
            f"refs/pull/{pr_number}/head:refs/remotes/origin/pr/{pr_number}/head"
        )
    fetch = _git(repo, fetch_args)
    if fetch.returncode != 0:
        return _failure(repo, "source clone fetch failed", fetch)

    upstream_result = _git(
        repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]
    )
    upstream = (
        upstream_result.stdout.strip() if upstream_result.returncode == 0 else None
    )
    if upstream:
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
