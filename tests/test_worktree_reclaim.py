"""Regression coverage for the one-time leaked-worktree reclaim sweep
(`command_center.worktree_reclaim`), VOYN-W0-AICC-WORKTREE-LEAK-REM.

Covers the three independent-review findings on db95d63 that rejected the
first version of this sweep:

1. Age must come from worktree metadata, never the HEAD commit's own
   timestamp -- a worktree freshly created against an old base commit must
   not look ancient.
2. A candidate's safety conditions must be re-derived immediately before
   deletion, never replayed from an earlier classification pass.
3. `--min-age-days` must reject negative and non-finite (`NaN`/`inf`)
   values, not silently treat them as "everything is old enough".
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from command_center import project_config, worktree_reclaim


def _git(cwd: Path, *args: str, env: dict | None = None) -> None:
    full_env = {**os.environ, **(env or {})}
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True, env=full_env)


def _make_repo(path: Path, *, old_commit: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "test")
    (path / "f.txt").write_text("hello\n")
    _git(path, "add", "f.txt")
    commit_env = {}
    if old_commit:
        # An ancient commit timestamp -- proves age comes from worktree
        # metadata, never from this (finding #1 on db95d63).
        old_date = "2000-01-01T00:00:00"
        commit_env = {"GIT_AUTHOR_DATE": old_date, "GIT_COMMITTER_DATE": old_date}
    _git(path, "commit", "-q", "-m", "init", env=commit_env)
    _git(path, "branch", "-M", "main")
    return path


def _add_worktree(repo: Path, worktree: Path, branch: str, base: str = "main") -> Path:
    _git(repo, "worktree", "add", "-b", branch, str(worktree), base)
    return worktree


def _age_worktree(worktree: Path, days: float) -> None:
    """Push a worktree's admin-dir files `days` into the past, simulating a
    worktree that has genuinely sat untouched."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"], cwd=worktree, capture_output=True, text=True, check=True
    )
    admin_dir = Path(result.stdout.strip())
    if not admin_dir.is_absolute():
        admin_dir = worktree / admin_dir
    admin_dir = admin_dir.resolve()
    past = time.time() - days * 86400
    for candidate in (
        admin_dir / "HEAD",
        admin_dir / "index",
        admin_dir / "ORIG_HEAD",
        admin_dir / "logs" / "HEAD",
    ):
        if candidate.exists():
            os.utime(candidate, (past, past))
    os.utime(admin_dir, (past, past))


def _stub_configs(monkeypatch, configs: dict[str, dict]) -> None:
    monkeypatch.setattr(project_config, "load_project_configs", lambda: configs)


def _no_lease(monkeypatch) -> None:
    monkeypatch.setattr(worktree_reclaim, "blocking_lease", lambda _path: None)


# --------------------------------------------------------------------------
# worktree_metadata_age_days
# --------------------------------------------------------------------------


def test_age_is_near_zero_for_a_freshly_created_worktree_on_an_ancient_commit(tmp_path):
    repo = _make_repo(tmp_path / "repo", old_commit=True)
    wt = _add_worktree(repo, tmp_path / "wt", "task/fresh")

    age = worktree_reclaim.worktree_metadata_age_days(wt)

    assert age is not None
    assert age < 0.01, "a worktree created moments ago must not inherit its base commit's age"


def test_age_reflects_an_explicitly_backdated_admin_directory(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt", "task/old")
    _age_worktree(wt, days=30)

    age = worktree_reclaim.worktree_metadata_age_days(wt)

    assert age is not None
    assert 29.9 < age < 30.1


def test_age_is_none_for_a_directory_with_no_git_metadata(tmp_path):
    plain = tmp_path / "not-a-worktree"
    plain.mkdir()

    assert worktree_reclaim.worktree_metadata_age_days(plain) is None


# --------------------------------------------------------------------------
# evaluate_worktree
# --------------------------------------------------------------------------


def test_evaluate_reclaimable_when_clean_unleased_and_old_enough(tmp_path, monkeypatch):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt", "task/a")
    _age_worktree(wt, days=10)

    candidate = worktree_reclaim.evaluate_worktree(repo, wt, min_age_days=7)

    assert candidate.reclaimable is True
    assert candidate.reason == worktree_reclaim.REASON_RECLAIMABLE
    assert candidate.age_days > 9


def test_evaluate_refuses_a_dirty_worktree(tmp_path, monkeypatch):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt", "task/a")
    _age_worktree(wt, days=10)
    (wt / "untracked.txt").write_text("oops\n")

    candidate = worktree_reclaim.evaluate_worktree(repo, wt, min_age_days=7)

    assert candidate.reclaimable is False
    assert candidate.reason == worktree_reclaim.REASON_DIRTY


def test_evaluate_refuses_a_worktree_younger_than_the_threshold(tmp_path, monkeypatch):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt", "task/a")

    candidate = worktree_reclaim.evaluate_worktree(repo, wt, min_age_days=7)

    assert candidate.reclaimable is False
    assert candidate.reason == worktree_reclaim.REASON_TOO_YOUNG


def test_evaluate_refuses_a_leased_worktree(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt", "task/a")
    _age_worktree(wt, days=10)
    monkeypatch.setattr(
        worktree_reclaim, "blocking_lease", lambda _path: "worktree held by writer 'someone'"
    )

    candidate = worktree_reclaim.evaluate_worktree(repo, wt, min_age_days=7)

    assert candidate.reclaimable is False
    assert candidate.reason == worktree_reclaim.REASON_LEASED


def test_evaluate_refuses_a_directory_it_does_not_own(tmp_path, monkeypatch):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    unrelated = _make_repo(tmp_path / "unrelated")

    candidate = worktree_reclaim.evaluate_worktree(repo, unrelated, min_age_days=0)

    assert candidate.reclaimable is False
    assert candidate.reason == worktree_reclaim.REASON_NOT_OWNED


def test_evaluate_never_reclaims_the_primary_working_tree(tmp_path, monkeypatch):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")

    candidate = worktree_reclaim.evaluate_worktree(repo, repo, min_age_days=0)

    assert candidate.reclaimable is False
    assert candidate.reason == worktree_reclaim.REASON_NOT_OWNED


# --------------------------------------------------------------------------
# reclaim_worktree -- must re-derive safety immediately before deleting,
# never trust an earlier classification (finding #2 on db95d63)
# --------------------------------------------------------------------------


def test_reclaim_removes_a_worktree_that_is_still_reclaimable_on_recheck(tmp_path, monkeypatch):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt", "task/a")
    _age_worktree(wt, days=10)

    outcome = worktree_reclaim.reclaim_worktree(repo, wt, min_age_days=7)

    assert outcome == "removed"
    assert not wt.exists()


def test_reclaim_refuses_when_the_tree_went_dirty_after_classification(tmp_path, monkeypatch):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt", "task/a")
    _age_worktree(wt, days=10)

    # An earlier sweep pass would have classified this as reclaimable...
    cached = worktree_reclaim.evaluate_worktree(repo, wt, min_age_days=7)
    assert cached.reclaimable is True

    # ...but something wrote into it before the delete actually ran.
    (wt / "in-flight.txt").write_text("agent still working\n")

    outcome = worktree_reclaim.reclaim_worktree(repo, wt, min_age_days=7)

    assert outcome == worktree_reclaim.REASON_DIRTY
    assert wt.exists(), "a dirty tree must never be removed, regardless of an earlier decision"


def test_reclaim_refuses_when_a_lease_appears_after_classification(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt", "task/a")
    _age_worktree(wt, days=10)

    lease_state = {"held": False}
    monkeypatch.setattr(
        worktree_reclaim,
        "blocking_lease",
        lambda _path: ("held by another writer" if lease_state["held"] else None),
    )

    cached = worktree_reclaim.evaluate_worktree(repo, wt, min_age_days=7)
    assert cached.reclaimable is True

    lease_state["held"] = True  # taken in the window between classify and delete

    outcome = worktree_reclaim.reclaim_worktree(repo, wt, min_age_days=7)

    assert outcome == worktree_reclaim.REASON_LEASED
    assert wt.exists()


# --------------------------------------------------------------------------
# --min-age-days validation (finding #3 on db95d63)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("raw", ["-1", "-0.5", "nan", "NaN", "inf", "-inf"])
def test_cli_rejects_negative_and_non_finite_min_age_days(raw, capsys):
    with pytest.raises(SystemExit) as exc_info:
        worktree_reclaim.main(["--min-age-days", raw])

    assert exc_info.value.code == 2
    assert "--min-age-days" in capsys.readouterr().err


def test_cli_accepts_zero_and_positive_min_age_days():
    assert worktree_reclaim._min_age_days("0") == 0.0
    assert worktree_reclaim._min_age_days("3.5") == 3.5


# --------------------------------------------------------------------------
# sweep / discover_candidates / main -- end to end over configured repos
# --------------------------------------------------------------------------


def test_discover_candidates_classifies_every_non_primary_worktree(tmp_path, monkeypatch):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    _add_worktree(repo, tmp_path / "wt-old", "task/old")
    _age_worktree(tmp_path / "wt-old", days=30)
    _add_worktree(repo, tmp_path / "wt-new", "task/new")

    candidates = worktree_reclaim.discover_candidates(repo, min_age_days=7)

    by_path = {c.worktree_path: c for c in candidates}
    assert len(candidates) == 2
    assert by_path[str((tmp_path / "wt-old").resolve())].reclaimable is True
    assert by_path[str((tmp_path / "wt-new").resolve())].reclaimable is False


def test_sweep_dry_run_never_removes_anything(tmp_path, monkeypatch):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt", "task/a")
    _age_worktree(wt, days=30)
    _stub_configs(monkeypatch, {"AICC": {"repository_path": str(repo)}})

    candidates = worktree_reclaim.sweep(min_age_days=7, apply=False)

    assert len(candidates) == 1
    assert candidates[0].reclaimable is True
    assert wt.exists()


def test_sweep_apply_removes_only_reclaimable_worktrees(tmp_path, monkeypatch):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    old_wt = _add_worktree(repo, tmp_path / "wt-old", "task/old")
    _age_worktree(old_wt, days=30)
    new_wt = _add_worktree(repo, tmp_path / "wt-new", "task/new")
    _stub_configs(monkeypatch, {"AICC": {"repository_path": str(repo)}})

    results = worktree_reclaim.sweep(min_age_days=7, apply=True)

    outcomes = {r.worktree_path: r.reason for r in results}
    assert outcomes[str(old_wt.resolve())] == "removed"
    assert outcomes[str(new_wt.resolve())] == worktree_reclaim.REASON_TOO_YOUNG
    assert not old_wt.exists()
    assert new_wt.exists()


def test_sweep_dedupes_a_repository_path_shared_by_two_projects(tmp_path, monkeypatch):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    _add_worktree(repo, tmp_path / "wt", "task/a")
    _stub_configs(
        monkeypatch,
        {"AICC": {"repository_path": str(repo)}, "AIOS": {"repository_path": str(repo)}},
    )

    candidates = worktree_reclaim.sweep(min_age_days=7, apply=False)

    assert len(candidates) == 1


def test_sweep_skips_a_configured_but_missing_repository(tmp_path, monkeypatch):
    missing = tmp_path / "never-cloned-on-this-host"
    _stub_configs(monkeypatch, {"AICC": {"repository_path": str(missing)}})

    assert worktree_reclaim.sweep(min_age_days=7, apply=False) == []


def test_main_dry_run_reports_without_removing(tmp_path, monkeypatch, capsys):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt", "task/a")
    _age_worktree(wt, days=30)
    _stub_configs(monkeypatch, {"AICC": {"repository_path": str(repo)}})

    assert worktree_reclaim.main(["--min-age-days", "7"]) == 0

    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert worktree_reclaim.REASON_RECLAIMABLE in out
    assert wt.exists()


def test_main_apply_removes_and_reports_count(tmp_path, monkeypatch, capsys):
    _no_lease(monkeypatch)
    repo = _make_repo(tmp_path / "repo")
    wt = _add_worktree(repo, tmp_path / "wt", "task/a")
    _age_worktree(wt, days=30)
    _stub_configs(monkeypatch, {"AICC": {"repository_path": str(repo)}})

    assert worktree_reclaim.main(["--min-age-days", "7", "--apply"]) == 0

    out = capsys.readouterr().out
    assert "APPLY" in out
    assert "removed 1 of 1 worktree(s)" in out
    assert not wt.exists()


def test_main_exits_zero_when_nothing_is_configured(monkeypatch, capsys):
    _stub_configs(monkeypatch, {})

    assert worktree_reclaim.main([]) == 0
    assert "no worktrees found" in capsys.readouterr().out
