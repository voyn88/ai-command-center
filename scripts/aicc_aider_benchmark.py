#!/usr/bin/env python3
"""Benchmarks the aider+Ollama executor against a fixed set of low-risk task
fixtures and feeds each case's pass/fail into
`command_center.orchestrator.local_model_gates.record_benchmark_run` under
`orchestrator.routing.BOUNDED_IMPLEMENTATION_TASK_CLASS` -- the ONLY thing
that can move that task class's `aider` link from excluded to offered in
`cascade_for` (AICC Fleet decision 2026-09-03: local-model promotion is
earned from measured runs, never granted on install).

Each case seeds a disposable, throwaway git repository (never a real task
clone), dispatches `agent_runner.run_claude_code` straight at the `aider`
executor -- bypassing the worker's queue entirely, because this script IS
the "is this executor any good yet" harness, not a task dispatch path -- and
verifies the resulting tree against a fixed, EXACT expectation.

"Fixed and exact" is the operative design constraint on every verification
below: a check that accepts a wider result than the task actually asked for
(a substring match instead of the precise value, a partial-field check
instead of the whole document) can record a PASS for a run that got the
task subtly wrong, injecting a false success straight into the promotion
ledger this script exists to feed -- a local model that is confidently,
plausibly wrong is exactly the failure mode a benchmark exists to catch, not
paper over.

Run manually, or from an unattended cron job: `main`'s exit code is 0 only
when every case in the suite passed, so a scheduler can alert on nonzero.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from command_center import agent_runner
from command_center.orchestrator import local_model_gates
from command_center.orchestrator.routing import BOUNDED_IMPLEMENTATION_TASK_CLASS

__all__ = [
    "CASES",
    "BenchmarkCase",
    "CaseResult",
    "run_case",
    "run_suite",
    "main",
]

DEFAULT_TIMEOUT_SECONDS = 300


def _git(args: list[str], cwd: Path) -> None:
    """Run one git step and assert it succeeded.

    An earlier revision of this script ran these with `check=False` and
    discarded the return codes: a missing or misconfigured git on the
    runner would then silently leave `_seed_repo` with a non-git (or
    improperly committed) working tree, and the benchmark would proceed to
    invoke `aider` against it anyway -- recording spurious FAILs across
    every case and quietly poisoning the ledger with an environment problem
    disguised as a model-quality signal. Raising here instead means that
    failure surfaces loudly, before a single sample reaches the ledger.
    """
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed in {cwd}: {result.stderr.strip()}"
        )


def _seed_repo(root: Path, seed_files: dict[str, str]) -> None:
    """A disposable git repository seeded with `seed_files` and one clean
    commit. `aider` requires a git repository to operate in."""
    _git(["init", "--quiet", str(root)], root)
    _git(["config", "--local", "user.name", "AICC Aider Benchmark"], root)
    _git(["config", "--local", "user.email", "aicc-aider-benchmark@localhost"], root)
    for relative_path, content in seed_files.items():
        target = root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(["add", relative_path], root)
    _git(["commit", "--quiet", "-m", "aicc aider benchmark seed"], root)


def _verify_docs_fix_typos(root: Path) -> bool:
    text = (root / "README_SNIPPET.md").read_text(encoding="utf-8")
    return "The quick brown fox jumps over the lazy dog." in text


#: Anchored on both sides (`\b`), NOT a bare `'= 10' in src` substring test:
#: the unanchored form also matches `= 100`, `= 105`, `= 1099`, ... because
#: "= 10" is a literal prefix of every one of those. A correctly-renamed but
#: value-corrupted patch (`MAX_ITEMS = 100` instead of `= 10`) must FAIL this
#: check, not pass it.
_MAX_ITEMS_EXACT_VALUE = re.compile(r"\bMAX_ITEMS\s*=\s*10\b")


def _verify_mechanical_rename_constant(root: Path) -> bool:
    src = (root / "limits.py").read_text(encoding="utf-8")
    return bool(_MAX_ITEMS_EXACT_VALUE.search(src)) and "OLD_LIMIT" not in src


def _verify_fixture_update_golden_value(root: Path) -> bool:
    """Whole-document equality, not "does the new value appear somewhere":
    the prompt asks for exactly one field changed and nothing else, and a
    check that only looks for the new `expected_count` would record a PASS
    even if `label` was corrupted along the way -- the same false-positive
    shape as the unanchored `mechanical-rename-constant` check above.
    """
    path = root / "fixtures" / "golden.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data == {"expected_count": 7, "label": "golden"}


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    name: str
    seed_files: dict[str, str]
    prompt: str
    verify: Callable[[Path], bool]


CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        name="docs-fix-typos",
        seed_files={
            "README_SNIPPET.md": (
                "# Snippet\n\nThe qwick brown fox jumps over the lasy dog.\n"
            ),
        },
        prompt=(
            "In README_SNIPPET.md, fix the two spelling mistakes 'qwick' and "
            "'lasy' so the sentence reads exactly: 'The quick brown fox jumps "
            "over the lazy dog.' Do not change anything else in the file."
        ),
        verify=_verify_docs_fix_typos,
    ),
    BenchmarkCase(
        name="mechanical-rename-constant",
        seed_files={"limits.py": "OLD_LIMIT = 10\n"},
        prompt=(
            "Rename the constant OLD_LIMIT to MAX_ITEMS in limits.py. Do not "
            "change its value or add anything else."
        ),
        verify=_verify_mechanical_rename_constant,
    ),
    BenchmarkCase(
        name="fixture-update-golden-value",
        seed_files={
            "fixtures/golden.json": json.dumps(
                {"expected_count": 5, "label": "golden"}, indent=2
            )
            + "\n",
        },
        prompt=(
            "In fixtures/golden.json, update expected_count from 5 to 7. Do "
            "not change any other field."
        ),
        verify=_verify_fixture_update_golden_value,
    ),
)


@dataclass(frozen=True, slots=True)
class CaseResult:
    name: str
    passed: bool
    detail: str


def run_case(
    case: BenchmarkCase, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
) -> CaseResult:
    """Run one benchmark case in a fresh disposable repository.

    Never raises for an executor failure -- a timeout, a non-zero exit,
    `aider`/`ollama` being unavailable on this host -- all of which fold
    into `passed=False` with `detail` naming what happened, the same
    contract `agent_runner.run_claude_code` documents for itself (verified
    by reading it: an unavailable binary is caught as `OSError` and returned
    as a `status="failed"` `RunResult`, never raised). It does NOT protect
    against a harness defect in this function's own seeding/verification
    code -- that is a bug in the benchmark, not a signal about the model
    under test, and must not be silently absorbed into a sample.
    """
    with tempfile.TemporaryDirectory(
        prefix=f"aicc-aider-bench-{case.name}-"
    ) as raw_root:
        root = Path(raw_root)
        _seed_repo(root, case.seed_files)
        run = agent_runner.run_claude_code(
            repository_path=root,
            prompt=case.prompt,
            task_type="implementation",
            timeout_seconds=timeout_seconds,
            executor="aider",
        )
        if run.status != "completed":
            diagnostic = "\n".join(part for part in (run.stdout, run.stderr) if part)
            detail = diagnostic[-400:] or f"exit_code={run.exit_code!r}"
            return CaseResult(case.name, False, detail)
        try:
            passed = case.verify(root)
        except OSError as exc:
            return CaseResult(
                case.name, False, f"verification could not read the tree: {exc}"
            )
        return CaseResult(case.name, passed, "" if passed else "verification failed")


def run_suite(*, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> list[CaseResult]:
    results = [run_case(case, timeout_seconds=timeout_seconds) for case in CASES]
    for result in results:
        local_model_gates.record_benchmark_run(
            BOUNDED_IMPLEMENTATION_TASK_CLASS, result.passed
        )
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS
    )
    args = parser.parse_args(argv)
    results = run_suite(timeout_seconds=args.timeout_seconds)
    for result in results:
        status = "PASS" if result.passed else "FAIL"
        print(f"{status} {result.name}", file=sys.stderr)
        if not result.passed and result.detail:
            print(f"  {result.detail}", file=sys.stderr)
    failed = [result for result in results if not result.passed]
    print(f"{len(results) - len(failed)}/{len(results)} cases passed", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
