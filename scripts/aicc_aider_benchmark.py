"""Benchmark harness for the aider/Ollama bounded-implementation executor
lane (VOYN-W0-AICC-AIDER-OLLAMA-EXECUTOR).

Drives a handful of disposable fixture repositories through
``agent_runner.run_claude_code(executor="aider", ...)`` and records each
outcome into ``orchestrator.local_model_gates``'s per-task-class promotion
ledger -- the same ledger ``routing.cascade_for`` reads before ever letting
a real dispatch reach ``aider`` (owner decision 2026-09-03: "executor
quality is benchmarked per task class before promotion").

Every fixture below is a MECHANICAL, low-risk edit -- exactly the shape of
task this benchmark exists to gate the free lane onto ("docs, fixtures,
small mechanical patches"). Each case's ``verify`` callable is deliberately
conservative in one specific direction: a false NEGATIVE ("aider did the
right thing but ``verify`` says no") merely under-counts a pass and biases
the ledger pessimistically -- safe. A false POSITIVE ("``verify`` says yes
for a corrupted edit") injects an incorrect success straight into the
ledger that decides whether this lane goes live for real dispatches -- not
safe. Every ``verify`` here is therefore anchored (whole-token regex, not a
bare substring test), because independent review of PR #700 (b280dfc2,
chunk 3/6) found exactly this bug: an unanchored ``'= 10' in src`` check
that also matched ``= 100``/``= 1099``/etc., so a corrupted rename (right
name, wrong value) would still record a PASS.

Run standalone (e.g. from a periodic worker-01 cron job):

    python -m scripts.aicc_aider_benchmark

``--no-record`` runs the suite without writing to the ledger, for a manual
sanity check that aider/Ollama are reachable at all.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from command_center import agent_runner
from command_center.orchestrator import local_model_gates
from command_center.orchestrator.routing import BOUNDED_IMPLEMENTATION_TASK_CLASS

__all__ = ["BenchmarkCase", "CASES", "run_case", "run_suite", "main"]

_OS_SUBPROCESS_ERRORS = (OSError, subprocess.SubprocessError)

#: Bounded and generous for a small mechanical edit against a 14B local
#: model on CPU/GPU-modest worker hardware -- long enough that a normal
#: aider turn never hits it, short enough that a wedged benchmark run does
#: not block the next cron tick indefinitely.
DEFAULT_CASE_TIMEOUT_SECONDS = 300


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    name: str
    task_class: str
    prompt: str
    #: relative path -> seed content, written and committed before the run.
    seed_files: dict[str, str]
    #: Reads the (possibly aider-modified) repo tree and returns pass/fail.
    #: May raise OSError (e.g. a file aider never touched) -- `run_case`
    #: treats that as a fail, not a harness crash.
    verify: Callable[[Path], bool]


def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False
    )


def _seed_repo(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, ["init", "--quiet"])
    _git(root, ["config", "--local", "user.name", "AICC Aider Benchmark"])
    _git(root, ["config", "--local", "user.email", "aicc-aider-benchmark@localhost"])
    _git(root, ["add", "-A"])
    _git(root, ["commit", "--quiet", "-m", "seed"])


# -- fixture: mechanical-rename-constant -------------------------------------
# Anchored with `\b...\b` so a corrupted rename that gets the NAME right but
# the VALUE wrong (`MAX_ITEMS = 100`, `= 105`, `= 1099`, ...) cannot match --
# `\b10\b` requires a non-word boundary immediately after the "10", which a
# following digit never provides. This is the exact bug independent review
# found in the unanchored `'= 10' in src` predecessor of this check.
_MAX_ITEMS_ASSIGNMENT = re.compile(r"\bMAX_ITEMS\s*=\s*10\b")


def _verify_mechanical_rename_constant(repo: Path) -> bool:
    src = (repo / "config.py").read_text(encoding="utf-8")
    return bool(_MAX_ITEMS_ASSIGNMENT.search(src)) and "OLD_LIMIT" not in src


# -- fixture: docs-fix-typos -------------------------------------------------
# Checked as "the typo'd words are gone and the corrected words are present"
# rather than an exact full-sentence match: a reflow/rewrap of the same
# sentence that still fixes both typos must not register as a false FAIL
# (the direction that biases the ledger pessimistically rather than
# recording a real success).
def _verify_docs_fix_typos(repo: Path) -> bool:
    text = (repo / "README_FIXTURE.md").read_text(encoding="utf-8")
    return (
        "recieve" not in text
        and "seperate" not in text
        and "receive" in text
        and "separate" in text
    )


# -- fixture: fixture-update-golden-value ------------------------------------
_GOLDEN_EXPECTED_COUNT = re.compile(r'"expected_count"\s*:\s*7\b')


def _verify_fixture_update_golden_value(repo: Path) -> bool:
    text = (repo / "fixtures" / "golden.json").read_text(encoding="utf-8")
    return bool(_GOLDEN_EXPECTED_COUNT.search(text))


CASES: list[BenchmarkCase] = [
    BenchmarkCase(
        name="mechanical-rename-constant",
        task_class=BOUNDED_IMPLEMENTATION_TASK_CLASS,
        prompt=(
            "In config.py, rename the constant OLD_LIMIT to MAX_ITEMS. "
            "Do not change its value or any other line. Commit the change."
        ),
        seed_files={"config.py": "OLD_LIMIT = 10\n"},
        verify=_verify_mechanical_rename_constant,
    ),
    BenchmarkCase(
        name="docs-fix-typos",
        task_class=BOUNDED_IMPLEMENTATION_TASK_CLASS,
        prompt=(
            "Fix the two spelling mistakes in README_FIXTURE.md: "
            "'recieve' should be 'receive' and 'seperate' should be "
            "'separate'. Do not change anything else. Commit the change."
        ),
        seed_files={
            "README_FIXTURE.md": (
                "Clients recieve a single seperate response per request.\n"
            )
        },
        verify=_verify_docs_fix_typos,
    ),
    BenchmarkCase(
        name="fixture-update-golden-value",
        task_class=BOUNDED_IMPLEMENTATION_TASK_CLASS,
        prompt=(
            "In fixtures/golden.json, update the 'expected_count' field "
            "from 5 to 7. Do not change any other field. Commit the change."
        ),
        seed_files={
            "fixtures/golden.json": '{"expected_count": 5, "label": "golden"}\n'
        },
        verify=_verify_fixture_update_golden_value,
    ),
]


def run_case(
    case: BenchmarkCase, *, timeout_seconds: int = DEFAULT_CASE_TIMEOUT_SECONDS
) -> bool:
    """Run one benchmark case against a fresh disposable repo; return
    pass/fail.

    Never raises for an executor failure (a failed run, a timeout, aider or
    Ollama being unreachable) -- only for a harness defect. That contract
    holds because `agent_runner.run_claude_code` itself never raises for an
    executor failure (its own module docstring/contract: OS errors starting
    the process are caught and turned into a `status="failed"` result), and
    `case.verify` raising `OSError` (the file aider was supposed to touch
    was never created) is caught here and folded into a fail rather than
    propagated.
    """
    with tempfile.TemporaryDirectory(prefix="aicc-aider-bench-") as raw_repo:
        repo = Path(raw_repo)
        _seed_repo(repo, case.seed_files)
        result = agent_runner.run_claude_code(
            repository_path=repo,
            prompt=case.prompt,
            task_type="implementation",
            timeout_seconds=timeout_seconds,
            executor="aider",
        )
        if result.status != "completed":
            return False
        try:
            return bool(case.verify(repo))
        except OSError:
            return False


def run_suite(
    cases: list[BenchmarkCase] | None = None, *, record: bool = True
) -> list[tuple[str, bool]]:
    """Run every case in `cases` (default `CASES`), recording each outcome
    into the promotion ledger unless `record` is False (a dry run)."""
    outcomes: list[tuple[str, bool]] = []
    for case in cases if cases is not None else CASES:
        passed = run_case(case)
        outcomes.append((case.name, passed))
        if record:
            local_model_gates.apply_benchmark_run(
                case.task_class, passed, note=f"aicc_aider_benchmark:{case.name}"
            )
    return outcomes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--no-record",
        action="store_true",
        help="Run the suite without writing to the promotion ledger.",
    )
    args = parser.parse_args(argv)

    available, detail = agent_runner.aider_preflight()
    if not available:
        print(f"aider preflight failed: {detail}", file=sys.stderr)
        return 1

    outcomes = run_suite(record=not args.no_record)
    for name, passed in outcomes:
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    failed = [name for name, passed in outcomes if not passed]
    if failed:
        print(f"{len(failed)}/{len(outcomes)} cases failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
