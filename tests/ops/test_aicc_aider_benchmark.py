"""The aider+Ollama benchmark harness (VOYN-W0-AICC-AIDER-OLLAMA-EXECUTOR):
hermetic tests only -- the executor itself is faked at `agent_runner.
run_claude_code`, the same module seam `tests/worker/test_handlers.py` uses,
so nothing here launches a real `aider`/`ollama` process.

`AICC_DATA_DIR` is redirected to a temp dir by the session conftest (reset
between tests), so the `ROOT` passed to ledger calls below is unused for its
own sake -- same pattern as `tests/dispatch/test_policy_config.py`. `tmp_path`
is used only where a test needs a REAL, isolated filesystem worktree (seeding
a fixture git repo), which is unrelated to the ledger's storage root.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

from command_center import agent_runner
from command_center.orchestrator import local_model_gates

ROOT = Path("/unused-because-AICC_DATA_DIR-overrides")


def _module():
    path = Path(__file__).parents[2] / "ops" / "aicc_aider_benchmark.py"
    spec = importlib.util.spec_from_file_location("aicc_aider_benchmark", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


aicc_aider_benchmark = _module()


def _fake_completed_result():
    return agent_runner.RunResult(
        status="completed",
        exit_code=0,
        stdout="ok",
        stderr="",
        duration_seconds=1.0,
        started_at="2026-09-06T00:00:00Z",
        completed_at="2026-09-06T00:00:01Z",
    )


def _fake_failed_result():
    return agent_runner.RunResult(
        status="failed",
        exit_code=1,
        stdout="",
        stderr="boom",
        duration_seconds=1.0,
        started_at="2026-09-06T00:00:00Z",
        completed_at="2026-09-06T00:00:01Z",
    )


def test_seed_repo_creates_a_clean_initial_commit(tmp_path):
    case = aicc_aider_benchmark.CASES[0]
    aicc_aider_benchmark._seed_repo(tmp_path, case)
    for relative_path in case.seed_files:
        assert (tmp_path / relative_path).exists()
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() == ""


def test_run_case_passes_when_executor_completes_and_verification_succeeds(
    tmp_path, monkeypatch
):
    case = aicc_aider_benchmark.CASES[0]

    def fake_run_claude_code(*, repository_path, executor, **_kwargs):
        assert executor == "aider"
        # Simulate aider having made exactly the requested edit.
        (repository_path / "README.md").write_text(
            "This is a small fixture repo used to benchmark the "
            "bounded-implementation lane.\n",
            encoding="utf-8",
        )
        return _fake_completed_result()

    monkeypatch.setattr(agent_runner, "run_claude_code", fake_run_claude_code)
    assert aicc_aider_benchmark.run_case(case) is True


def test_run_case_fails_when_executor_run_does_not_complete(monkeypatch):
    case = aicc_aider_benchmark.CASES[0]
    monkeypatch.setattr(
        agent_runner, "run_claude_code", lambda **_kwargs: _fake_failed_result()
    )
    assert aicc_aider_benchmark.run_case(case) is False


def test_run_case_fails_when_verification_does_not_match(monkeypatch):
    case = aicc_aider_benchmark.CASES[0]

    def fake_run_claude_code(*, repository_path, **_kwargs):
        # aider "completes" but never actually fixes the typos.
        return _fake_completed_result()

    monkeypatch.setattr(agent_runner, "run_claude_code", fake_run_claude_code)
    assert aicc_aider_benchmark.run_case(case) is False


def test_run_suite_records_one_ledger_sample_per_case(monkeypatch):
    outcomes = iter([True, False, True])
    monkeypatch.setattr(
        aicc_aider_benchmark, "run_case", lambda _case, **_kwargs: next(outcomes)
    )
    results = aicc_aider_benchmark.run_suite(root=ROOT, actor="test-suite")
    assert results == {
        aicc_aider_benchmark.CASES[0].name: True,
        aicc_aider_benchmark.CASES[1].name: False,
        aicc_aider_benchmark.CASES[2].name: True,
    }
    record = local_model_gates.load_gates(ROOT)[aicc_aider_benchmark.TASK_CLASS]
    assert record.sample_count == 3
    assert record.pass_count == 2
    assert record.updated_by == "test-suite"


def test_main_status_reports_an_unbenchmarked_class_as_not_promoted(capsys):
    exit_code = aicc_aider_benchmark.main(["--root", str(ROOT), "status"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "promoted=False" in out
    assert "sample_count=0" in out


def test_main_demote_writes_an_explicit_reason():
    exit_code = aicc_aider_benchmark.main(
        ["--root", str(ROOT), "demote", "--reason", "regression on 2026-09-06"]
    )
    assert exit_code == 0
    record = local_model_gates.load_gates(ROOT)[aicc_aider_benchmark.TASK_CLASS]
    assert record.promoted is False
    assert record.notes == "regression on 2026-09-06"


def test_main_run_returns_nonzero_when_any_case_fails(monkeypatch):
    monkeypatch.setattr(aicc_aider_benchmark, "run_case", lambda _case, **_kwargs: False)
    exit_code = aicc_aider_benchmark.main(["--root", str(ROOT), "run"])
    assert exit_code == 1
