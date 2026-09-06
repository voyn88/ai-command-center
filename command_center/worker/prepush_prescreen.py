"""Worker-side pre-push prescreen (VOYN-W0-AICC-PREPUSH-LOCAL-PRESCREEN).

Every REJECT a reviewer hands back costs a full CI run, the review itself, a
`-REM` remediation task and PR, and a re-review — see
`command_center.orchestrator.review_merge._remediate_rejection`. Most of that
churn is avoidable: a bounded, free, local pass over the agent's own workspace
catches the same REJECT-class defects (lint drift, a broken import, an
obviously untested new code path) *before* the branch is pushed, either fixing
them outright or at least surfacing them for visibility.

This module runs three signals CI/review already trust — ruff, a compile
check, and the dependency-based impacted-test selector
(`scripts/ci/test_impact/select_tests.py`) — plus two local-model extras: a
qwen2.5-coder (Ollama) advisory read of the diff, and an opt-in `aider`
auto-fix pass restricted to mechanical classes (docstring drift, an obviously
missing test). All five steps are best-effort and individually time-boxed
inside one overall wall-clock budget.

Advisory, not a gate: this never raises and never blocks the push. A step that
cannot run (tool absent, budget exhausted, a crash) is recorded as skipped and
the caller proceeds regardless — CI's `quality-gates` job and review remain
the sole required authority. See `command_center.audit.checks.base.Check` for
the same "never raise, absence degrades to no findings" contract this mirrors.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from command_center.runtime import providers
from command_center.runtime.validation import (
    DEFAULT_VALIDATION_COMMANDS,
    InvalidValidationCommand,
    parse_command,
    run_validation,
)

DEFAULT_TIME_BUDGET_SECONDS = 240.0
_GIT_TIMEOUT_SECONDS = 15
_SELECTOR_TIMEOUT_SECONDS = 30
_OLLAMA_TIMEOUT_SECONDS = 60
_AIDER_TIMEOUT_SECONDS = 90
_MAX_OLLAMA_DIFF_CHARS = 12_000
_DETAIL_CHAR_LIMIT = 2_000


@dataclass
class StepResult:
    name: str
    ran: bool
    passed: bool
    detail: str = ""
    fixed: bool = False


@dataclass
class PrescreenReport:
    steps: list[StepResult] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    budget_exhausted: bool = False

    @property
    def findings(self) -> list[str]:
        return [f"{s.name}: {s.detail}" for s in self.steps if s.ran and not s.passed and s.detail]

    @property
    def fixes_applied(self) -> list[str]:
        return [s.name for s in self.steps if s.fixed]

    def summary(self) -> str:
        if not self.steps:
            return "prescreen: did not run"
        ran = [s for s in self.steps if s.ran]
        head = f"prescreen: {sum(1 for s in ran if s.passed)}/{len(ran)} steps clean" if ran else "prescreen: no steps ran"
        if self.fixes_applied:
            head += f"; auto-fixed by {', '.join(self.fixes_applied)}"
        if self.budget_exhausted:
            head += "; time budget exhausted before all steps ran"
        return head

    def as_payload(self) -> dict:
        return {
            "summary": self.summary(),
            "elapsed_seconds": round(self.elapsed_seconds, 3),
            "budget_exhausted": self.budget_exhausted,
            "steps": [
                {
                    "name": s.name,
                    "ran": s.ran,
                    "passed": s.passed,
                    "fixed": s.fixed,
                    "detail": s.detail,
                }
                for s in self.steps
            ],
        }


class _Budget:
    def __init__(self, total_seconds: float) -> None:
        self._deadline = time.monotonic() + max(0.0, total_seconds)

    def remaining(self) -> float:
        return max(0.0, self._deadline - time.monotonic())

    def exhausted(self) -> bool:
        return self.remaining() <= 0


def _truncate(text: str | None, limit: int = _DETAIL_CHAR_LIMIT) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 20] + "\n...[truncated]"


def _skip(report: PrescreenReport, name: str, reason: str = "time budget exhausted") -> None:
    report.steps.append(StepResult(name=name, ran=False, passed=True, detail=reason))
    report.budget_exhausted = True


def _safe_workspace_git_env() -> dict[str, str]:
    """Environment for any subprocess run inside the agent's own workspace,
    directly (`git`) or indirectly (`select_tests.py`, `aider`, both of which
    shell out to `git` themselves).

    Unlike a fresh publish clone, this workspace's `.git/config` and hooks
    are agent-controlled: a `core.fsmonitor` setting or a `pre-push` hook
    planted there would execute the moment anything here runs an
    index-refreshing git command (`status`, `diff`, `add`, ...), before the
    checkpoint below ever hands off to the trusted publish clone.
    `GIT_CONFIG_COUNT`/`_KEY_N`/`_VALUE_N` force both off regardless of what
    the repo's own config says, and survive into child processes we don't
    control the argv of. Mirrors the threat
    `workspace_provisioning._trusted_git_environment` closes for the publish
    side; see `test_isolated_workspace.py::test_agent_git_config_cannot_redirect_guarded_publish`.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_CONFIG_")}
    env.update(
        {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "core.fsmonitor",
            "GIT_CONFIG_VALUE_0": "false",
            "GIT_CONFIG_KEY_1": "core.hooksPath",
            "GIT_CONFIG_VALUE_1": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _git(workspace: Path, *args: str, timeout: float = _GIT_TIMEOUT_SECONDS) -> str:
    """Best-effort git invocation: empty string on any failure, never raises."""
    try:
        proc = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-c", f"core.hooksPath={os.devnull}", *args],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_safe_workspace_git_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout or ""


def _no_bytecode_env() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    return env


def _untracked_files(workspace: Path) -> list[str]:
    return [
        line.strip()
        for line in _git(workspace, "ls-files", "--others", "--exclude-standard", "--", ".").splitlines()
        if line.strip()
    ]


def _changed_paths(workspace: Path, base_sha: str) -> list[str]:
    """Files this task touched, tracked or not.

    This runs before the checkpoint commit (see `handlers.py`), so a brand
    new file the agent created is still untracked -- plain `git diff
    --name-only` is blind to it. Order preserved, duplicates dropped.
    """
    tracked = [
        line.strip()
        for line in _git(workspace, "diff", "--name-only", base_sha, "--", ".").splitlines()
        if line.strip()
    ]
    seen: set[str] = set()
    result: list[str] = []
    for path in (*tracked, *_untracked_files(workspace)):
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _diff_including_untracked(workspace: Path, base_sha: str) -> str:
    """`git diff` plus whole-file text for untracked new files (see `_changed_paths`).

    Untracked content is read straight off disk with plain Python I/O, never
    through `git diff --no-index`: an untracked `.gitattributes` (itself
    invisible to `git diff <base_sha>`, so this is the only place that would
    touch it) can assign a `clean` filter to an untracked file, and
    `--no-index` still resolves and runs it -- see
    `test_isolated_workspace.py::test_dirty_checkpoint_captures_file_modes_without_agent_git_execution`.
    """
    parts = []
    tracked_diff = _git(workspace, "diff", base_sha, "--", ".")
    if tracked_diff.strip():
        parts.append(tracked_diff)
    for path in _untracked_files(workspace):
        file_path = workspace / path
        if file_path.is_symlink() or not file_path.is_file():
            continue
        try:
            content = file_path.read_text(errors="replace")
        except OSError:
            continue
        body = "\n".join(f"+{line}" for line in content.splitlines())
        parts.append(f"--- /dev/null\n+++ {path}\n{body}")
    return "\n".join(parts)


def _ollama_model() -> str:
    return os.environ.get("AICC_OLLAMA_MODEL") or providers.DEFAULT_OLLAMA_MODEL


# -- static-tool steps: ruff autofix, compile check --------------------------


def _run_ruff_fix(workspace: Path, report: PrescreenReport, budget: _Budget) -> None:
    name = "ruff"
    if budget.remaining() < 1:
        _skip(report, name)
        return
    try:
        # `python3 -m ruff`, not a bare `ruff` PATH lookup: this always matches
        # the interpreter's installed package (see `audit.checks._ruff`'s
        # docstring for why a bare binary guess is the wrong default here). No
        # `--quiet`: ruff's plain-text fix summary ("N fixed") is the only
        # cheap, reliable "did this actually change anything" signal -- a
        # before/after `git status` diff is blind to edits inside a file that
        # is still untracked (its status stays `??` either way).
        plan = [parse_command("python3 -m ruff check --fix .")]
    except InvalidValidationCommand as exc:
        report.steps.append(StepResult(name, ran=False, passed=True, detail=f"cannot build ruff command: {exc}"))
        return
    outcome = run_validation(str(workspace), plan, timeout_seconds=int(budget.remaining()))
    result = outcome.results[0] if outcome.results else None
    if result is None:
        report.steps.append(StepResult(name, ran=False, passed=True, detail="ruff did not run"))
        return
    if result.exit_code is None:
        report.steps.append(StepResult(name, ran=False, passed=True, detail=f"ruff unavailable: {result.stderr_summary or 'spawn failed'}"))
        return
    fix_match = re.search(r"\((\d+) fixed", result.stdout_summary)
    fixed = bool(fix_match) and int(fix_match.group(1)) > 0
    passed = result.exit_code == 0
    detail = "" if passed else _truncate(result.stdout_summary or result.stderr_summary)
    report.steps.append(StepResult(name, ran=True, passed=passed, fixed=fixed, detail=detail))


def _run_compileall(workspace: Path, report: PrescreenReport, budget: _Budget) -> None:
    name = "compile"
    if budget.remaining() < 1:
        _skip(report, name)
        return
    plan = [parse_command(DEFAULT_VALIDATION_COMMANDS[0])]
    outcome = run_validation(str(workspace), plan, timeout_seconds=int(budget.remaining()))
    result = outcome.results[0] if outcome.results else None
    if result is None:
        report.steps.append(StepResult(name, ran=False, passed=True, detail="compile check did not run"))
        return
    if result.exit_code is None:
        report.steps.append(StepResult(name, ran=False, passed=True, detail=f"python3 unavailable: {result.stderr_summary or 'spawn failed'}"))
        return
    passed = result.exit_code == 0
    detail = "" if passed else _truncate(result.stderr_summary or result.stdout_summary)
    report.steps.append(StepResult(name, ran=True, passed=passed, detail=detail))


# -- impacted tests: reuses CI's own selector, never the full suite ---------


def _run_impacted_tests(workspace: Path, report: PrescreenReport, budget: _Budget, base_sha: str | None) -> None:
    name = "impacted_tests"
    if not base_sha:
        report.steps.append(StepResult(name, ran=False, passed=True, detail="no base sha to diff against"))
        return
    selector = workspace / "scripts" / "ci" / "test_impact" / "select_tests.py"
    if not selector.is_file():
        report.steps.append(StepResult(name, ran=False, passed=True, detail="test-impact selector not present in this workspace"))
        return
    if budget.remaining() < 2:
        _skip(report, name)
        return
    output_file = workspace / ".prepush_prescreen_selected_tests.txt"
    try:
        try:
            subprocess.run(
                [sys.executable, str(selector), "--base", base_sha, "--format", "pytest", "--output", str(output_file)],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=min(_SELECTOR_TIMEOUT_SECONDS, budget.remaining()),
                check=False,
                # The selector shells out to `git diff`/`git status` itself
                # against this same agent-controlled workspace -- see
                # `_safe_workspace_git_env`.
                env=_safe_workspace_git_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            report.steps.append(StepResult(name, ran=False, passed=True, detail=f"selector failed to run: {exc}"))
            return
        selected = [line.strip() for line in output_file.read_text().splitlines()] if output_file.exists() else []
        selected = [line for line in selected if line]
    finally:
        # Never leave selector scratch output in the worktree: the checkpoint
        # step commits everything dirty, and this file is not part of the change.
        output_file.unlink(missing_ok=True)
    if not selected:
        report.steps.append(StepResult(name, ran=True, passed=True, detail="no impacted tests for this change"))
        return
    if selected == ["tests"]:
        report.steps.append(StepResult(name, ran=True, passed=True, detail="global file changed; full suite deferred to CI"))
        return
    if budget.remaining() < 5:
        _skip(report, name, "not enough time budget left to run the selected tests")
        return
    timeout = budget.remaining()
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", *selected],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_no_bytecode_env(),
        )
    except subprocess.TimeoutExpired:
        report.steps.append(StepResult(name, ran=False, passed=True, detail=f"impacted tests timed out after {timeout:.0f}s"))
        return
    except (OSError, subprocess.SubprocessError) as exc:
        report.steps.append(StepResult(name, ran=False, passed=True, detail=f"pytest failed to run: {exc}"))
        return
    passed = proc.returncode == 0
    detail = "" if passed else _truncate(proc.stdout or proc.stderr)
    report.steps.append(StepResult(name, ran=True, passed=passed, detail=detail))


# -- ollama advisory read of the diff ----------------------------------------

_OLLAMA_PROMPT_PREFIX = (
    "You are a terse pre-commit code reviewer. Read the git diff below and list only "
    "concrete defects that would fail review: correctness bugs, obvious security issues, "
    "or new behaviour with no test coverage. If there is nothing worth flagging, reply "
    "with exactly: no findings. Do not restate or summarize the diff.\n\n"
)


def _run_ollama_prescreen(workspace: Path, report: PrescreenReport, budget: _Budget, base_sha: str | None) -> None:
    name = "ollama_prescreen"
    if not base_sha:
        report.steps.append(StepResult(name, ran=False, passed=True, detail="no base sha to diff against"))
        return
    if budget.remaining() < 2:
        _skip(report, name)
        return
    availability = providers.get_provider("ollama").availability()
    if not availability.available or not availability.executable:
        report.steps.append(StepResult(name, ran=False, passed=True, detail=f"ollama unavailable: {availability.message}"))
        return
    diff = _diff_including_untracked(workspace, base_sha)
    if not diff.strip():
        report.steps.append(StepResult(name, ran=True, passed=True, detail="no diff to review"))
        return
    diff = _truncate(diff, _MAX_OLLAMA_DIFF_CHARS)
    timeout = min(_OLLAMA_TIMEOUT_SECONDS, budget.remaining())
    if timeout < 1:
        _skip(report, name)
        return
    try:
        proc = subprocess.run(
            [availability.executable, "run", _ollama_model(), "--nowordwrap", "--hidethinking"],
            input=_OLLAMA_PROMPT_PREFIX + diff,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.steps.append(StepResult(name, ran=False, passed=True, detail=f"ollama invocation failed: {exc}"))
        return
    output = (proc.stdout or "").strip()
    if not output or output.lower().startswith("no findings"):
        report.steps.append(StepResult(name, ran=True, passed=True, detail="no findings"))
        return
    report.steps.append(StepResult(name, ran=True, passed=False, detail=_truncate(output)))


# -- aider auto-fix: opt-in, mechanical classes only -------------------------

_AIDER_INSTRUCTION = (
    "Fix ONLY mechanical, low-risk issues introduced by this diff: stale or missing "
    "docstrings on functions changed here, and an obviously missing test for a clearly "
    "new code path. Do not change behaviour, do not refactor, do not touch any file not "
    "already listed."
)


def _run_aider_autofix(workspace: Path, report: PrescreenReport, budget: _Budget, base_sha: str | None) -> None:
    name = "aider_autofix"
    if os.environ.get("AICC_PREPUSH_AIDER_ENABLED", "0").strip().lower() not in {"1", "true", "yes"}:
        report.steps.append(StepResult(name, ran=False, passed=True, detail="disabled (set AICC_PREPUSH_AIDER_ENABLED=1 to enable)"))
        return
    if not base_sha:
        report.steps.append(StepResult(name, ran=False, passed=True, detail="no base sha to diff against"))
        return
    if budget.remaining() < 5:
        _skip(report, name)
        return
    aider_bin = shutil.which("aider")
    if not aider_bin:
        report.steps.append(StepResult(name, ran=False, passed=True, detail="aider not installed"))
        return
    availability = providers.get_provider("ollama").availability()
    if not availability.available:
        report.steps.append(StepResult(name, ran=False, passed=True, detail=f"aider needs a local model backend; ollama unavailable: {availability.message}"))
        return
    changed = [path for path in _changed_paths(workspace, base_sha) if (workspace / path).is_file()]
    if not changed:
        report.steps.append(StepResult(name, ran=True, passed=True, detail="no changed files to review"))
        return
    before = _git(workspace, "status", "--porcelain")
    # aider reads git status/diff itself for context even with
    # `--no-auto-commits`, against this same agent-controlled workspace --
    # see `_safe_workspace_git_env`.
    env = _safe_workspace_git_env()
    env.setdefault("OLLAMA_API_BASE", "http://localhost:11434")
    argv = [
        aider_bin,
        "--model",
        f"ollama/{_ollama_model()}",
        "--yes-always",
        "--no-auto-commits",
        "--message",
        _AIDER_INSTRUCTION,
        *changed,
    ]
    try:
        subprocess.run(
            argv,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=min(_AIDER_TIMEOUT_SECONDS, budget.remaining()),
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        report.steps.append(StepResult(name, ran=False, passed=True, detail=f"aider failed to run: {exc}"))
        return
    after = _git(workspace, "status", "--porcelain")
    fixed = after != before
    report.steps.append(
        StepResult(name, ran=True, passed=True, fixed=fixed, detail="applied trivial fixes" if fixed else "no trivial fixes needed")
    )


def run_prepush_prescreen(
    workspace: Path,
    *,
    base_sha: str | None = None,
    time_budget_seconds: float | None = None,
) -> PrescreenReport:
    """Run the bounded local prescreen against `workspace` before it is pushed.

    Advisory only: every step is best-effort and this never raises, so a
    caller can invoke it unconditionally on the push path without touching
    its own error handling. `base_sha` scopes the diff-dependent steps
    (impacted tests, the Ollama read, aider) to what this task actually
    changed; without it those steps are skipped, not run against everything.
    """
    if os.environ.get("AICC_PREPUSH_PRESCREEN_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        return PrescreenReport(
            steps=[StepResult("prescreen", ran=False, passed=True, detail="disabled via AICC_PREPUSH_PRESCREEN_ENABLED")]
        )
    if time_budget_seconds is None:
        time_budget_seconds = float(os.environ.get("AICC_PREPUSH_PRESCREEN_BUDGET_SECONDS", DEFAULT_TIME_BUDGET_SECONDS))

    report = PrescreenReport()
    started = time.monotonic()
    try:
        budget = _Budget(time_budget_seconds)
        _run_ruff_fix(workspace, report, budget)
        _run_compileall(workspace, report, budget)
        _run_impacted_tests(workspace, report, budget, base_sha)
        _run_ollama_prescreen(workspace, report, budget, base_sha)
        _run_aider_autofix(workspace, report, budget, base_sha)
    except Exception as exc:  # noqa: BLE001 - advisory pass must never break the push path
        report.steps.append(StepResult("prescreen", ran=False, passed=True, detail=f"prescreen crashed: {exc}"))
    report.elapsed_seconds = time.monotonic() - started
    return report
