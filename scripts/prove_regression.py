#!/usr/bin/env python3
"""Prove that a named test actually fails on the defect it claims to catch.

Why this exists
----------------
"This test catches the defect" is a claim. A claim is not evidence until
something ran and produced the failure — and this repository has already
watched that specific gap survive three rounds of its own review:

Round 1 shipped `check()` so that the base run was accepted whenever *any*
selected test failed. Given two named tests where one correctly failed on the
defect and the other incorrectly passed, the tool printed `confirmed` for
both and exited 0. A strong test concealed an arbitrarily weak one sitting
next to it in the same invocation.

Round 2 fixed that aggregation and recreated the same flaw one level down: a
single node id can select a parametrized function, a class, or a whole file,
which pytest collects as *several* test cases. Treating that node id as
confirmed whenever the base run contained at least one failure let the other
collected cases pass — or stay skipped — unnoticed.

Round 3 fixed both aggregations — every case, of every node id, evaluated on
its own — and still had two ways to lie:

* It ran each node id at the base commit exactly as that commit's own tree
  had it. A regression test the pull request adds, or rewrites, does not
  exist yet at the base commit, or exists there with its old, weaker
  assertions. Running the base commit's version of the test answers a
  question nobody asked; the tool refused the very evidence it exists to
  validate.
* It read "every collected case failed or errored" as proof, full stop. A
  fixture that raises because a dependency is missing, or a module that
  fails to import, produces an all-error report with nothing to do with the
  defect. That report satisfied "every case died" and was reported
  `confirmed` regardless.

This version fixes both. For each node id it runs the *current* tree's copy
of that node id's own test file — transplanted onto the base commit's
implementation, in an isolated worktree — and requires every collected case
to fail or error there. Then it runs the same node id, untouched, against
the current tree (implementation and test both) and requires every collected
case there to pass, and requires the two runs to have collected exactly the
same set of cases. A defect-unrelated error reproduces on both sides of that
comparison and is refused; only a case that dies against the old
implementation and passes against the fixed one counts as proof.

Usage::

    python scripts/prove_regression.py check <base-ref> <node-id> [<node-id> ...]

`<base-ref>` is anything `git rev-parse` accepts (a commit, a tag, `HEAD~1`),
resolved against the git repository containing the current working
directory. It is checked out into an isolated `git worktree` — the current
working tree and index are never touched — with each `<node-id>`'s own test
file copied from the current tree onto that worktree before anything runs
there. Each `<node-id>` then runs twice: once alone in that worktree, once
alone against the current tree. A node id "confirms" only when both runs
collected the identical, non-empty set of cases, every case failed or
errored at the base, and every case passed against the current tree.
Anything the tool cannot positively establish is refused, not guessed at.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional
from xml.etree import ElementTree

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


def _repo_root(start: Path) -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ProofError(
            f"`{start}` is not inside a git repository: {completed.stderr.strip()}"
        )
    return Path(completed.stdout.strip())


def _resolve_commit(root: Path, ref: str) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise ProofError(
            f"`{ref}` does not resolve to a commit: {completed.stderr.strip()}"
        )
    return completed.stdout.strip()


def _test_file(node_id: str) -> str:
    """The file-path component of a pytest node id.

    `a/b.py::TestX::test_y[z]` collects at `a/b.py`; a bare `a/b.py` (or a
    directory) is its own file-path component.
    """
    return node_id.split("::", 1)[0]


@contextmanager
def _base_worktree(root: Path, commit: str) -> Iterator[Path]:
    """An isolated checkout of `commit`, never touching this checkout's tree."""
    with tempfile.TemporaryDirectory(prefix="prove-regression-") as tmp:
        path = Path(tmp) / "worktree"
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(path), commit],
            cwd=root,
            capture_output=True,
            text=True,
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
                cwd=root,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "worktree", "prune"], cwd=root, capture_output=True, text=True
            )


def _transplant(root: Path, worktree: Path, node_ids: list[str]) -> None:
    """Copy each node id's own test file from the current tree onto `worktree`.

    A regression test is proof about the defect that existed *before* the
    fix, not about whatever that file happened to contain at the base
    commit. If the pull request added the test, or rewrote its assertions,
    the base commit has no test — or a weaker one — at that path. Running
    the node id as the base commit wrote it answers a question nobody asked.
    This overwrites (or creates) each referenced file in the worktree with
    the current tree's copy before anything runs there, so the base run
    exercises the current test body against the base implementation, which
    is the comparison a regression proof actually requires.
    """
    seen: set[str] = set()
    for node_id in node_ids:
        rel = _test_file(node_id)
        if rel in seen:
            continue
        seen.add(rel)
        src = root / rel
        if not src.is_file():
            raise ProofError(
                f"`{rel}` does not exist in the current tree to transplant onto "
                "the base commit"
            )
        dst = worktree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())


def _run_node_id(cwd: Path, node_id: str) -> tuple[CaseResult, ...]:
    """Run exactly one node id, alone, and return every case it collected.

    One pytest invocation per node id, per side of the comparison — never one
    invocation for the whole request — so that node id B's report, or the
    base side's report, can never absorb another report's failure and read
    as this one's proof.
    """
    with tempfile.TemporaryDirectory(prefix="prove-regression-report-") as tmp:
        report = Path(tmp) / f"{uuid.uuid4().hex}.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            node_id,
            "-q",
            f"--junitxml={report}",
        ]
        completed = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
        # 0 = every collected case passed; 1 = at least one collected case
        # failed or errored during the run. Both are reports this tool
        # trusts. Every other code (2 collection interrupted/errored, 3
        # internal error, 4 usage error, 5 no tests collected) means pytest
        # could not answer the question at all — including a bare import
        # failure while collecting the test module — and is refused rather
        # than read as a defect-shaped failure.
        if completed.returncode not in (0, 1):
            tail = (completed.stdout + completed.stderr)[-2000:]
            raise ProofError(
                f"pytest could not run `{node_id}` in {cwd} "
                f"(exit {completed.returncode}):\n{tail}"
            )
        if not report.exists():
            raise ProofError(f"pytest produced no JUnit report for `{node_id}` in {cwd}")
        return parse_junit(report.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class NodeVerdict:
    """Both sides of the comparison for one requested node id.

    Confirmation is a property of the whole pair, not of either side alone:
    a case that dies at the base proves nothing by itself (it might die for
    a reason that has nothing to do with the defect, and would die exactly
    the same way against the fix); a case that passes against the fix proves
    nothing by itself either (a test can pass unconditionally). Only a case
    that dies without the fix and passes with it — the same case, identified
    the same way, on both sides — is evidence.
    """

    node_id: str
    base_cases: tuple[CaseResult, ...]
    head_cases: tuple[CaseResult, ...]

    @property
    def _base_ids(self) -> frozenset[str]:
        return frozenset(case.node_id for case in self.base_cases)

    @property
    def _head_ids(self) -> frozenset[str]:
        return frozenset(case.node_id for case in self.head_cases)

    @property
    def confirmed(self) -> bool:
        if not self.base_cases or not self.head_cases:
            return False
        if self._base_ids != self._head_ids:
            return False
        return all(case.status in _DIED for case in self.base_cases) and all(
            case.status == "passed" for case in self.head_cases
        )

    @property
    def reason(self) -> str:
        if not self.base_cases:
            return f"`{self.node_id}` collected no test cases against the base commit"
        if not self.head_cases:
            return f"`{self.node_id}` collected no test cases against the current tree"
        if self._base_ids != self._head_ids:
            return (
                f"`{self.node_id}` collected different cases at the base commit "
                f"({sorted(self._base_ids)}) than in the current tree "
                f"({sorted(self._head_ids)}); the two runs are not comparable"
            )
        base_survivors = [c for c in self.base_cases if c.status not in _DIED]
        if base_survivors:
            named = ", ".join(f"{c.node_id} ({c.status})" for c in base_survivors)
            return f"`{self.node_id}` did not fail in full against the base commit: {named}"
        head_failures = [c for c in self.head_cases if c.status != "passed"]
        named = ", ".join(f"{c.node_id} ({c.status})" for c in head_failures)
        return (
            f"`{self.node_id}` fails in full against the base commit, but does not "
            f"pass cleanly against the current tree, so the failure cannot be "
            f"attributed to the defect alone: {named}"
        )


def check(
    base_ref: str, node_ids: list[str], root: Optional[Path] = None
) -> dict[str, NodeVerdict]:
    """Return one independent verdict per node id. Never merges them.

    Confirming the whole request is the caller's job, and the caller must
    require every entry's `.confirmed` to be true — this function does not
    collapse the map into a bool, because that collapse is precisely where
    the aggregate-outcome defect lived twice already.
    """
    if not node_ids:
        raise ProofError("at least one pytest node id is required")
    root = root if root is not None else _repo_root(Path.cwd())
    commit = _resolve_commit(root, base_ref)
    base_results: dict[str, tuple[CaseResult, ...]] = {}
    with _base_worktree(root, commit) as worktree:
        _transplant(root, worktree, node_ids)
        for node_id in node_ids:
            base_results[node_id] = _run_node_id(worktree, node_id)
    # The head run happens against the caller's own tree only after the
    # worktree (and its transplanted files) has been torn down, so nothing
    # here can be mistaken for, or contaminate, the base measurement above.
    verdicts: dict[str, NodeVerdict] = {}
    for node_id in node_ids:
        head_cases = _run_node_id(root, node_id)
        verdicts[node_id] = NodeVerdict(
            node_id=node_id, base_cases=base_results[node_id], head_cases=head_cases
        )
    return verdicts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    check_parser = sub.add_parser(
        "check", help="prove each node id fails, in full, on the defect at the base ref"
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
            print(
                f"confirmed: `{node_id}` fails in full ({len(verdict.base_cases)} "
                f"case(s)) at {args.base_ref} and passes in full in the current tree"
            )
        else:
            all_confirmed = False
            print(f"NOT confirmed: {verdict.reason}", file=sys.stderr)

    return 0 if all_confirmed else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
