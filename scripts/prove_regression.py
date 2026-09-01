#!/usr/bin/env python3
"""Prove that named pytest node IDs actually catch a defect, or say why not.

A PR that claims "this test proves the regression" is a claim, not evidence.
Four rounds of review found this exact claim shipped false in four different
shapes: two node IDs graded as one aggregate result (a strong test hiding a
weak one), a single node ID whose parametrized cases were graded in aggregate
(same flaw, one level down), a base run built from stale test code that could
never see the fix it was supposed to prove, and an unrelated import error
counted as if it were the expected assertion failure.

This tool judges every node ID independently, requires every collected case
under that node ID to agree, and never touches the test code at all: it
checks out `--head` whole (tests, conftest, fixtures, plugins, helpers — all
of it, unmodified) and reverts only the explicit `--fix` implementation
path(s) back to their `--base` contents before the second run. A node ID is
"confirmed" only if, in that one worktree:

    * at --head, with the fix in place, every case it collects passes
      (exit code 0), and at least one case is collected at all;
    * with only the --fix path(s) put back to --base, every case it
      collects fails on a plain assertion (exit code 1 — never an error,
      never a session-level exit like 2/3/4/5); and
    * both runs collected the exact same test cases, in the exact same
      multiplicity (a `sorted()` list, not a `frozenset` that would let a
      changed parametrize count slip through unnoticed).

Usage:
    prove_regression.py --base <base-ref> [--head <head-ref>] \\
        --fix path/to/impl.py [--fix path/to/other.py] \\
        NODE_ID [NODE_ID ...]

Exits 0 only if every given node ID is confirmed.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class UnsafePathError(ValueError):
    """A --fix path tried to escape the worktree it was reverted in."""


@dataclasses.dataclass(frozen=True)
class CaseResult:
    case_id: str
    status: str  # "passed" | "failed" | "error" | "skipped"


@dataclasses.dataclass(frozen=True)
class RunResult:
    exit_code: int
    cases: tuple[CaseResult, ...]
    stdout: str
    stderr: str


@dataclasses.dataclass(frozen=True)
class NodeVerdict:
    node_id: str
    confirmed: bool
    reason: str


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------


def _safe_target(worktree: Path, raw_path: str) -> Path:
    """Resolve a --fix path to a location strictly inside `worktree`.

    Rejects absolute paths, `..` traversal (before *and* after
    normalization), and symlinks — a head-authored symlink at this path
    must not let a later write/unlink touch a file outside the worktree.
    """
    if not raw_path or raw_path != raw_path.strip():
        raise UnsafePathError(f"malformed path: {raw_path!r}")
    if Path(raw_path).is_absolute():
        raise UnsafePathError(f"absolute path is not allowed: {raw_path!r}")
    normalized = Path(os.path.normpath(raw_path))
    if normalized.is_absolute() or ".." in normalized.parts:
        raise UnsafePathError(f"path escapes the repository root: {raw_path!r}")

    root = worktree.resolve(strict=True)
    concrete = worktree / normalized
    resolved = concrete.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError:
        raise UnsafePathError(f"path escapes the worktree root: {raw_path!r}") from None
    if concrete.is_symlink():
        raise UnsafePathError(f"refusing to mutate a symlink: {raw_path!r}")
    return concrete


# --------------------------------------------------------------------------
# git plumbing
# --------------------------------------------------------------------------


def _run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=check)


def _add_worktree(repo_root: Path, head_ref: str) -> Path:
    parent = Path(tempfile.mkdtemp(prefix="prove-regression-"))
    worktree = parent / "wt"
    _run(["git", "worktree", "add", "--detach", str(worktree), head_ref], cwd=repo_root)
    return worktree


def _remove_worktree(repo_root: Path, worktree: Path) -> None:
    _run(["git", "worktree", "remove", "--force", str(worktree)], cwd=repo_root, check=False)
    shutil.rmtree(worktree.parent, ignore_errors=True)


def _read_blob(worktree: Path, ref: str, rel_path: str) -> bytes | None:
    posix_path = Path(rel_path).as_posix()
    probe = _run(["git", "cat-file", "-e", f"{ref}:{posix_path}"], cwd=worktree, check=False)
    if probe.returncode != 0:
        return None
    result = subprocess.run(
        ["git", "show", f"{ref}:{posix_path}"],
        cwd=worktree,
        capture_output=True,
        check=True,
    )
    return result.stdout


def _write_ref_contents(worktree: Path, ref: str, rel_path: str) -> None:
    """Overwrite `rel_path` inside `worktree` with its contents at `ref`.

    If the path did not exist at `ref`, it is removed instead — reverting a
    file the fix newly *added* means the base state has no such file.
    """
    target = _safe_target(worktree, rel_path)
    blob = _read_blob(worktree, ref, rel_path)
    if blob is None:
        if target.exists():
            target.unlink()
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(blob)


def _restore_fix_paths(worktree: Path, head_ref: str, fix_paths: list[str]) -> None:
    for rel_path in fix_paths:
        _write_ref_contents(worktree, head_ref, rel_path)


def _revert_fix_paths(worktree: Path, base_ref: str, fix_paths: list[str]) -> None:
    for rel_path in fix_paths:
        _write_ref_contents(worktree, base_ref, rel_path)


# --------------------------------------------------------------------------
# pytest execution + JUnit parsing
# --------------------------------------------------------------------------


def _parse_junit(path: Path) -> tuple[CaseResult, ...]:
    tree = ET.parse(path)
    cases = []
    for testcase in tree.getroot().iter("testcase"):
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        case_id = f"{classname}::{name}" if classname else name
        if testcase.find("failure") is not None:
            status = "failed"
        elif testcase.find("error") is not None:
            status = "error"
        elif testcase.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        cases.append(CaseResult(case_id=case_id, status=status))
    return tuple(cases)


def _run_pytest_node(worktree: Path, node_id: str) -> RunResult:
    junit_path = worktree / f".prove-regression-{uuid.uuid4().hex}.xml"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                node_id,
                "-q",
                "-p",
                "no:cacheprovider",
                f"--junitxml={junit_path}",
            ],
            cwd=worktree,
            capture_output=True,
            text=True,
        )
        cases = _parse_junit(junit_path) if junit_path.exists() else ()
    finally:
        junit_path.unlink(missing_ok=True)
    return RunResult(exit_code=proc.returncode, cases=cases, stdout=proc.stdout, stderr=proc.stderr)


# --------------------------------------------------------------------------
# judging
# --------------------------------------------------------------------------


def _case_ids(cases: tuple[CaseResult, ...]) -> list[str]:
    return sorted(c.case_id for c in cases)


def judge_node(
    worktree: Path,
    base_ref: str,
    head_ref: str,
    fix_paths: list[str],
    node_id: str,
) -> NodeVerdict:
    _restore_fix_paths(worktree, head_ref, fix_paths)
    head_run = _run_pytest_node(worktree, node_id)

    if not head_run.cases:
        return NodeVerdict(node_id, False, "head run collected no test cases for this node id")
    if head_run.exit_code != 0:
        return NodeVerdict(
            node_id, False, f"head run did not pass cleanly (pytest exit code {head_run.exit_code})"
        )
    non_passing = [c for c in head_run.cases if c.status != "passed"]
    if non_passing:
        offenders = ", ".join(f"{c.case_id}={c.status}" for c in non_passing)
        return NodeVerdict(node_id, False, f"head run has non-passing case(s): {offenders}")

    try:
        _revert_fix_paths(worktree, base_ref, fix_paths)
        base_run = _run_pytest_node(worktree, node_id)
    finally:
        _restore_fix_paths(worktree, head_ref, fix_paths)

    if not base_run.cases:
        return NodeVerdict(node_id, False, "base run collected no test cases for this node id")
    if base_run.exit_code != 1:
        return NodeVerdict(
            node_id,
            False,
            f"base run exit code {base_run.exit_code} is not a plain assertion failure (expected 1)",
        )

    head_ids = _case_ids(head_run.cases)
    base_ids = _case_ids(base_run.cases)
    if base_ids != head_ids:
        return NodeVerdict(
            node_id,
            False,
            f"base and head collected different cases: base={base_ids} head={head_ids}",
        )

    non_failing = [c for c in base_run.cases if c.status != "failed"]
    if non_failing:
        offenders = ", ".join(f"{c.case_id}={c.status}" for c in non_failing)
        return NodeVerdict(node_id, False, f"base run has non-failing case(s): {offenders}")

    return NodeVerdict(node_id, True, "every collected case failed on base and passed on head")


def check(repo_root: Path, base_ref: str, head_ref: str, fix_paths: list[str], node_ids: list[str]) -> list[NodeVerdict]:
    """Judge every node ID independently; never aggregate their outcomes."""
    worktree = _add_worktree(repo_root, head_ref)
    try:
        verdicts = []
        for node_id in node_ids:
            try:
                verdict = judge_node(worktree, base_ref, head_ref, fix_paths, node_id)
            except UnsafePathError as exc:
                verdict = NodeVerdict(node_id, False, f"rejected: {exc}")
            verdicts.append(verdict)
        return verdicts
    finally:
        _remove_worktree(repo_root, worktree)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=ROOT, help="path to the git repository (default: repo root)")
    parser.add_argument("--base", required=True, help="git ref for the pre-fix state")
    parser.add_argument("--head", default="HEAD", help="git ref for the post-fix state (default: HEAD)")
    parser.add_argument(
        "--fix",
        dest="fix_paths",
        action="append",
        required=True,
        help="repo-relative implementation path to revert to --base; may be repeated",
    )
    parser.add_argument("node_ids", nargs="+", help="exact pytest node ids to prove")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    repo_root = args.repo.resolve()
    verdicts = check(repo_root, args.base, args.head, args.fix_paths, args.node_ids)
    for verdict in verdicts:
        status = "confirmed" if verdict.confirmed else "REJECTED"
        print(f"[{status}] {verdict.node_id}: {verdict.reason}")
    confirmed = [v for v in verdicts if v.confirmed]
    print(f"{len(confirmed)}/{len(verdicts)} node id(s) confirmed")
    return 0 if len(confirmed) == len(verdicts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
