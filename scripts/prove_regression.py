#!/usr/bin/env python3
"""Prove that a named test actually fails on the defect it claims to catch.

Why this exists
----------------
"This test catches the defect" is a claim. A claim is not evidence until
something ran and produced the failure — and this repository has already
watched that specific gap survive two rounds of its own review:

Round 1 shipped `check()` so that the base run was accepted whenever *any*
selected test failed. Given two named tests where one correctly failed on the
defect and the other incorrectly passed, the tool printed `confirmed` for
both and exited 0. A strong test concealed an arbitrarily weak one sitting
next to it in the same invocation, which is exactly the class of defect this
tool exists to end.

Round 2 fixed the node-to-node aggregation and recreated the same flaw one
level down: a single node id can select a parametrized function, a class, or
a whole file, which pytest collects as *several* test cases. Treating that
node id as confirmed whenever the base run contained at least one failure let
the other collected cases pass — or stay skipped — unnoticed.

Both rounds trusted a count. A count forgets which specific thing produced
it. This version never asks "did anything fail" at any granularity; it reads
pytest's own per-test-case JUnit report, requires it to be non-empty, and
requires *every* entry in it to be a failure or an error — evaluated
independently for each node id given, one pytest invocation per node id,
never merged with another node id's report and never reduced to a single bit
within its own.

Usage::

    python scripts/prove_regression.py check <base-ref> <node-id> [<node-id> ...]

`<base-ref>` is anything `git rev-parse` accepts (a commit, a tag, `HEAD~1`).
It is checked out into an isolated `git worktree` — the current working tree
and index are never touched — and each `<node-id>` is run there, alone, with
its own JUnit report. A node id "confirms" only when it collected at least
one test case there and every one of them failed or errored. Anything the
tool cannot positively establish — no report, an unreadable report, pytest
exiting with an unrelated code, zero collected cases, one passing or skipped
case sitting among several failures — is refused, not guessed at.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]

#: Outcomes that count as "the test died on the defect", as opposed to
#: quietly declining to run (skipped) or agreeing with the defect (passed).
_DIED = ("failed", "error")


class ProofError(RuntimeError):
    """The requested node id cannot be shown to catch its defect."""


@dataclass(frozen=True)
class CaseResult:
    """One `<testcase>` from a JUnit report, reduced to what this tool checks."""

    node_id: str
    status: str  # "failed" | "error" | "skipped" | "passed"


@dataclass(frozen=True)
class NodeVerdict:
    """Every case pytest collected for one requested node id, at the base ref.

    Confirmation is a property of the whole tuple, not of any single case in
    it: one passing or skipped case among several failures must sink the
    verdict for this node id exactly as surely as an all-passing node id
    would, because a reviewer reading `confirmed` needs it to mean every
    collected case failed, not merely that failure occurred somewhere.
    """

    node_id: str
    cases: tuple[CaseResult, ...]

    @property
    def confirmed(self) -> bool:
        return bool(self.cases) and all(case.status in _DIED for case in self.cases)

    @property
    def reason(self) -> str:
        if not self.cases:
            return f"`{self.node_id}` collected no test cases at the base commit"
        survivors = [case for case in self.cases if case.status not in _DIED]
        named = ", ".join(f"{case.node_id} ({case.status})" for case in survivors)
        return (
            f"`{self.node_id}` collected {len(self.cases)} case(s) at the base "
            f"commit; {len(survivors)} did not fail there: {named}"
        )


def parse_junit(xml_text: str) -> tuple[CaseResult, ...]:
    """Every `<testcase>` in a JUnit report, whatever pytest nested it under.

    Read case-by-case rather than from pytest's own `N passed, M failed`
    summary line: the summary is exactly the count that let a parametrized
    case, or a second node id, hide inside an aggregate that looked right.
    """
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as error:
        raise ProofError(f"the JUnit report is not valid XML: {error}") from error
    results = []
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        name = case.get("name") or ""
        node_id = f"{classname}::{name}" if classname else name
        if case.find("failure") is not None:
            status = "failed"
        elif case.find("error") is not None:
            status = "error"
        elif case.find("skipped") is not None:
            status = "skipped"
        else:
            status = "passed"
        results.append(CaseResult(node_id=node_id, status=status))
    return tuple(results)


def _resolve_commit(ref: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise ProofError(
            f"`{ref}` does not resolve to a commit: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


@contextmanager
def _base_worktree(commit: str) -> Iterator[Path]:
    """An isolated checkout of `commit`, never touching this checkout's tree."""
    with tempfile.TemporaryDirectory(prefix="prove-regression-") as tmp:
        path = Path(tmp) / "worktree"
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(path), commit],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if added.returncode != 0:
            raise ProofError(
                f"could not check out {commit} in an isolated worktree: "
                f"{added.stderr.strip()}"
            )
        try:
            yield path
        finally:
            # `--force`: pytest leaves caches and `__pycache__` behind as
            # untracked files, which `git worktree remove` otherwise refuses
            # to remove. This cleans up regardless of what a test run left.
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            subprocess.run(
                ["git", "worktree", "prune"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )


def _run_node_id(worktree: Path, node_id: str) -> NodeVerdict:
    """Run exactly one node id, alone, and return every case it collected.

    One pytest invocation per node id — not one invocation for the whole
    request — so that node id B's report can never absorb node id A's
    failure and read as A's proof.
    """
    report = worktree / f".prove-regression-{uuid.uuid4().hex}.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        node_id,
        "-q",
        f"--junitxml={report}",
    ]
    completed = subprocess.run(
        command, cwd=worktree, capture_output=True, text=True, check=False
    )
    # 0 = every collected case passed (not proof of a defect); 1 = at least
    # one collected case failed, which is the only code whose report this
    # tool trusts. Every other code (2 interrupted, 3 internal error, 4 usage
    # error, 5 no tests collected) means pytest could not answer the question
    # at all, and "could not answer" is refused rather than read as failure.
    if completed.returncode not in (0, 1):
        tail = (completed.stdout + completed.stderr)[-2000:]
        raise ProofError(
            f"pytest could not run `{node_id}` at the base commit "
            f"(exit {completed.returncode}):\n{tail}"
        )
    if not report.exists():
        raise ProofError(f"pytest produced no JUnit report for `{node_id}`")
    try:
        cases = parse_junit(report.read_text(encoding="utf-8"))
    finally:
        report.unlink(missing_ok=True)
    return NodeVerdict(node_id=node_id, cases=cases)


def check(base_ref: str, node_ids: list[str]) -> dict[str, NodeVerdict]:
    """Return one independent verdict per node id. Never merges them.

    Confirming the whole request is the caller's job, and the caller must
    require every entry's `.confirmed` to be true — this function does not
    collapse the map into a bool, because that collapse is precisely where
    the aggregate-outcome defect lived twice already.
    """
    if not node_ids:
        raise ProofError("at least one pytest node id is required")
    commit = _resolve_commit(base_ref)
    verdicts: dict[str, NodeVerdict] = {}
    with _base_worktree(commit) as worktree:
        for node_id in node_ids:
            verdicts[node_id] = _run_node_id(worktree, node_id)
    return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check_parser = sub.add_parser(
        "check", help="prove each node id fails, in full, at the base ref"
    )
    check_parser.add_argument("base_ref")
    check_parser.add_argument("node_ids", nargs="+", metavar="node_id")

    args = parser.parse_args(argv)

    try:
        verdicts = check(args.base_ref, args.node_ids)
    except ProofError as error:
        print(f"regression proof refused: {error}", file=sys.stderr)
        return 1

    # Every node id is reported on its own line and judged on its own
    # verdict: nothing here shortcuts to "something failed somewhere", which
    # is the sentence that let a weak test hide next to a strong one.
    all_confirmed = True
    for node_id in args.node_ids:
        verdict = verdicts[node_id]
        if verdict.confirmed:
            print(f"confirmed: `{node_id}` fails in full ({len(verdict.cases)} case(s)) at {args.base_ref}")
        else:
            all_confirmed = False
            print(f"NOT confirmed: {verdict.reason}", file=sys.stderr)

    return 0 if all_confirmed else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
