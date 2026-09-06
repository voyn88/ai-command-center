#!/usr/bin/python3
"""Benchmark suite for the aider+Ollama bounded-implementation executor lane
(VOYN-W0-AICC-AIDER-OLLAMA-EXECUTOR).

Owner decision (2026-09-03): before `orchestrator.routing` ever offers the
free `aider` link for the `bounded_implementation` task class, that class
must be "benchmarked ... before promotion". This script is the benchmark:
each `BenchmarkCase` seeds a disposable, throwaway git repository with fixed
fixture content, drives the aider executor over it through the SAME
`agent_runner.run_claude_code` entry point the worker daemon uses, and
verifies the result with a plain shell/Python check -- the identical kind of
mechanical, machine-checkable gate `docs`/`fixtures`/small-patch tasks
actually get in CI. A case passes iff the executor run completed AND the
verification command exits zero; either failure records a benchmark loss.

Every case's outcome is folded into `orchestrator.local_model_gates`
one call at a time (`record_benchmark_run`), so the promotion ledger is
exact accumulated evidence -- this script has no opinion of its own about
whether the lane is "good enough"; the ledger's sample-size/pass-rate bar
decides that, the same way for every task class, every time it is run again.

Run against a real `aider` + local `ollama` install (`aicc_aider_benchmark.py
run`); `status`/`demote` only touch the ledger and need neither.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from command_center import agent_runner  # noqa: E402
from command_center.orchestrator import local_model_gates  # noqa: E402

#: The task class this whole script benchmarks. A second script would exist
#: for a second class -- this one is not parametrized over task classes
#: because "docs, fixtures, small mechanical patches" already collapse into
#: one bounded-implementation lane in `orchestrator.routing.ROUTING_MATRIX`.
TASK_CLASS = "bounded_implementation"

DEFAULT_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    #: relative path -> seed content, committed before the executor runs.
    seed_files: dict[str, str]
    prompt: str
    #: argv run inside the resulting worktree; exit 0 means the case passed.
    verify: tuple[str, ...]


CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        name="docs-fix-typos",
        seed_files={
            "README.md": (
                "# Demo\n\nThsi is a smal fixture repo used to benchmark the "
                "bounded-implementation lane.\n"
            )
        },
        prompt=(
            "Fix the two spelling mistakes in README.md: 'Thsi' should be "
            "'This' and 'smal' should be 'small'. Do not change anything "
            "else in the file."
        ),
        verify=(
            "grep",
            "-q",
            "This is a small fixture repo used to benchmark the "
            "bounded-implementation lane.",
            "README.md",
        ),
    ),
    BenchmarkCase(
        name="fixture-add-json-field",
        seed_files={"fixtures/user.json": '{\n  "id": 1,\n  "name": "Ada"\n}\n'},
        prompt=(
            'Add an "active": true field to fixtures/user.json. Keep the '
            "file valid JSON and do not touch the existing fields."
        ),
        verify=(
            sys.executable,
            "-c",
            "import json,sys\n"
            "d = json.load(open('fixtures/user.json'))\n"
            "sys.exit(0 if d.get('id') == 1 and d.get('name') == 'Ada' and d.get('active') is True else 1)\n",
        ),
    ),
    BenchmarkCase(
        name="mechanical-rename-constant",
        seed_files={
            "app.py": (
                "OLD_LIMIT = 10\n\n\ndef cap(value):\n    return min(value, OLD_LIMIT)\n"
            )
        },
        prompt=(
            "Rename the constant OLD_LIMIT to MAX_ITEMS everywhere in app.py "
            "(its definition and every use). Do not change the value or any "
            "other logic."
        ),
        verify=(
            sys.executable,
            "-c",
            "import sys\n"
            "src = open('app.py').read()\n"
            "sys.exit(0 if 'MAX_ITEMS' in src and 'OLD_LIMIT' not in src and '= 10' in src else 1)\n",
        ),
    ),
)


def _run_git(argv: tuple[str, ...], cwd: Path) -> None:
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(argv)} failed: {result.stderr.strip()}")


def _seed_repo(root: Path, case: BenchmarkCase) -> None:
    for relative_path, content in case.seed_files.items():
        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _run_git(("git", "init", "--quiet"), root)
    _run_git(("git", "config", "user.email", "aicc-aider-benchmark@localhost"), root)
    _run_git(("git", "config", "user.name", "AICC Aider Benchmark"), root)
    _run_git(("git", "add", "-A"), root)
    _run_git(("git", "commit", "--quiet", "-m", "seed"), root)


def run_case(case: BenchmarkCase, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> bool:
    """Seed a disposable worktree, drive aider over it, and verify the
    result. Returns whether the case passed -- never raises for an executor
    failure, only for a harness defect (a seed/verify command that cannot
    even be attempted), so one bad case cannot abort the whole suite."""
    with tempfile.TemporaryDirectory(prefix=f"aicc-aider-bench-{case.name}-") as raw:
        root = Path(raw)
        _seed_repo(root, case)
        result = agent_runner.run_claude_code(
            repository_path=root,
            prompt=case.prompt,
            task_type="implementation",
            timeout_seconds=timeout_seconds,
            executor="aider",
        )
        if result.status != "completed":
            return False
        verification = subprocess.run(
            case.verify, cwd=root, capture_output=True, text=True, check=False
        )
        return verification.returncode == 0


def run_suite(
    cases: tuple[BenchmarkCase, ...] = CASES,
    *,
    root: Path | None = None,
    actor: str | None = None,
) -> dict[str, bool]:
    """Run every case and fold each outcome into the promotion ledger one at
    a time (not as one batched write) -- the ledger's sample count must
    reflect exactly the runs actually attempted even if a later case in the
    suite raises."""
    resolved_root = root if root is not None else agent_runner.ROOT
    outcomes: dict[str, bool] = {}
    for case in cases:
        passed = run_case(case)
        outcomes[case.name] = passed
        local_model_gates.record_benchmark_run(
            resolved_root, TASK_CLASS, passed=passed, actor=actor
        )
    return outcomes


def _print_status(root: Path) -> None:
    record = local_model_gates.load_gates(root).get(
        TASK_CLASS, local_model_gates.GateRecord(task_class=TASK_CLASS)
    )
    print(
        f"{TASK_CLASS}: promoted={record.promoted} "
        f"sample_count={record.sample_count} pass_count={record.pass_count} "
        f"pass_rate={record.pass_rate:.0%} "
        f"(bar: >={local_model_gates.MIN_BENCHMARK_SAMPLES} samples, "
        f">={local_model_gates.MIN_PASS_RATE:.0%} pass rate)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Repo root whose data/ dir holds the promotion ledger (default: this checkout).",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("run", help="Run the fixed benchmark suite and record results.")
    sub.add_parser("status", help="Print the current promotion ledger entry.")
    demote_parser = sub.add_parser(
        "demote", help="Explicitly revoke promotion (regression / incident)."
    )
    demote_parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    root = args.root if args.root is not None else agent_runner.ROOT

    if args.action == "run":
        outcomes = run_suite(root=root)
        passed = sum(1 for ok in outcomes.values() if ok)
        print(f"{passed}/{len(outcomes)} cases passed this run:")
        for name, ok in outcomes.items():
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        _print_status(root)
        return 0 if passed == len(outcomes) else 1
    if args.action == "status":
        _print_status(root)
        return 0
    record = local_model_gates.demote(root, TASK_CLASS, reason=args.reason)
    print(f"demoted {TASK_CLASS}: {record.notes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
