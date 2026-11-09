"""PR-Agent (Qodo OSS) review companion (VOYN-W0-AICC-PR-AGENT-INTEGRATION).

Targets the specific defects a prior version of this module was rejected
for (PR #735, b350d6a5):

* an in-checkout `--state-path` was claimed in prose to be impossible to
  make `self_deploy`'s dirty-tree check see, while nothing enforced it
  (chunk 1/3, finding 1) -- `test_state_path_inside_checkout_raises`;
* the CLI-equivalent report had no way to distinguish an operational
  defect from success (chunk 1/3, finding 2) -- the `errors` vs `skipped`
  split is exercised throughout;
* a bare `"python"` rather than `sys.executable` was spawned (chunk 2/3,
  finding 1) -- `test_run_pr_agent_exact_argv_uses_sys_executable`, which
  (unlike the rejected PR's own test) asserts the FULL argv, not a
  substring/membership check (chunk 3/3, finding 1);
* `TimeoutExpired`/`OSError` propagated out of the per-PR loop instead of
  being recorded as a per-PR skip (chunk 2/3, finding 2) --
  `test_run_pr_agent_timeout_is_caught_not_raised`,
  `test_run_pr_agent_oserror_is_caught_not_raised`,
  `test_review_once_pr_agent_failure_isolated_to_one_pr`;
* a credential value could leak into a stored skip/error reason (chunk
  2/3, finding 3) -- `test_run_pr_agent_redacts_credential_from_failure_output`.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from command_center.orchestrator import pr_agent_review
from command_center.orchestrator.pr_agent_review import (
    PrAgentConfig,
    _run_pr_agent,
    _state_file,
    preflight,
    review_once,
)

PR_URL = "https://github.com/voyn88/ai-command-center/pull/42"


# -- preflight: credential resolution never reveals a secret value ----------


def test_preflight_neither_set_is_not_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    env_var, detail = preflight()
    assert env_var is None
    assert detail == "not_configured"


def test_preflight_single_provider_resolves_by_name_only(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    env_var, detail = preflight()
    assert env_var == "GEMINI_API_KEY"
    assert detail == "configured:GEMINI_API_KEY"
    assert "super-secret-value" not in detail


def test_preflight_both_set_refuses_rather_than_picks_a_winner(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "a")
    monkeypatch.setenv("GROQ_API_KEY", "b")
    env_var, detail = preflight()
    assert env_var is None
    assert detail == "multiple_providers_configured:GEMINI_API_KEY+GROQ_API_KEY"


# -- state path: enforced outside the checkout, not merely documented -------


def test_state_path_outside_checkout_resolves(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state_dir = tmp_path / "state"
    resolved = _state_file(str(repo), str(state_dir / "state.json"))
    assert resolved == (state_dir / "state.json").resolve()


def test_state_path_inside_checkout_raises(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    with pytest.raises(ValueError, match="resolves inside repo_path"):
        _state_file(str(repo), str(repo / "state.json"))


def test_state_path_relative_inside_checkout_raises(tmp_path, monkeypatch):
    """The exact scenario the rejected PR's review flagged: a relative
    `--state-path`, passed from a shell whose cwd happens to be the
    checkout (plausible when running this by hand), resolves inside the
    repo and must be refused -- not just one that is already absolute."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    with pytest.raises(ValueError, match="resolves inside repo_path"):
        _state_file(str(repo), "state.json")


# -- _run_pr_agent: exact argv, sys.executable, isolated failures -----------


def test_run_pr_agent_exact_argv_uses_sys_executable(monkeypatch):
    """Asserts the COMPLETE argv (not a substring/membership check, the
    exact defect the rejected PR's own test suite had -- chunk 3/3,
    finding 1), and that the interpreter is `sys.executable`, never a bare
    `"python"` (chunk 2/3, finding 1)."""
    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["cwd"] = kwargs.get("cwd")
        captured["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    monkeypatch.setattr(pr_agent_review.subprocess, "run", fake_run)
    cfg = PrAgentConfig(repo_path="/some/repo", timeout=123)
    ok, detail = _run_pr_agent(PR_URL, cfg)

    assert ok is True
    assert detail == "ok"
    assert captured["argv"] == [
        sys.executable,
        "-m",
        "pr_agent.cli",
        "--pr_url",
        PR_URL,
        "review",
        "--config.enable_auto_approval=false",
    ]
    assert captured["argv"][0] != "python"
    assert captured["cwd"] == "/some/repo"
    assert captured["timeout"] == 123


def test_run_pr_agent_timeout_is_caught_not_raised(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 0))

    monkeypatch.setattr(pr_agent_review.subprocess, "run", fake_run)
    ok, detail = _run_pr_agent(PR_URL, PrAgentConfig(timeout=7))
    assert ok is False
    assert detail == "timed_out_after_7s"


def test_run_pr_agent_oserror_is_caught_not_raised(monkeypatch):
    def fake_run(argv, **kwargs):
        raise FileNotFoundError("no such file or directory: 'python'")

    monkeypatch.setattr(pr_agent_review.subprocess, "run", fake_run)
    ok, detail = _run_pr_agent(PR_URL, PrAgentConfig())
    assert ok is False
    assert "spawn_failed" in detail


def test_run_pr_agent_nonzero_exit_reports_tail(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 2, "", "boom: something failed")

    monkeypatch.setattr(pr_agent_review.subprocess, "run", fake_run)
    ok, detail = _run_pr_agent(PR_URL, PrAgentConfig())
    assert ok is False
    assert "exit_2" in detail
    assert "boom: something failed" in detail


def test_run_pr_agent_redacts_credential_from_failure_output(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "sk-live-secretvalue123")
    monkeypatch.setenv("GITHUB__USER_TOKEN", "ghp_tokensecret456")

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(
            argv, 1, "", "auth failed with Authorization: Bearer ghp_tokensecret456 "
            "and key sk-live-secretvalue123"
        )

    monkeypatch.setattr(pr_agent_review.subprocess, "run", fake_run)
    ok, detail = _run_pr_agent(PR_URL, PrAgentConfig())
    assert ok is False
    assert "sk-live-secretvalue123" not in detail
    assert "ghp_tokensecret456" not in detail
    assert "***REDACTED***" in detail


# -- review_once: dedup, isolation, error/skip classification ---------------


def _pr(number, head, *, draft=False, url=None):
    return {
        "number": number,
        "html_url": url or f"https://github.com/voyn88/ai-command-center/pull/{number}",
        "head": {"sha": head},
        "draft": draft,
    }


@pytest.fixture
def layout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = tmp_path / "state" / "state.json"
    return repo, state_path


def test_review_once_no_credential_is_clean_skip_not_error(monkeypatch, layout):
    repo, state_path = layout
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    report = review_once(PrAgentConfig(repo_path=str(repo), state_path=str(state_path)))
    assert report.credential_env is None
    assert report.errors == []
    assert report.skipped == [(None, "not_configured")]
    assert report.reviewed == []


def test_review_once_state_path_inside_checkout_is_error(monkeypatch, layout):
    repo, _ = layout
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    report = review_once(
        PrAgentConfig(repo_path=str(repo), state_path=str(repo / "state.json"))
    )
    assert report.errors
    assert "resolves inside repo_path" in report.errors[0][1]


def test_review_once_pr_list_failed_is_error(monkeypatch, layout):
    repo, state_path = layout
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(pr_agent_review, "_list_open_prs", lambda repo_path, limit: None)
    report = review_once(PrAgentConfig(repo_path=str(repo), state_path=str(state_path)))
    assert report.errors == [(None, "pr_list_failed")]


def test_review_once_filters_drafts_and_dedups_at_head(monkeypatch, layout):
    repo, state_path = layout
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    prs = [_pr(1, "a" * 40), _pr(2, "b" * 40, draft=True)]
    monkeypatch.setattr(pr_agent_review, "_list_open_prs", lambda repo_path, limit: prs)
    monkeypatch.setattr(pr_agent_review, "_run_pr_agent", lambda url, cfg: (True, "ok"))

    report = review_once(PrAgentConfig(repo_path=str(repo), state_path=str(state_path)))
    assert report.reviewed == [(1, prs[0]["html_url"])]
    assert (2, "draft") in report.skipped
    assert report.errors == []

    state = json.loads(state_path.read_text())
    assert state == {"1": "a" * 40}

    # Second tick, same head: already reviewed, PR-Agent not run again.
    calls = []
    monkeypatch.setattr(
        pr_agent_review,
        "_run_pr_agent",
        lambda url, cfg: calls.append(url) or (True, "ok"),
    )
    report2 = review_once(PrAgentConfig(repo_path=str(repo), state_path=str(state_path)))
    assert report2.reviewed == []
    assert (1, "already_reviewed_at_head") in report2.skipped
    assert calls == []


def test_review_once_pr_agent_failure_isolated_to_one_pr(monkeypatch, layout):
    """A failing PR must not abort review of the PRs after it (the exact
    propagation defect `_run_pr_agent`'s own try/except now prevents one
    layer down)."""
    repo, state_path = layout
    monkeypatch.setenv("GROQ_API_KEY", "x")
    prs = [_pr(1, "a" * 40), _pr(2, "b" * 40)]
    monkeypatch.setattr(pr_agent_review, "_list_open_prs", lambda repo_path, limit: prs)

    def fake_run(url, cfg):
        if "pull/1" in url:
            return False, "timed_out_after_600s"
        return True, "ok"

    monkeypatch.setattr(pr_agent_review, "_run_pr_agent", fake_run)
    report = review_once(PrAgentConfig(repo_path=str(repo), state_path=str(state_path)))

    assert report.reviewed == [(2, prs[1]["html_url"])]
    assert (1, "pr_agent_failed:timed_out_after_600s") in report.errors
    state = json.loads(state_path.read_text())
    assert state == {"2": "b" * 40}


def test_review_once_malformed_listing_entry_is_skipped(monkeypatch, layout):
    repo, state_path = layout
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    monkeypatch.setattr(
        pr_agent_review, "_list_open_prs",
        lambda repo_path, limit: [{"number": 3, "head": {}}],
    )
    report = review_once(PrAgentConfig(repo_path=str(repo), state_path=str(state_path)))
    assert report.skipped == [(3, "malformed_pr_list_entry")]
    assert report.errors == []
    assert report.reviewed == []


def test_review_once_never_imports_marker_or_acceptance_modules():
    """Static guard against regressing the comment-only/no-marker-authority
    design: this module must import neither the acceptance identity nor
    the review contour's own module (the only places the ACCEPTANCE/
    VERDICT marker is ever constructed or posted)."""
    import ast

    import command_center.orchestrator.pr_agent_review as mod

    with open(mod.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported_modules = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert not any("github_app_auth" in name for name in imported_modules)
    assert not any(name.endswith("review_merge") for name in imported_modules)
