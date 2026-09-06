from __future__ import annotations

import json
import subprocess

import pytest

from command_center.orchestrator import pr_agent_review as par

PR1 = "https://github.com/voyn88/ai-command-center/pull/1"
PR2 = "https://github.com/voyn88/ai-command-center/pull/2"
HEAD1, HEAD2 = "a" * 40, "b" * 40


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["x"], returncode, stdout, stderr)


def _set_creds(monkeypatch, *, model="gemini/gemini-2.5-flash", gemini="g-key", groq=""):
    monkeypatch.setenv("PR_AGENT_MODEL", model) if model else monkeypatch.delenv(
        "PR_AGENT_MODEL", raising=False
    )
    if gemini:
        monkeypatch.setenv("GEMINI_API_KEY", gemini)
    else:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    if groq:
        monkeypatch.setenv("GROQ_API_KEY", groq)
    else:
        monkeypatch.delenv("GROQ_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("PR_AGENT_MODEL", "GEMINI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(key, raising=False)


def test_credentials_none_when_unset():
    assert par._credentials() is None


def test_credentials_none_when_model_missing_is_misconfiguration(monkeypatch):
    _set_creds(monkeypatch, model="", gemini="g-key")
    assert par._credentials() is None


def test_credentials_none_when_key_missing(monkeypatch):
    _set_creds(monkeypatch, model="gemini/gemini-2.5-flash", gemini="", groq="")
    assert par._credentials() is None


def test_credentials_gemini(monkeypatch):
    _set_creds(monkeypatch, gemini="g-key")
    creds = par._credentials()
    assert creds.model == "gemini/gemini-2.5-flash"
    assert creds.env == {"GOOGLE_AI_STUDIO__GEMINI_API_KEY": "g-key"}


def test_credentials_groq(monkeypatch):
    _set_creds(monkeypatch, model="groq/llama-3.3-70b-versatile", gemini="", groq="q-key")
    creds = par._credentials()
    assert creds.model == "groq/llama-3.3-70b-versatile"
    assert creds.env == {"GROQ__KEY": "q-key"}


def test_review_once_skips_loudly_without_credentials(tmp_path):
    cfg = par.PrAgentConfig(repo_path=".", state_path=tmp_path / "state.json")
    report = par.review_once(cfg)
    assert report.reviewed == []
    assert report.skipped == [("*", "pr_agent_not_configured")]


def test_review_once_skips_when_gh_token_unavailable(monkeypatch, tmp_path):
    _set_creds(monkeypatch)
    monkeypatch.setattr(par, "_gh", lambda argv, repo: _cp(returncode=1))
    cfg = par.PrAgentConfig(repo_path=".", state_path=tmp_path / "state.json")
    report = par.review_once(cfg)
    assert report.skipped == [("*", "github_token_unavailable")]


def test_review_once_skips_when_pr_list_fails(monkeypatch, tmp_path):
    _set_creds(monkeypatch)

    def fake_gh(argv, repo):
        if argv[:2] == ["auth", "token"]:
            return _cp(stdout="tok\n")
        return _cp(returncode=1)

    monkeypatch.setattr(par, "_gh", fake_gh)
    cfg = par.PrAgentConfig(repo_path=".", state_path=tmp_path / "state.json")
    report = par.review_once(cfg)
    assert report.skipped == [("*", "gh_pr_list_failed")]


def _open_prs_gh(prs):
    def fake_gh(argv, repo):
        if argv[:2] == ["auth", "token"]:
            return _cp(stdout="tok\n")
        if argv[0] == "pr" and argv[1] == "list":
            return _cp(stdout=json.dumps(prs))
        raise AssertionError(f"unexpected gh call: {argv}")

    return fake_gh


def test_review_once_reviews_open_prs_and_filters_drafts(monkeypatch, tmp_path):
    _set_creds(monkeypatch)
    prs = [
        {"number": 1, "url": PR1, "headRefOid": HEAD1, "isDraft": False},
        {"number": 2, "url": PR2, "headRefOid": HEAD2, "isDraft": True},
    ]
    monkeypatch.setattr(par, "_gh", _open_prs_gh(prs))

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return _cp(returncode=0)

    monkeypatch.setattr(par.subprocess, "run", fake_run)
    cfg = par.PrAgentConfig(repo_path=".", state_path=tmp_path / "state.json")
    report = par.review_once(cfg)

    assert report.reviewed == [("1", PR1)]
    assert report.skipped == []
    assert len(calls) == 1
    argv = calls[0]
    assert argv[:5] == ["python", "-m", "pr_agent.cli", "--pr_url", PR1]
    assert "review" in argv
    assert "auto_approve" not in argv


def test_review_once_never_reruns_same_head(monkeypatch, tmp_path):
    _set_creds(monkeypatch)
    prs = [{"number": 1, "url": PR1, "headRefOid": HEAD1, "isDraft": False}]
    monkeypatch.setattr(par, "_gh", _open_prs_gh(prs))

    calls = []
    monkeypatch.setattr(
        par.subprocess, "run", lambda argv, **kw: (calls.append(argv), _cp())[1]
    )
    state_path = tmp_path / "state.json"
    cfg = par.PrAgentConfig(repo_path=".", state_path=state_path)

    first = par.review_once(cfg)
    assert first.reviewed == [("1", PR1)]
    assert json.loads(state_path.read_text()) == {"1": HEAD1}

    second = par.review_once(cfg)
    assert second.reviewed == []
    assert second.skipped == []
    assert len(calls) == 1  # not called again for the same head


def test_review_once_respects_max_per_tick(monkeypatch, tmp_path):
    _set_creds(monkeypatch)
    prs = [
        {"number": i, "url": f"{PR1}{i}", "headRefOid": "a" * 39 + str(i), "isDraft": False}
        for i in range(5)
    ]
    monkeypatch.setattr(par, "_gh", _open_prs_gh(prs))
    calls = []
    monkeypatch.setattr(
        par.subprocess, "run", lambda argv, **kw: (calls.append(argv), _cp())[1]
    )
    cfg = par.PrAgentConfig(
        repo_path=".", state_path=tmp_path / "state.json", max_per_tick=2
    )
    report = par.review_once(cfg)
    assert len(report.reviewed) == 2
    assert len(calls) == 2


def test_review_once_records_pr_agent_failure_without_updating_state(monkeypatch, tmp_path):
    _set_creds(monkeypatch)
    prs = [{"number": 1, "url": PR1, "headRefOid": HEAD1, "isDraft": False}]
    monkeypatch.setattr(par, "_gh", _open_prs_gh(prs))
    monkeypatch.setattr(
        par.subprocess, "run",
        lambda argv, **kw: _cp(returncode=1, stderr="boom"),
    )
    state_path = tmp_path / "state.json"
    cfg = par.PrAgentConfig(repo_path=".", state_path=state_path)
    report = par.review_once(cfg)
    assert report.reviewed == []
    assert len(report.skipped) == 1
    number, reason = report.skipped[0]
    assert number == "1"
    assert "boom" in reason
    assert not state_path.exists()
