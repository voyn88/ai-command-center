"""VOYN-W0-AICC-PREPUSH-LOCAL-PRESCREEN: the bounded, best-effort worker-side
prescreen. Ruff, the compile check, and (when the fake stands in for it) the
Ollama advisory pass exercise their real subprocess plumbing over the real
``git_repo`` fixture -- these are fast, local tools and the point of the
module is exactly that tool-invocation glue. `aider` is never installed in
this environment (nor most worker hosts yet), so its tests only prove the
opt-in/graceful-absence contract, never a real invocation."""

from __future__ import annotations

import subprocess
from pathlib import Path

from command_center.runtime import providers
from command_center.worker import prepush_prescreen as ps


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _status(repo: Path) -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


def _unavailable(monkeypatch, provider_id: str = "ollama") -> None:
    monkeypatch.setattr(
        providers,
        "get_provider",
        lambda pid: type(
            "_Unavailable",
            (),
            {
                "availability": staticmethod(
                    lambda: providers.ProviderAvailability(
                        provider_id, False, "executable_missing", "not found"
                    )
                )
            },
        )(),
    )


# -- kill switch --------------------------------------------------------------


def test_disabled_via_env_skips_every_step(git_repo, monkeypatch) -> None:
    monkeypatch.setenv("AICC_PREPUSH_PRESCREEN_ENABLED", "0")
    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))
    assert [s.name for s in report.steps] == ["prescreen"]
    assert report.steps[0].detail == "disabled via AICC_PREPUSH_PRESCREEN_ENABLED"


# -- ruff autofix --------------------------------------------------------------


def test_ruff_autofixes_an_unused_import_and_reports_it(git_repo, monkeypatch) -> None:
    _unavailable(monkeypatch)  # keep the ollama/aider steps out of this test's way
    (git_repo / "bad.py").write_text("import os\n\nx = 1\n")
    before = _status(git_repo)

    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    ruff_step = next(s for s in report.steps if s.name == "ruff")
    assert ruff_step.ran and ruff_step.passed and ruff_step.fixed
    assert "import os" not in (git_repo / "bad.py").read_text()
    assert _status(git_repo) != before  # the fix is a real, uncommitted edit


def test_ruff_reports_an_unfixable_violation_without_blocking(git_repo, monkeypatch) -> None:
    _unavailable(monkeypatch)
    # F821 (undefined name) has no safe autofix: ruff must report it as a
    # finding, not silently drop it or crash the pass.
    (git_repo / "bad.py").write_text("def f():\n    return undefined_name\n")

    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    ruff_step = next(s for s in report.steps if s.name == "ruff")
    assert ruff_step.ran and not ruff_step.passed
    assert "F821" in ruff_step.detail or "undefined" in ruff_step.detail.lower()


# -- compile check --------------------------------------------------------------


def test_compile_check_flags_a_syntax_error(git_repo, monkeypatch) -> None:
    _unavailable(monkeypatch)
    (git_repo / "broken.py").write_text("def f(:\n    pass\n")

    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    compile_step = next(s for s in report.steps if s.name == "compile")
    assert compile_step.ran and not compile_step.passed


def test_compile_check_passes_clean_source(git_repo, monkeypatch) -> None:
    _unavailable(monkeypatch)
    (git_repo / "fine.py").write_text("x = 1\n")

    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    compile_step = next(s for s in report.steps if s.name == "compile")
    assert compile_step.ran and compile_step.passed


# -- impacted tests --------------------------------------------------------------


def test_impacted_tests_skips_when_selector_is_not_in_this_workspace(
    git_repo, monkeypatch
) -> None:
    _unavailable(monkeypatch)
    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    step = next(s for s in report.steps if s.name == "impacted_tests")
    assert not step.ran
    assert "selector" in step.detail.lower()


def test_impacted_tests_skips_without_a_base_sha(git_repo, monkeypatch) -> None:
    _unavailable(monkeypatch)
    report = ps.run_prepush_prescreen(git_repo, base_sha=None)

    step = next(s for s in report.steps if s.name == "impacted_tests")
    assert not step.ran
    assert "base sha" in step.detail.lower()


# -- ollama advisory read --------------------------------------------------------


def test_ollama_step_skips_when_unavailable(git_repo, monkeypatch) -> None:
    _unavailable(monkeypatch)
    (git_repo / "changed.py").write_text("y = 2\n")

    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    step = next(s for s in report.steps if s.name == "ollama_prescreen")
    assert not step.ran
    assert "unavailable" in step.detail.lower()


def test_ollama_step_skips_with_no_diff(git_repo, monkeypatch, tmp_path) -> None:
    fake = tmp_path / "fake-ollama"
    fake.write_text("#!/bin/sh\necho 'no findings'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(
        providers,
        "get_provider",
        lambda pid: type(
            "_Available",
            (),
            {
                "availability": staticmethod(
                    lambda: providers.ProviderAvailability(
                        "ollama", True, "usable", "ok", str(fake), "0"
                    )
                )
            },
        )(),
    )
    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    step = next(s for s in report.steps if s.name == "ollama_prescreen")
    assert step.ran and step.passed
    assert step.detail == "no diff to review"


def test_ollama_step_surfaces_findings_from_a_fake_model(git_repo, monkeypatch, tmp_path) -> None:
    fake = tmp_path / "fake-ollama"
    fake.write_text(
        "#!/bin/sh\ncat <<'EOF'\n- the new branch has no test coverage\nEOF\n"
    )
    fake.chmod(0o755)
    monkeypatch.setattr(
        providers,
        "get_provider",
        lambda pid: type(
            "_Available",
            (),
            {
                "availability": staticmethod(
                    lambda: providers.ProviderAvailability(
                        "ollama", True, "usable", "ok", str(fake), "0"
                    )
                )
            },
        )(),
    )
    (git_repo / "changed.py").write_text("y = 2\n")

    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    step = next(s for s in report.steps if s.name == "ollama_prescreen")
    assert step.ran and not step.passed
    assert "test coverage" in step.detail


# -- workspace git safety: agent-controlled config/hooks must never fire ---------


def test_poisoned_fsmonitor_and_hooks_never_execute(git_repo, monkeypatch, tmp_path) -> None:
    """An agent-controlled `.git/config`/hooks must not fire just because the
    prescreen ran a `git` command in this workspace -- mirrors
    `test_isolated_workspace.py::test_agent_git_config_cannot_redirect_guarded_publish`,
    the same threat against the guarded-publish checkpoint that runs right
    after this."""
    _unavailable(monkeypatch)
    sentinel = tmp_path / "sentinel"
    attacker = tmp_path / "attacker.sh"
    attacker.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
    attacker.chmod(0o755)
    subprocess.run(["git", "config", "core.fsmonitor", str(attacker)], cwd=git_repo, check=True)
    hooks_dir = git_repo / ".git" / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    for hook_name in ("pre-push", "post-commit"):
        hook = hooks_dir / hook_name
        hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\n")
        hook.chmod(0o755)
    (git_repo / "changed.py").write_text("y = 2\n")

    ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    assert not sentinel.exists()


def test_poisoned_clean_filter_on_an_untracked_file_never_executes(
    git_repo, monkeypatch, tmp_path
) -> None:
    """An untracked `.gitattributes` plus a local `filter.<name>.clean` --
    both fully agent-controlled -- must not run just because the ollama step
    reads an untracked file's content. Regression: `git diff --no-index`
    still resolves and executes attribute-driven filters even though the
    file itself was never added to the index."""
    fake = tmp_path / "fake-ollama"
    fake.write_text("#!/bin/sh\necho 'no findings'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(
        providers,
        "get_provider",
        lambda pid: type(
            "_Available",
            (),
            {
                "availability": staticmethod(
                    lambda: providers.ProviderAvailability(
                        "ollama", True, "usable", "ok", str(fake), "0"
                    )
                )
            },
        )(),
    )
    sentinel = tmp_path / "sentinel"
    attacker = tmp_path / "attacker.sh"
    attacker.write_text(f"#!/bin/sh\ntouch '{sentinel}'\ncat\n")
    attacker.chmod(0o755)
    subprocess.run(["git", "config", "filter.evil.clean", str(attacker)], cwd=git_repo, check=True)
    subprocess.run(["git", "config", "filter.evil.required", "true"], cwd=git_repo, check=True)
    (git_repo / ".gitattributes").write_text("payload.txt filter=evil\n")
    (git_repo / "payload.txt").write_text("raw payload\n")

    ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    assert not sentinel.exists()


# -- aider auto-fix: opt-in, off by default --------------------------------------


def test_aider_is_off_by_default(git_repo, monkeypatch) -> None:
    _unavailable(monkeypatch)
    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    step = next(s for s in report.steps if s.name == "aider_autofix")
    assert not step.ran
    assert "disabled" in step.detail.lower()


def test_aider_skips_gracefully_when_the_binary_is_absent(git_repo, monkeypatch) -> None:
    monkeypatch.setenv("AICC_PREPUSH_AIDER_ENABLED", "1")
    monkeypatch.setattr(ps.shutil, "which", lambda name: None)
    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    step = next(s for s in report.steps if s.name == "aider_autofix")
    assert not step.ran
    assert "not installed" in step.detail.lower()


# -- time budget and crash safety ------------------------------------------------


def test_exhausted_budget_skips_every_step(git_repo, monkeypatch) -> None:
    _unavailable(monkeypatch)
    report = ps.run_prepush_prescreen(
        git_repo, base_sha=_head(git_repo), time_budget_seconds=0
    )
    assert report.budget_exhausted
    assert all(not s.ran for s in report.steps)


def test_a_crashing_step_never_raises_out_of_the_prescreen(git_repo, monkeypatch) -> None:
    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(ps, "_run_ruff_fix", _boom)

    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))

    assert any("prescreen crashed" in s.detail for s in report.steps)


def test_report_summary_and_payload_shape(git_repo, monkeypatch) -> None:
    _unavailable(monkeypatch)
    report = ps.run_prepush_prescreen(git_repo, base_sha=_head(git_repo))
    payload = report.as_payload()
    assert payload["summary"].startswith("prescreen:")
    assert isinstance(payload["steps"], list) and payload["steps"]
    assert {"name", "ran", "passed", "fixed", "detail"} <= payload["steps"][0].keys()
