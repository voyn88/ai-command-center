#!/usr/bin/env python3
"""Prove a test fails on the defect it is named for, instead of trusting the name.

AICC #304 was rejected by independent acceptance three rounds running for one
recurring shape, in three disguises: a false claim in a comment, the same
claim retracted and replaced by a new false claim in the commit that retracted
it, and — the round this tool exists to end — two tests that were *named*
after a defect while passing against it. One asserted on two substrings that
were already present in the unfixed file; the other watched only one side of
a two-sided invariant, so mutating the other side left it green. Both were
caught the same way every time: restore the production code the fix touched
to what it looked like before the fix, keep the test as written, and watch it
either fail (it is evidence) or pass (it is not, whatever its name says).

That restoration was done by hand, once per round. This does it mechanically:

    uv run python scripts/assert_test_catches_named_defect.py \\
        --base origin/main tests/db/test_config.py::test_the_strong_password_is_generated

runs the named test(s) against the current tree — they must already pass, or
there is nothing to check — then runs the *same* test files, unmodified,
against `--base` with every other file reverted to that ref. A test that
still passes there is not bound to the fix; it is bound to something the fix
happened not to disturb, which is exactly how a substring match on a comment
and a one-sided extras check both slipped through review as tests.

This does not decide whether the code is right — `evidence.py` and the normal
suite do that. It decides whether a specific test is the reason to believe it,
which is a narrower question and the one that kept failing here.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

#: pytest's own summary line, whatever order it lists outcomes in — see
#: `evidence.py`, which parses the same line for the same reason: a second
#: definition of "how many tests failed" is a second thing to be wrong.
_SUMMARY = re.compile(r"(?P<count>\d+) (?P<outcome>passed|failed|skipped|errors?|xfailed)")


def _outcome(completed: "subprocess.CompletedProcess[str]") -> dict[str, int]:
    tail = [line for line in completed.stdout.splitlines() if _SUMMARY.search(line)]
    if not tail:
        return {}
    return {match.group("outcome"): int(match.group("count")) for match in _SUMMARY.finditer(tail[-1])}


def _root() -> Path:
    """The repository this run measures — see `mirror_slice_checks.py::_root`
    for why this is an explicit flag rather than a guess: probing a base ref
    from a throwaway worktree makes `parents[1]` point at whichever tree this
    file happens to be copied into, not the one under test."""
    for index, argument in enumerate(sys.argv):
        if argument == "--root" and index + 1 < len(sys.argv):
            return Path(sys.argv[index + 1]).resolve()
    return Path(__file__).resolve().parents[1]


ROOT = _root()


def _pytest_command(node_ids: list[str]) -> list[str]:
    if shutil.which("uv"):
        return ["uv", "run", "pytest", *node_ids, "-q"]
    return [sys.executable, "-m", "pytest", *node_ids, "-q"]


def _run(cwd: Path, node_ids: list[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        _pytest_command(node_ids), cwd=cwd, capture_output=True, text=True
    )


def _carried_files(node_ids: list[str], extra_files: list[str]) -> list[str]:
    """Every file that must move with the test: the file(s) the node ids name,
    plus whatever the caller names explicitly.

    Only the named files move. A shared `conftest.py` or fixture module that
    the fix also touched is not carried unless `--also` names it — carrying
    everything the diff touched would silently drag the fix itself along and
    the test would pass at the base for the same reason it is meant to fail:
    the defect would no longer be there to catch.
    """
    files = sorted({node_id.split("::", 1)[0] for node_id in node_ids})
    files.extend(extra_files)
    seen: list[str] = []
    for path in files:
        if path not in seen:
            seen.append(path)
    for path in seen:
        if not (ROOT / path).is_file():
            raise SystemExit(f"{path}: not a file under {ROOT}")
    return seen


def check(node_ids: list[str], base: str, extra_files: list[str]) -> int:
    carried = _carried_files(node_ids, extra_files)

    here = _run(ROOT, node_ids)
    if here.returncode != 0:
        print(
            f"{' '.join(node_ids)} does not pass at the current head; a test that "
            "cannot pass here is not evidence of anything yet:\n"
        )
        print(here.stdout[-4000:])
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        tree = Path(tmp) / "tree"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(tree), base],
            cwd=ROOT,
            check=True,
            capture_output=True,
        )
        try:
            for path in carried:
                destination = tree / path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / path, destination)
            there = _run(tree, node_ids)
        finally:
            # Removed even if the run above raised: a probe that can leave a
            # stray worktree behind is a probe nobody trusts to run twice.
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(tree)],
                cwd=ROOT,
                check=False,
                capture_output=True,
            )

    if there.returncode == 0:
        print(
            f"with every file but {', '.join(carried)} reverted to {base}, "
            f"{' '.join(node_ids)} still passed:\n"
        )
        print(there.stdout[-4000:])
        print(
            "\nthis test does not fail on the defect it is named for — see AICC #304, "
            "rounds four and five, for the two shapes that looked like coverage and were not."
        )
        return 1

    outcome = _outcome(there)
    failed = outcome.get("failed", 0)
    errored = outcome.get("errors", 0) + outcome.get("error", 0)
    if not failed:
        # A pytest error (setup/collection) is not the same claim as a failed
        # assertion. Treating the two alike is the exact mistake this tool
        # exists to end: a red exit code that is true for the wrong reason is
        # no more evidence than a green one that is true for the wrong reason.
        # The likeliest wrong reason here is a file the test needs that the
        # fix also touched — `--also` carries it; if that is not it, the
        # failure below needs to be read, not trusted.
        reason = f"{errored} error(s)" if errored else "an unparsed result"
        print(
            f"{' '.join(node_ids)} fails at {base}, but with no failed assertion "
            f"— {reason} instead:\n"
        )
        print(there.stdout[-2000:])
        print(
            "\nthat is not evidence the test binds to the fix. If a shared file (a "
            "fixture, a helper module) the test needs was introduced by the same "
            "change, carry it across with --also and try again."
        )
        return 1

    print(f"confirmed: {' '.join(node_ids)} passes at head and fails at {base}:\n")
    print(there.stdout[-2000:])
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("node_ids", nargs="+", help="pytest node id(s), e.g. tests/x.py::test_y")
    parser.add_argument("--base", required=True, help="git ref the fix started from")
    parser.add_argument(
        "--also",
        action="append",
        default=[],
        dest="extra_files",
        metavar="FILE",
        help="an additional file to carry unchanged into the base tree (e.g. a shared fixture)",
    )
    parser.add_argument("--root", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return check(args.node_ids, args.base, args.extra_files)


if __name__ == "__main__":
    raise SystemExit(main())
