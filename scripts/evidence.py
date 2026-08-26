#!/usr/bin/env python3
"""Measure the evidence line instead of remembering it.

Every rejection this repository's PostgreSQL migration produced — six of them
across fifteen slices — was the same defect, and none was a defect in the code:
a sentence claiming more than had been run. Four were test counts written from
memory after a rebase changed them; the others were behavioural claims
("the contract checks X", "each write is covered") that nothing had exercised.

A number a human types is a number that can drift. This tool types it instead:

    uv run python scripts/evidence.py measure tests/db
    -> Evidence: `tests/db` 609 passed / 44 skipped; ruff clean.

and, more usefully, re-measures a claim that already exists so the drift is
found before a reviewer finds it:

    uv run python scripts/evidence.py check --message-from-head tests/db
    -> claims 527 passed / 40 skipped, measured 609 passed / 44 skipped  [MISMATCH]

`check` exits non-zero on a mismatch, so it can gate a push — and it fails
**closed**: a red suite, or a message whose claim it cannot parse, is also a
non-zero exit. The first version returned 0 in both cases, which independent
acceptance demonstrated in three shapes; a gate that passes when it cannot tell
is not a gate, and it is the exact defect class this tool was written to end.

It deliberately does **not** rewrite the message: a wrong number is often the
visible end of a wrong belief, and silently correcting it would hide the part
worth reading.

Deliberately not a pytest plugin and not part of the required suite. This
measures what a *claim* says against what a run says; a suite that measured its
own claims would be the thing being checked.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: `123 passed, 4 skipped` / `123 passed` / `1 failed, 2 passed` — pytest's own
#: summary line, whatever order it puts the terms in.
_SUMMARY = re.compile(r"(?P<count>\d+) (?P<outcome>passed|failed|skipped|errors?|xfailed)")

#: How an evidence sentence in this repository states a measurement. Anchored
#: to the `Evidence:` line rather than matching anywhere in the text: a message
#: that *discusses* counts — a fenced example, a quoted failure, a correction
#: describing what a number used to be — would otherwise be compared against
#: one measured suite and report mismatches that are not claims at all. A gate
#: that always fires is a gate people learn to skip.
_CLAIM = re.compile(
    r"^Evidence:[^\n]*?(?P<passed>\d+)\s+passed\s*[/,]\s*(?P<skipped>\d+)\s+skipped",
    re.MULTILINE,
)



def _pytest_command(paths: list[str], extra: list[str]) -> list[str]:
    """How to invoke pytest here, rather than how I invoke it on my laptop.

    The first version hard-coded `uv run --with …`, which works locally and
    does not exist on a CI runner that installs its dependencies with pip. The
    tool then produced no output at all and its own tests failed on empty
    stdout — a gate that only runs in one environment is not a gate, and this
    one had shipped claiming it could gate a push.

    `uv` is preferred when present because it supplies the optional database
    extras this repository's PostgreSQL tests want; otherwise the interpreter
    already running is exactly the right one.
    """
    if shutil.which("uv"):
        # `pytest` is in the `--with` set deliberately. Without it uv resolves
        # pytest from PATH, the console script re-pins `sys.prefix`, and the
        # extra never reaches the interpreter that runs the tests — review
        # measured exactly that: `HAS psycopg_pool: False` under the form this
        # tool used, `True` once pytest is resolved alongside it. The docstring
        # claimed a benefit the command did not deliver.
        return ["uv", "run", *_DB_EXTRAS, "--with", "pytest", "pytest", *paths, "-q", *extra]
    return [sys.executable, "-m", "pytest", *paths, "-q", *extra]


#: `psycopg` as well as the pool: review set the DSN with the driver absent and
#: every PostgreSQL-backed test skipped **silently** — 393 passed / 260 skipped,
#: exit 0, no FAILED term — under a line saying `PostgreSQL requested`. The
#: pinned extra was the pool, while every skip named the driver, so the tool's
#: own command could not reach a database it claimed to have requested.
#:
#: Named rather than inlined because `_driver_reachable` must probe the exact
#: environment the tests will run in. Two copies of this list would drift, and
#: the drift would be invisible: the probe would answer for an environment
#: nothing runs in.
_DB_EXTRAS = ["--with", "psycopg[binary]>=3.2,<4", "--with", "psycopg-pool>=3.2,<4"]


def _driver_reachable() -> bool:
    """Can the interpreter that runs the tests import `psycopg`?

    This is the whole difference between "a database was asked for" and "a
    database was reached". `tests/db/conftest.py` gates on
    `pytest.importorskip("psycopg")`, so a missing driver turns every
    database-backed test into a silent skip — which is indistinguishable, in a
    summary line, from a suite that has none.
    """
    try:
        return (
            subprocess.run(_driver_probe_command(), cwd=ROOT, capture_output=True).returncode == 0
        )
    except OSError:
        return False


def _driver_probe_command() -> list[str]:
    """The probe's argv, separated from running it.

    Extracted for the same reason `_classify_ruff` was: a test that cannot see
    the command cannot see it drift. Review replaced this function's extras
    with an unrelated package and the test named for exactly that drift passed
    — it was watching `_pytest_command` and nothing else, so it bound one side
    of a two-sided invariant.
    """
    if shutil.which("uv"):
        return ["uv", "run", *_DB_EXTRAS, "python", "-c", "import psycopg"]
    return [sys.executable, "-c", "import psycopg"]


def _run_pytest(paths: list[str], extra: list[str]) -> dict[str, int]:
    """Run pytest and return its own counts, parsed from its own summary.

    The counts come from pytest rather than from a re-implementation of
    pytest's bookkeeping: a second definition of "how many tests passed" is a
    second thing to be wrong.
    """
    completed = subprocess.run(
        _pytest_command(paths, extra), cwd=ROOT, capture_output=True, text=True
    )
    tail = [line for line in completed.stdout.splitlines() if _SUMMARY.search(line)]
    if not tail:
        sys.stderr.write(completed.stdout[-4000:])
        raise SystemExit(f"pytest produced no summary line for {paths}")
    counts = {
        match.group("outcome"): int(match.group("count"))
        for match in _SUMMARY.finditer(tail[-1])
    }
    counts.setdefault("passed", 0)
    counts.setdefault("skipped", 0)
    counts["_returncode"] = completed.returncode
    return counts


def _ruff() -> str:
    """`clean`, `dirty`, or `unavailable` — three answers, not two.

    Collapsing the third into "dirty" made the tool report a lint problem
    where there was only a missing lint tool, and then refuse to exit 0 over
    it. Fail-closed is right for a claim about tests; it is wrong for an
    inability to check something the claim does not assert, and a gate that
    cries about the wrong thing is one people route around.
    """
    ruff = ["uv", "run", "ruff"] if shutil.which("uv") else [sys.executable, "-m", "ruff"]
    try:
        completed = subprocess.run(
            [*ruff, "check", "."], cwd=ROOT, capture_output=True, text=True
        )
    except OSError:
        return "unavailable"
    return _classify_ruff(completed)


def _classify_ruff(completed: "subprocess.CompletedProcess[str]") -> str:
    """The decision itself, separated from the act of running ruff.

    It lives apart from `_ruff` so a test can hand it the four shapes that
    matter without depending on whether the host happens to have ruff on it.
    The test that used to cover this stripped `PATH` and asserted the result
    was not `dirty` — which passes on a host where ruff is importable anyway,
    proving nothing about the branch it names.
    """
    if completed.returncode == 0:
        return "clean"
    # Ruff writes its findings to stdout and its own failures to stderr, and it
    # exits 2 when it could not run at all. The first version matched two
    # literal strings, which only covered the `python -m ruff` wording: review
    # made `uv run` fail to spawn and made ruff reject a config, and both were
    # reported as "**ruff NOT clean**" — a false statement in the one line this
    # tool exists to keep true.
    stderr = completed.stderr.strip()
    if (
        completed.returncode != 1
        or stderr.startswith(("error", "ruff failed"))
        # `python -m ruff` with no ruff installed exits 1 and says so on stderr
        # with no prefix of its own. The previous discriminator covered exactly
        # this wording and the one before it covered only the uv wording; each
        # traded the other's case away. Both are named now, because the point
        # of the third state is that "could not check" never reads as "found
        # problems".
        or "No module named" in stderr
    ):
        return "unavailable"
    return "dirty"


def _evidence_line(paths: list[str], counts: dict[str, int], ruff_state: str) -> str:
    scope = ", ".join(f"`{path}`" for path in paths)
    failed = counts.get("failed", 0) + counts.get("errors", 0) + counts.get("error", 0)
    # Always both terms, even at zero. The first version omitted `0 skipped`
    # while `check`'s pattern required it, so the documented
    # measure -> paste -> check workflow could not round-trip on a suite with
    # no skips: the tool produced a line its own reader called unparseable.
    parts = [f"{counts['passed']} passed", f"{counts['skipped']} skipped"]
    if failed:
        # Stated, never omitted. An evidence line that quietly drops failures
        # is worse than no evidence line, because it reads like a clean run.
        parts.append(f"**{failed} FAILED**")
    suffix = {
        "clean": "; ruff clean",
        "dirty": "; **ruff NOT clean**",
        "unavailable": "; ruff not run here",
    }[ruff_state]
    # The configuration, in the line rather than in a stderr warning nobody
    # pastes. Two honest runs of one tree differ by whether a database was
    # reachable, and the lines were previously indistinguishable.
    #
    # It reports what was *requested*, not what the run achieved. Two earlier
    # versions of this comment claimed that difference could never pose as a
    # clean run, and review broke both: with the DSN set and the driver absent,
    # every database-backed test **skips silently** — no FAILED term, exit 0 —
    # under a line reading `PostgreSQL requested`.
    #
    # The second claim was written in the commit that retracted the first, and
    # was false the same way: it said pinning the driver meant "the tool's own
    # path cannot produce that shape", while the pin exists on only one of
    # `_pytest_command`'s two branches. The pip fallback — the branch that
    # docstring says exists for CI — pins nothing, and review reproduced the
    # 260-silent-skip line through it.
    #
    # So the marker no longer relies on a claim at all. It asks whether the
    # interpreter that runs the tests can import the driver, and says so when
    # it cannot. `requested` still does not promise the database was *reached*
    # — a DSN pointing at a closed port fails loudly rather than skipping — but
    # the one shape that used to pass silently is now named in the line.
    if not os.environ.get("AICC_TEST_PG_ADMIN_DSN"):
        database = "serverless"
    elif _driver_reachable():
        database = "PostgreSQL requested"
    else:
        database = "PostgreSQL requested, **driver missing**"
    return f"Evidence: {scope} " + " / ".join(parts) + f"{suffix} ({database})."


def _head_message() -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    measure = sub.add_parser("measure", help="run the suite and print the evidence line")
    measure.add_argument("paths", nargs="+")

    check = sub.add_parser("check", help="re-measure the claims already in a message")
    check.add_argument("paths", nargs="+")
    source = check.add_mutually_exclusive_group(required=True)
    source.add_argument("--message-from-head", action="store_true")
    source.add_argument("--message-file", type=Path)

    args = parser.parse_args(argv)

    if "AICC_TEST_PG_ADMIN_DSN" not in os.environ:
        # Warned rather than refused: the serverless configuration is a real
        # one and the counts differ there legitimately. What must not happen is
        # a number measured in one configuration being read as the other.
        sys.stderr.write(
            "warning: AICC_TEST_PG_ADMIN_DSN is unset — this measures the "
            "serverless configuration, where PostgreSQL-backed tests skip.\n"
        )

    counts = _run_pytest(args.paths, [])
    ruff_state = _ruff()
    line = _evidence_line(args.paths, counts, ruff_state)
    # A failing suite is a non-zero exit whatever the claim says: the first
    # version compared only the numbers, so a red suite whose `passed`/`skipped`
    # happened to match its claim exited 0 and reported `[ok]` — the tool
    # agreeing with a sentence while the thing it describes was on fire.
    #
    # A dirty `ruff` is reported in the line but does not decide the exit code.
    # This tool answers one question — does the claimed measurement match the
    # measurement — and the repository already has a dedicated lint gate that
    # answers the other one.
    healthy = counts["_returncode"] == 0

    if args.command == "measure":
        print(line)
        return 0 if healthy else 1

    message = (
        _head_message()
        if args.message_from_head
        else args.message_file.read_text(encoding="utf-8")
    )
    claims = list(_CLAIM.finditer(message))
    if not claims:
        # Non-zero, because "I could not find the claim" is not "the claim is
        # right". A message with no `Evidence:` line has nothing for a reviewer
        # to check either, which is itself worth stopping for.
        print(f"no `Evidence: ... N passed / M skipped` line found in the message\n{line}")
        return 1

    if len(claims) > 1:
        # Several `Evidence:` lines mean several measurements, and this run
        # measured one suite. Comparing all of them against it reports a false
        # mismatch for every line that is not the one being measured.
        print(
            f"{len(claims)} `Evidence:` lines found; this run measured one suite "
            "— compare them one at a time rather than trusting the verdict below."
        )

    mismatched = False
    for claim in claims:
        claimed = (int(claim.group("passed")), int(claim.group("skipped")))
        measured = (counts["passed"], counts["skipped"])
        verdict = "ok" if claimed == measured else "MISMATCH"
        mismatched |= claimed != measured
        print(
            f"claims {claimed[0]} passed / {claimed[1]} skipped, "
            f"measured {measured[0]} passed / {measured[1]} skipped  [{verdict}]"
        )
    print(line)
    return 1 if (mismatched or not healthy) else 0


if __name__ == "__main__":
    raise SystemExit(main())
