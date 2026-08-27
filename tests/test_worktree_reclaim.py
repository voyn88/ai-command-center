"""Regression coverage for the one-time auditable worktree reclaim sweep
(`command_center.worktree_reclaim`, VOYN-W0-AICC-WORKTREE-LEAK).

Root cause it guards against: several worktree-creation mechanisms
(`portfolio_launch.py` once a launch has started, `roadmap/program/
ready_tasks.py --prepare-worktrees`) have no removal counterpart at all, so
their worktrees accumulate on disk forever. This sweep reclaims what is
provably safe — clean, unleased, idle past a grace period — regardless of
which mechanism created it, and never touches anything else.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from command_center import project_config, worktree_reclaim


def _git(cwd: Path, *args: str, env: dict | None = None) -> None:
    subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=env
    )


def _make_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "test")
    (path / "f.txt").write_text("hello\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "init")
    _git(path, "branch", "-M", "main")
    return path


def _add_worktree(repo: Path, path: Path, branch: str) -> Path:
    _git(repo, "worktree", "add", "-b", branch, str(path), "main")
    return path


def _commit_dated(worktree: Path, days_ago: float, filename: str = "note.txt") -> None:
    (worktree / filename).write_text(f"{days_ago}\n")
    _git(worktree, "add", filename)
    moment = datetime.now(timezone.utc) - timedelta(days=days_ago)
    date = moment.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    _git(worktree, "commit", "-q", "-m", f"backdated {days_ago}d", env=env)


def _stub_configs(monkeypatch, configs: dict[str, dict]) -> None:
    monkeypatch.setattr(project_config, "load_project_configs", lambda: configs)


def _no_lease_authority(monkeypatch) -> None:
    monkeypatch.delenv("VOYN_LEASE_DSN", raising=False)


@pytest.fixture(autouse=True)
def _isolate_lease_env(monkeypatch):
    # Every test gets a clean slate: no accidental lease authority from the
    # ambient environment steering a decision.
    monkeypatch.delenv("VOYN_LEASE_DSN", raising=False)
    monkeypatch.delenv("VOYN_LEASE_TOOL", raising=False)


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def test_old_clean_worktree_is_reclaimable(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "old", "task/old")
    _commit_dated(wt, days_ago=30)

    decisions = worktree_reclaim.sweep_repository(repo, min_age_days=7)

    assert len(decisions) == 1
    assert decisions[0].decision == worktree_reclaim.DECISION_RECLAIMABLE
    assert decisions[0].worktree_path == str(wt)
    assert decisions[0].age_days >= 29


def test_recent_worktree_is_skipped(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "recent", "task/recent")
    _commit_dated(wt, days_ago=0.01)

    decisions = worktree_reclaim.sweep_repository(repo, min_age_days=7)

    assert len(decisions) == 1
    assert decisions[0].decision == worktree_reclaim.DECISION_SKIP_RECENT


def test_dirty_worktree_is_never_reclaimable_regardless_of_age(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "dirty", "task/dirty")
    _commit_dated(wt, days_ago=30)
    (wt / "untracked.txt").write_text("leftover work\n")

    decisions = worktree_reclaim.sweep_repository(repo, min_age_days=7)

    assert len(decisions) == 1
    assert decisions[0].decision == worktree_reclaim.DECISION_SKIP_DIRTY


def test_primary_worktree_is_never_a_candidate(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    _add_worktree(repo, tmp_path / "wt" / "old", "task/old")
    _commit_dated(tmp_path / "wt" / "old", days_ago=30)

    decisions = worktree_reclaim.sweep_repository(repo, min_age_days=7)

    assert all(d.worktree_path != str(repo.resolve()) for d in decisions)
    assert len(decisions) == 1  # only the linked worktree, not the primary tree


def test_min_age_days_threshold_is_configurable(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "mid", "task/mid")
    _commit_dated(wt, days_ago=3)

    lenient = worktree_reclaim.sweep_repository(repo, min_age_days=1)
    strict = worktree_reclaim.sweep_repository(repo, min_age_days=7)

    assert lenient[0].decision == worktree_reclaim.DECISION_RECLAIMABLE
    assert strict[0].decision == worktree_reclaim.DECISION_SKIP_RECENT


def test_evaluate_worktree_skips_a_directory_not_owned_by_this_repository(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    other = _make_repo(tmp_path / "unrelated")

    decision = worktree_reclaim.evaluate_worktree(
        {"path": str(other), "branch": "main", "head": "deadbeef"},
        repo,
        lease_state=None,
    )

    assert decision is not None
    assert decision.decision == worktree_reclaim.DECISION_SKIP_NOT_OWNED


# --------------------------------------------------------------------------
# Writer-lease signal
# --------------------------------------------------------------------------


def test_leased_worktree_is_skipped(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "leased", "task/leased")
    _commit_dated(wt, days_ago=30)

    lease_state = (True, frozenset({wt.resolve()}))
    decisions = worktree_reclaim.sweep_repository(repo, min_age_days=7, lease_state=lease_state)

    assert decisions[0].decision == worktree_reclaim.DECISION_SKIP_LEASED


def test_lease_authority_unreachable_fails_closed(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "old", "task/old")
    _commit_dated(wt, days_ago=30)

    lease_state = (False, frozenset())  # authority configured but unreachable
    decisions = worktree_reclaim.sweep_repository(repo, min_age_days=7, lease_state=lease_state)

    assert decisions[0].decision == worktree_reclaim.DECISION_SKIP_LEASE_UNKNOWN


def test_leased_worktree_paths_returns_none_without_an_authority_configured(monkeypatch):
    monkeypatch.delenv("VOYN_LEASE_DSN", raising=False)
    assert worktree_reclaim.leased_worktree_paths() is None


def test_leased_worktree_paths_fails_closed_when_tool_missing(monkeypatch):
    monkeypatch.setenv("VOYN_LEASE_DSN", "postgres://example/leases")
    monkeypatch.setenv("VOYN_LEASE_TOOL", "definitely-not-a-real-binary-xyz")

    reachable, paths = worktree_reclaim.leased_worktree_paths()

    assert reachable is False
    assert paths == frozenset()


# --------------------------------------------------------------------------
# Apply — the one write path
# --------------------------------------------------------------------------


def test_apply_decision_removes_a_reclaimable_worktree(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "old", "task/old")
    _commit_dated(wt, days_ago=30)

    [decision] = worktree_reclaim.sweep_repository(repo, min_age_days=7)
    applied = worktree_reclaim.apply_decision(decision)

    assert applied.applied_outcome == "removed"
    assert not wt.exists()


def test_apply_decision_refuses_a_non_reclaimable_decision(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "recent", "task/recent")
    _commit_dated(wt, days_ago=0.01)

    [decision] = worktree_reclaim.sweep_repository(repo, min_age_days=7)

    with pytest.raises(ValueError):
        worktree_reclaim.apply_decision(decision)
    assert wt.exists()


# --------------------------------------------------------------------------
# sweep_configured_repositories / main — the multi-repo, CLI surface
# --------------------------------------------------------------------------


def test_sweep_configured_repositories_dedupes_a_shared_repository_path(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "old", "task/old")
    _commit_dated(wt, days_ago=30)
    _stub_configs(
        monkeypatch,
        {"AICC": {"repository_path": str(repo)}, "AIOS": {"repository_path": str(repo)}},
    )

    decisions = worktree_reclaim.sweep_configured_repositories(min_age_days=7)

    assert len(decisions) == 1
    assert decisions[0].decision == worktree_reclaim.DECISION_RECLAIMABLE


def test_main_dry_run_never_removes_anything(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "old", "task/old")
    _commit_dated(wt, days_ago=30)
    _stub_configs(monkeypatch, {"AICC": {"repository_path": str(repo)}})
    audit_log = tmp_path / "audit.jsonl"

    exit_code = worktree_reclaim.main(
        ["--min-age-days", "7", "--audit-log", str(audit_log)]
    )

    assert exit_code == 0
    assert wt.exists()
    lines = audit_log.read_text().splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["decision"] == worktree_reclaim.DECISION_RECLAIMABLE
    assert record["applied_outcome"] is None
    assert "dry-run" in capsys.readouterr().err


def test_main_apply_removes_reclaimable_worktrees_and_logs_the_outcome(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "old", "task/old")
    _commit_dated(wt, days_ago=30)
    _stub_configs(monkeypatch, {"AICC": {"repository_path": str(repo)}})
    audit_log = tmp_path / "audit.jsonl"

    exit_code = worktree_reclaim.main(
        ["--min-age-days", "7", "--apply", "--audit-log", str(audit_log)]
    )

    assert exit_code == 0
    assert not wt.exists()
    record = json.loads(audit_log.read_text().splitlines()[0])
    assert record["applied_outcome"] == "removed"


def test_main_reports_nonzero_when_apply_fails_to_remove(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt" / "old", "task/old")
    _commit_dated(wt, days_ago=30)
    _stub_configs(monkeypatch, {"AICC": {"repository_path": str(repo)}})
    monkeypatch.setattr(
        worktree_reclaim.workspace_provisioning,
        "remove_workspace",
        lambda *a, **k: "remove_failed",
    )

    exit_code = worktree_reclaim.main(["--min-age-days", "7", "--apply"])

    assert exit_code == 1
    assert wt.exists()  # the stub never actually removed it


def test_main_exits_zero_when_nothing_is_configured(monkeypatch):
    _stub_configs(monkeypatch, {})
    assert worktree_reclaim.main([]) == 0
