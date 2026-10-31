"""Regression coverage for the periodic worktree-prune sweep
(`command_center.worktree_sweep`), VOYN-W0-AICC-ISOLATED-WORKTREE-PER-ATTEMPT
hardening follow-up.

Root cause it guards against: `workspace_provisioning.remove_workspace`'s
inline `git worktree prune` only reconciles the one worktree it just
removed. Every other case — a `remove_workspace` call that returned
`"not_owned"`/`"remove_failed"`, or a worker process killed before any
cleanup ran — leaves dangling `.git/worktrees/<name>` metadata that nothing
else ever revisits. This sweep is that "something else".
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from command_center import project_config, worktree_sweep


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


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


def _stub_configs(monkeypatch, configs: dict[str, dict]) -> None:
    monkeypatch.setattr(project_config, "load_project_configs", lambda: configs)


def test_sweep_prunes_every_distinct_configured_repository(tmp_path, monkeypatch):
    repo_a = _make_repo(tmp_path / "repo_a")
    repo_b = _make_repo(tmp_path / "repo_b")
    wt = tmp_path / "wt"
    _git(repo_a, "worktree", "add", "-b", "task/a", str(wt / "a"), "main")
    shutil.rmtree(wt / "a")  # directory gone without `worktree remove` -> dangling metadata

    _stub_configs(
        monkeypatch,
        {
            "AICC": {"repository_path": str(repo_a)},
            "AIOS": {"repository_path": str(repo_b)},
        },
    )

    results = worktree_sweep.sweep_configured_repositories()

    assert results == {str(repo_a): "pruned", str(repo_b): "pruned"}


def test_sweep_dedupes_a_repository_path_shared_by_two_projects(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    _stub_configs(
        monkeypatch,
        {
            "AICC": {"repository_path": str(repo)},
            "AIOS": {"repository_path": str(repo)},
        },
    )

    results = worktree_sweep.sweep_configured_repositories()

    assert results == {str(repo): "pruned"}


def test_sweep_skips_projects_without_a_configured_repository_path(monkeypatch):
    _stub_configs(
        monkeypatch,
        {
            "AICC": {"repository_path": None},
            "AIOS": {},
        },
    )

    assert worktree_sweep.sweep_configured_repositories() == {}


def test_sweep_reports_a_configured_but_missing_repository_without_raising(tmp_path, monkeypatch):
    missing = tmp_path / "never-cloned-on-this-host"
    _stub_configs(monkeypatch, {"AICC": {"repository_path": str(missing)}})

    assert worktree_sweep.sweep_configured_repositories() == {str(missing): "not_a_repository"}


def test_main_exits_zero_when_nothing_is_configured(monkeypatch, capsys):
    _stub_configs(monkeypatch, {})

    assert worktree_sweep.main() == 0
    assert "no configured repositories" in capsys.readouterr().out


def test_main_exits_zero_for_pruned_and_not_a_repository_outcomes(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path / "repo")
    missing = tmp_path / "missing"
    _stub_configs(
        monkeypatch,
        {"AICC": {"repository_path": str(repo)}, "AIOS": {"repository_path": str(missing)}},
    )

    assert worktree_sweep.main() == 0
    out = capsys.readouterr().out
    assert "pruned" in out
    assert "not_a_repository" in out


def test_main_exits_nonzero_when_a_prune_fails(tmp_path, monkeypatch, capsys):
    repo = _make_repo(tmp_path / "repo")
    _stub_configs(monkeypatch, {"AICC": {"repository_path": str(repo)}})
    monkeypatch.setattr(
        worktree_sweep.workspace_provisioning,
        "prune_repository",
        lambda _repo: "prune_failed",
    )

    assert worktree_sweep.main() == 1
    assert "prune_failed" in capsys.readouterr().out
