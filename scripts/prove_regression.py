#!/usr/bin/env python3
"""Prove that specific tests actually catch the defect they claim to catch.

Four rounds of adversarial review rejected earlier versions of this tool for
the same underlying class of mistake: it accepted "some evidence, somewhere"
as proof of "this specific test catches this specific defect". Concretely,
past versions:

  1. Ran several node ids in one pytest invocation and called the whole run
     "confirmed" if *any* selected test failed at the base commit — a strong
     test could hide an arbitrarily weak sibling.
  2. Even for a single node id, accepted the run as "confirmed" if *any*
     collected case (out of a parametrized set, a class, or a whole file)
     failed at the base commit, letting untouched passing cases ride along.
  3. Ran the requested node id inside a worktree checked out at the base
     commit, so a test (or fixture) added or changed by the fix under test
     did not exist there yet, or ran a stale copy of itself.
  4. Treated pytest "error" outcomes (import failures, fixture setup
     failures, missing services) the same as "failure" outcomes (assertion
     failures), so a broken environment looked identical to proof.
  5. Fixed (3) by copying only the changed *test file* into the base
     worktree, leaving conftest.py, fixtures, plugins, and test helper
     modules stale — and compared collected test ids as a ``frozenset``,
     which silently discards duplicate-count mismatches.

This tool is built to make all five structurally impossible rather than
patching each as a special case:

  - Each requested node id is run, parsed, and judged completely
    independently (fixes #1: no cross-node aggregation).
  - A node id is confirmed only if *every* case pytest collects under it
    fails at the base and *every* case passes at the head (fixes #2: no
    within-node aggregation). This also means callers do not need to pass
    exact leaf node ids — a whole parametrized function, class, or file is
    fine, as long as it collects at least one case and all of them agree.
  - The base run is built by checking out the *head* commit in full, then
    reverting only the caller-specified ``--impl`` paths to their base-commit
    content. Every other file — the test itself, conftest.py, fixtures,
    plugins, helper modules, anything — is whatever the head commit says it
    is. There is nothing left to separately "transplant" (fixes #3 and #5's
    first half): the base worktree *is* the head worktree, minus exactly the
    files the caller claims constitute the fix.
  - A case counts as proof of the defect only when pytest reports it as a
    JUnit ``<failure>`` (an assertion did not hold). A ``<error>`` (import
    error, fixture setup exception, ...) is never proof of anything and is
    always rejected (fixes #4).
  - Collected case ids are compared between base and head runs with
    ``collections.Counter``, which is sensitive to how many times each id
    was collected, not just which ids appeared (fixes #5's second half).

Usage:

    python scripts/prove_regression.py \\
        --repo . --base <sha-before-fix> --head <sha-after-fix> \\
        --impl command_center/foo.py --impl command_center/bar.py \\
        tests/test_foo.py::test_regression tests/test_bar.py::TestBar

Exits 0 only if every requested node id is confirmed. Prints a JSON object
mapping each node id to its verdict and the reasoning behind it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

FAILED = "failed"
ERROR = "error"
SKIPPED = "skipped"
PASSED = "passed"


@dataclass(frozen=True)
class CaseResult:
    testcase_id: str
    outcome: str


@dataclass(frozen=True)
class RunResult:
    node_id: str
    exit_code: int
    cases: tuple[CaseResult, ...]
    stdout: str
    stderr: str
    junit_missing: bool = False


@dataclass(frozen=True)
class NodeVerdict:
    node_id: str
    confirmed: bool
    reason: str


class ProveRegressionError(RuntimeError):
    """Raised for setup failures (bad shas, git errors) unrelated to any node id's verdict."""


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
    )


@contextlib.contextmanager
def _git_worktree(repo: Path, sha: str):
    tmp = Path(tempfile.mkdtemp(prefix="prove_regression_wt_"))
    tmp.rmdir()  # git worktree add refuses to reuse an existing directory
    proc = _run_git(repo, "worktree", "add", "--detach", str(tmp), sha)
    if proc.returncode != 0:
        raise ProveRegressionError(
            f"failed to create worktree for {sha!r}: {proc.stderr.decode(errors='replace')}"
        )
    try:
        yield tmp
    finally:
        _run_git(repo, "worktree", "remove", "--force", str(tmp))
        shutil.rmtree(tmp, ignore_errors=True)


def _revert_path_to_base(repo: Path, worktree: Path, base_sha: str, rel_path: str) -> None:
    target = worktree / rel_path
    proc = _run_git(repo, "show", f"{base_sha}:{rel_path}")
    if proc.returncode != 0:
        # The path did not exist at base_sha (it was added by the fix) -- it
        # must not exist in the reconstructed base worktree either.
        if target.exists():
            target.unlink()
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(proc.stdout)


@contextlib.contextmanager
def _base_worktree(repo: Path, base_sha: str, head_sha: str, impl_paths: list[str]):
    with _git_worktree(repo, head_sha) as wt:
        for rel_path in impl_paths:
            _revert_path_to_base(repo, wt, base_sha, rel_path)
        yield wt


def _parse_junit(path: Path) -> tuple[CaseResult, ...]:
    tree = ElementTree.parse(path)
    cases = []
    for testcase in tree.getroot().iter("testcase"):
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        testcase_id = f"{classname}::{name}" if classname else name
        if testcase.find("error") is not None:
            outcome = ERROR
        elif testcase.find("failure") is not None:
            outcome = FAILED
        elif testcase.find("skipped") is not None:
            outcome = SKIPPED
        else:
            outcome = PASSED
        cases.append(CaseResult(testcase_id=testcase_id, outcome=outcome))
    return tuple(cases)


def _run_node_id(worktree: Path, node_id: str) -> RunResult:
    junit_path = worktree / "._prove_regression_junit.xml"
    if junit_path.exists():
        junit_path.unlink()
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
    if not junit_path.exists():
        return RunResult(
            node_id=node_id,
            exit_code=proc.returncode,
            cases=(),
            stdout=proc.stdout,
            stderr=proc.stderr,
            junit_missing=True,
        )
    cases = _parse_junit(junit_path)
    junit_path.unlink(missing_ok=True)
    return RunResult(
        node_id=node_id,
        exit_code=proc.returncode,
        cases=cases,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )


def _describe(cases: list[CaseResult]) -> str:
    return ", ".join(f"{c.testcase_id}={c.outcome}" for c in cases)


def judge_node(node_id: str, base_run: RunResult, head_run: RunResult) -> NodeVerdict:
    """Pure judging logic over two already-executed runs. Kept separate from
    process/git plumbing so every rejection reason can be unit tested against
    fabricated results, not only against real pytest subprocesses."""

    if base_run.junit_missing:
        return NodeVerdict(
            node_id, False,
            f"base run produced no JUnit report (exit code {base_run.exit_code}); "
            f"cannot tell whether the node id resolved to any test",
        )
    if head_run.junit_missing:
        return NodeVerdict(
            node_id, False,
            f"head run produced no JUnit report (exit code {head_run.exit_code}); "
            f"cannot tell whether the node id resolved to any test",
        )
    if not base_run.cases:
        return NodeVerdict(node_id, False, f"collected 0 test cases at base for {node_id!r}")
    if not head_run.cases:
        return NodeVerdict(node_id, False, f"collected 0 test cases at head for {node_id!r}")

    base_counts = Counter(c.testcase_id for c in base_run.cases)
    head_counts = Counter(c.testcase_id for c in head_run.cases)
    if base_counts != head_counts:
        return NodeVerdict(
            node_id, False,
            "collected test cases differ between base and head runs "
            f"(base: {dict(base_counts)}, head: {dict(head_counts)})",
        )

    not_failed_at_base = [c for c in base_run.cases if c.outcome != FAILED]
    if not_failed_at_base:
        return NodeVerdict(
            node_id, False,
            "not every collected case failed at base "
            f"({_describe(not_failed_at_base)}) -- an 'error' or a pass is not proof of the defect",
        )

    not_passed_at_head = [c for c in head_run.cases if c.outcome != PASSED]
    if not_passed_at_head:
        return NodeVerdict(
            node_id, False,
            f"not every collected case passed at head ({_describe(not_passed_at_head)})",
        )

    return NodeVerdict(
        node_id, True,
        f"all {len(base_run.cases)} collected case(s) failed at base and passed at head",
    )


def check(
    repo: str | Path,
    base_sha: str,
    head_sha: str,
    impl_paths: list[str],
    node_ids: list[str],
) -> dict[str, NodeVerdict]:
    repo = Path(repo).resolve()
    verdicts: dict[str, NodeVerdict] = {}
    with _git_worktree(repo, head_sha) as head_wt, \
            _base_worktree(repo, base_sha, head_sha, impl_paths) as base_wt:
        for node_id in node_ids:
            base_run = _run_node_id(base_wt, node_id)
            head_run = _run_node_id(head_wt, node_id)
            verdicts[node_id] = judge_node(node_id, base_run, head_run)
    return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", default=".", help="path to the git repository (default: cwd)")
    parser.add_argument("--base", required=True, help="commit sha before the fix (defect present)")
    parser.add_argument("--head", required=True, help="commit sha after the fix (defect resolved)")
    parser.add_argument(
        "--impl",
        action="append",
        dest="impl_paths",
        required=True,
        help="repo-relative path that constitutes the fix; reverted to --base content when "
             "building the base run. Repeat for multiple files.",
    )
    parser.add_argument("node_ids", nargs="+", help="pytest node ids to prove, each judged independently")
    args = parser.parse_args(argv)

    try:
        verdicts = check(args.repo, args.base, args.head, args.impl_paths, args.node_ids)
    except ProveRegressionError as exc:
        print(json.dumps({"error": str(exc)}, indent=2))
        return 1

    payload = {
        node_id: {"confirmed": v.confirmed, "reason": v.reason}
        for node_id, v in verdicts.items()
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if all(v.confirmed for v in verdicts.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
