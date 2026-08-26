"""Unit coverage for the Integration Center health collectors
(`command_center/integration/collectors.py`) — AICC-INT-001 increment 1.

Contract under test (docs/INTEGRATION_CENTER.md): strictly read-only, one
`_run_gh` seam for every GitHub read, and structured degradation — a missing
checkout, missing `gh`, or bad JSON yields `available: False`, never an
exception.
"""

from __future__ import annotations

import json
import subprocess

from command_center.integration import collectors


def _entry(repo_path) -> dict:
    return {
        "id": "x",
        "name": "X",
        "kind": "application",
        "project": "AICC",
        "repo_path": str(repo_path) if repo_path else None,
        "remote": None,
        "default_branch": "main",
    }


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr="")


# --- worktree state ---------------------------------------------------------


def test_worktree_state_unconfigured_without_path():
    assert collectors.worktree_state(_entry(None)) == "unconfigured"


def test_worktree_state_invalid_path(tmp_path):
    assert collectors.worktree_state(_entry(tmp_path / "missing")) == "invalid_path"


def test_worktree_state_not_git_repo(tmp_path):
    assert collectors.worktree_state(_entry(tmp_path)) == "not_git_repo"


def test_worktree_state_ok_for_real_repo(git_repo):
    assert collectors.worktree_state(_entry(git_repo)) == "ok"


# --- git signals ------------------------------------------------------------


def test_collect_git_reports_branch_and_last_activity(git_repo):
    result = collectors.collect_git(git_repo)
    assert result["available"] is True
    assert result["branch"]
    assert result["last_activity"]  # ISO timestamp of the seed commit
    assert result["dirty"] is False


def test_collect_git_degrades_outside_a_repo(tmp_path):
    result = collectors.collect_git(tmp_path)
    assert result == {"available": False, "error": "not a git repository"}


# --- github signals (mocked through the one _run_gh seam) -------------------


def test_collect_github_reports_ci_and_pr_count(tmp_path, monkeypatch):
    def fake_gh(repo_path, args):
        if args[:2] == ["pr", "list"]:
            return _completed(json.dumps([{"number": 1}, {"number": 2}]))
        assert args[:2] == ["run", "list"]
        assert "main" in args  # default branch is what the CI badge reports on
        return _completed(json.dumps([{"status": "completed", "conclusion": "success"}]))

    monkeypatch.setattr(collectors, "_run_gh", fake_gh)
    result = collectors.collect_github(tmp_path)
    assert result["available"] is True
    assert result["open_pr_count"] == 2
    assert result["ci_state"] == "success"


def test_collect_github_in_progress_run(tmp_path, monkeypatch):
    def fake_gh(repo_path, args):
        if args[:2] == ["pr", "list"]:
            return _completed("[]")
        return _completed(json.dumps([{"status": "in_progress", "conclusion": None}]))

    monkeypatch.setattr(collectors, "_run_gh", fake_gh)
    assert collectors.collect_github(tmp_path)["ci_state"] == "in_progress"


def test_collect_github_degrades_when_gh_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(collectors, "_run_gh", lambda repo_path, args: None)
    result = collectors.collect_github(tmp_path)
    assert result["available"] is False
    assert "gh unavailable" in result["error"]


def test_collect_github_degrades_on_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(collectors, "_run_gh", lambda repo_path, args: _completed("not-json"))
    result = collectors.collect_github(tmp_path)
    assert result["available"] is False


def test_run_gh_survives_missing_binary(tmp_path, monkeypatch):
    def raise_missing(*args, **kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(collectors.subprocess, "run", raise_missing)
    assert collectors._run_gh(tmp_path, ["pr", "list"]) is None


# --- composed health --------------------------------------------------------


def test_collect_health_short_circuits_on_bad_worktree():
    health = collectors.collect_health(_entry(None))
    assert health["worktree_state"] == "unconfigured"
    assert health["git"]["available"] is False
    assert health["github"]["available"] is False


def test_collect_health_composes_all_signals_for_a_real_repo(git_repo, monkeypatch):
    monkeypatch.setattr(
        collectors,
        "_run_gh",
        lambda repo_path, args: _completed("[]"),
    )
    health = collectors.collect_health(_entry(git_repo))
    assert health["worktree_state"] == "ok"
    assert health["git"]["available"] is True
    assert health["github"]["available"] is True
    assert health["github"]["open_pr_count"] == 0
