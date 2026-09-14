"""dirty_pr_remediation's git mechanics against a real local repo -- no
PostgreSQL, no gh, no network. `_attempt_local_merge` does real fetches,
worktrees and merges (clean, conflicting, and binary-conflicting); `_git`'s
timeout handling and the Markdown-fence escaping are unit-tested directly.
"""

from __future__ import annotations

import subprocess

import pytest

from command_center.orchestrator import dirty_pr_remediation as dpr


def _git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path):
    """A bare 'origin' plus a routed clone (`main_clone`) that already has
    both `origin/main` and a `origin/pr` remote-tracking ref -- the shape
    `_attempt_local_merge` expects of `repo_path`."""
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True,
    )
    clone = tmp_path / "main_clone"
    subprocess.run(["git", "clone", str(bare), str(clone)], check=True, capture_output=True)
    _git(clone, "config", "user.email", "t@t")
    _git(clone, "config", "user.name", "t")
    (clone / "shared.txt").write_text("base\n")
    (clone / "other.txt").write_text("other-base\n")
    _git(clone, "add", ".")
    _git(clone, "commit", "-m", "base")
    _git(clone, "push", "origin", "main")

    _git(clone, "checkout", "-b", "pr")
    (clone / "other.txt").write_text("other-base\nfrom-pr\n")
    _git(clone, "commit", "-am", "pr change")
    _git(clone, "push", "origin", "pr")

    _git(clone, "checkout", "main")
    return clone


def test_attempt_local_merge_clean_merge_reports_new_head(repo, tmp_path):
    """origin/main advances with a change to a DIFFERENT file than the PR
    branch touched: `git merge origin/main` onto the PR branch succeeds
    cleanly, and the worktree it ran in is reported so the caller can
    publish from it."""
    (repo / "shared.txt").write_text("base\nfrom-main\n")
    _git(repo, "commit", "-am", "main change")
    _git(repo, "push", "origin", "main")

    attempt = dpr._attempt_local_merge(
        str(repo), "pr", "main", old_head_sha="deadbeef",
        worktree_root=str(tmp_path), timeout=30, conflict_snippet_max_chars=4000,
    )
    try:
        assert attempt.clean
        assert attempt.error == ""
        assert attempt.conflicts == ()
        assert attempt.worktree_path is not None
        assert attempt.new_head_sha and len(attempt.new_head_sha) == 40
        content = subprocess.run(
            ["git", "show", "HEAD:shared.txt"], cwd=attempt.worktree_path,
            check=True, capture_output=True, text=True,
        ).stdout
        assert "from-main" in content
    finally:
        if attempt.worktree_path is not None:
            dpr._remove_worktree(str(repo), attempt.worktree_path, 30)


def test_attempt_local_merge_conflict_captures_both_sides_and_leaves_clean_worktree(
    repo, tmp_path
):
    """A real conflict on `shared.txt`: both `ours` and `theirs` content are
    captured before `merge --abort` runs, and the worktree is left in a
    normal (non-mid-merge) state afterward -- confirmed by `git status`."""
    (repo / "shared.txt").write_text("base\nfrom-main\n")
    _git(repo, "commit", "-am", "main change")
    _git(repo, "push", "origin", "main")

    clone2 = tmp_path / "second_clone"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(clone2)],
        check=True, capture_output=True,
    )
    _git(clone2, "config", "user.email", "t@t")
    _git(clone2, "config", "user.name", "t")
    _git(clone2, "checkout", "pr")
    (clone2 / "shared.txt").write_text("base\nfrom-pr-conflicting\n")
    _git(clone2, "commit", "-am", "pr conflicting change")
    _git(clone2, "push", "origin", "pr", "--force")

    attempt = dpr._attempt_local_merge(
        str(repo), "pr", "main", old_head_sha="deadbeef",
        worktree_root=str(tmp_path), timeout=30, conflict_snippet_max_chars=4000,
    )
    try:
        assert not attempt.clean
        assert attempt.error == ""
        assert attempt.worktree_path is not None
        assert len(attempt.conflicts) == 1
        conflict = attempt.conflicts[0]
        assert conflict.path == "shared.txt"
        assert not conflict.binary
        assert conflict.ours is not None and "from-pr-conflicting" in conflict.ours
        assert conflict.theirs is not None and "from-main" in conflict.theirs

        status = subprocess.run(
            ["git", "status", "--porcelain=v1"], cwd=attempt.worktree_path,
            check=True, capture_output=True, text=True,
        ).stdout
        assert status.strip() == ""
        merge_head = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "MERGE_HEAD"],
            cwd=attempt.worktree_path, capture_output=True, text=True,
        )
        assert merge_head.returncode != 0
    finally:
        if attempt.worktree_path is not None:
            dpr._remove_worktree(str(repo), attempt.worktree_path, 30)


def test_attempt_local_merge_handles_binary_conflict_without_crashing(repo, tmp_path):
    """A conflicting BINARY file must never raise `UnicodeDecodeError` --
    `_stage_content` catches the decode failure and reports it as an
    omitted, non-crashing binary conflict instead."""
    binary_bytes = bytes(range(256))
    (repo / "shared.bin").write_bytes(binary_bytes + b"-main")
    _git(repo, "add", "shared.bin")
    _git(repo, "commit", "-m", "main binary change")
    _git(repo, "push", "origin", "main")

    clone2 = tmp_path / "second_clone_bin"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(clone2)],
        check=True, capture_output=True,
    )
    _git(clone2, "config", "user.email", "t@t")
    _git(clone2, "config", "user.name", "t")
    _git(clone2, "checkout", "pr")
    (clone2 / "shared.bin").write_bytes(binary_bytes + b"-pr")
    _git(clone2, "add", "shared.bin")
    _git(clone2, "commit", "-m", "pr binary change")
    _git(clone2, "push", "origin", "pr", "--force")

    attempt = dpr._attempt_local_merge(
        str(repo), "pr", "main", old_head_sha="deadbeef",
        worktree_root=str(tmp_path), timeout=30, conflict_snippet_max_chars=4000,
    )
    try:
        assert not attempt.clean
        assert attempt.error == ""
        assert len(attempt.conflicts) == 1
        conflict = attempt.conflicts[0]
        assert conflict.path == "shared.bin"
        assert conflict.binary
        assert conflict.ours is None
        assert conflict.theirs is None
    finally:
        if attempt.worktree_path is not None:
            dpr._remove_worktree(str(repo), attempt.worktree_path, 30)


def test_attempt_local_merge_truncates_long_conflict_snippets(repo, tmp_path):
    long_main = "main-" + ("x" * 5000)
    long_pr = "pr-" + ("y" * 5000)
    (repo / "shared.txt").write_text(long_main)
    _git(repo, "commit", "-am", "main long change")
    _git(repo, "push", "origin", "main")

    clone2 = tmp_path / "second_clone_long"
    subprocess.run(
        ["git", "clone", str(tmp_path / "origin.git"), str(clone2)],
        check=True, capture_output=True,
    )
    _git(clone2, "config", "user.email", "t@t")
    _git(clone2, "config", "user.name", "t")
    _git(clone2, "checkout", "pr")
    (clone2 / "shared.txt").write_text(long_pr)
    _git(clone2, "commit", "-am", "pr long change")
    _git(clone2, "push", "origin", "pr", "--force")

    attempt = dpr._attempt_local_merge(
        str(repo), "pr", "main", old_head_sha="deadbeef",
        worktree_root=str(tmp_path), timeout=30, conflict_snippet_max_chars=100,
    )
    try:
        assert len(attempt.conflicts) == 1
        conflict = attempt.conflicts[0]
        assert conflict.truncated
        assert conflict.ours is not None and conflict.ours.endswith("... (truncated)")
        assert len(conflict.ours) < 150
        assert conflict.theirs is not None and conflict.theirs.endswith("... (truncated)")
        assert len(conflict.theirs) < 150
    finally:
        if attempt.worktree_path is not None:
            dpr._remove_worktree(str(repo), attempt.worktree_path, 30)


def test_remove_worktree_deletes_directory_and_prunes(repo, tmp_path):
    (repo / "shared.txt").write_text("base\nfrom-main\n")
    _git(repo, "commit", "-am", "main change")
    _git(repo, "push", "origin", "main")

    attempt = dpr._attempt_local_merge(
        str(repo), "pr", "main", old_head_sha="deadbeef",
        worktree_root=str(tmp_path), timeout=30, conflict_snippet_max_chars=4000,
    )
    assert attempt.worktree_path is not None
    import os
    assert os.path.isdir(attempt.worktree_path)

    dpr._remove_worktree(str(repo), attempt.worktree_path, 30)
    assert not os.path.isdir(attempt.worktree_path)
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo,
        check=True, capture_output=True, text=True,
    ).stdout
    assert attempt.worktree_path not in listing


def test_git_timeout_expired_is_data_not_an_exception(monkeypatch):
    """A single slow `git` call must never crash the whole tick -- `_git`
    catches `subprocess.TimeoutExpired` and returns an ordinary failed
    `CompletedProcess` instead of letting the exception propagate."""

    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "fetch"], timeout=5)

    monkeypatch.setattr(subprocess, "run", _raise)
    result = dpr._git(["fetch", "origin"], "/tmp", timeout=5)
    assert result.returncode == 124
    assert "timeout" in result.stderr


def test_git_bytes_timeout_expired_is_data_not_an_exception(monkeypatch):
    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["git", "show"], timeout=5)

    monkeypatch.setattr(subprocess, "run", _raise)
    result = dpr._git_bytes(["show", ":2:x"], "/tmp", timeout=5)
    assert result.returncode == 124
    assert result.stdout == b""


def test_fence_for_escapes_content_containing_triple_backticks():
    assert dpr._fence_for("no backticks here") == "```"
    assert dpr._fence_for("has ``` three backticks") == "````"
    assert dpr._fence_for("has ```` four backticks") == "`````"


def test_format_conflicts_uses_a_longer_fence_when_content_has_backticks():
    conflict = dpr.ConflictFile(
        path="x.md", ours="```embedded fence```", theirs="plain", binary=False,
    )
    formatted = dpr._format_conflicts((conflict,))
    # The fence surrounding the "ours" block must be longer than any run of
    # backticks the content itself contains, or the Markdown would break.
    assert "````\n```embedded fence```\n````" in formatted


def test_format_conflicts_reports_binary_files_without_dumping_content():
    conflict = dpr.ConflictFile(path="img.png", ours=None, theirs=None, binary=True)
    formatted = dpr._format_conflicts((conflict,))
    assert "binary file" in formatted
    assert "img.png" in formatted
