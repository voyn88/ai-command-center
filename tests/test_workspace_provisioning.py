"""Regression coverage for the shared workspace provisioning + verification
gate (`command_center.workspace_provisioning`).

Root cause it guards against: a task whose expected branch was
`audit/execution-queue` launched Claude in the *main* repository on `main`,
reached "Workspace Verified", and left untracked files behind — branch
mismatch was only a warning, never a hard gate, and the workspace silently
fell back to `repository_path`.
"""

from __future__ import annotations

from dataclasses import replace

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from command_center import git_info
from command_center import workspace_provisioning as wp
from command_center.workspace_authority import (
    decode_workspace_authority_key,
    load_workspace_authority_environment,
)


def test_workspace_authority_never_falls_back_to_rotating_lease_dsn(
    monkeypatch,
):
    monkeypatch.delenv("AICC_WORKSPACE_AUTHORITY_KEY", raising=False)
    monkeypatch.setenv("VOYN_LEASE_DSN", "test-only-rotating-dsn")

    assert wp._workspace_authority_key() is None


@pytest.mark.parametrize(
    "value",
    ["plain-text-secret", "hex:01", "hex:not-hex", "base64:YWJj"],
)
def test_workspace_authority_rejects_weak_or_ambiguous_keys(monkeypatch, value):
    monkeypatch.setenv("AICC_WORKSPACE_AUTHORITY_KEY", value)

    assert wp._workspace_authority_key() is None


def test_workspace_authority_accepts_explicit_32_byte_key(monkeypatch):
    monkeypatch.setenv("AICC_WORKSPACE_AUTHORITY_KEY", "hex:" + "ab" * 32)

    assert wp._workspace_authority_key() == bytes.fromhex("ab" * 32)


def test_workspace_authority_runtime_and_installer_decoder_accept_same_base64():
    encoded = "base64:YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXowMTIzNDU="  # pragma: allowlist secret

    assert (
        decode_workspace_authority_key(encoded) == b"abcdefghijklmnopqrstuvwxyz012345"
    )


def test_installer_rejects_long_encoding_with_short_decoded_key(tmp_path):
    authority = tmp_path / "workspace-authority.env"
    # 24 decoded bytes; the encoded EnvironmentFile value itself is longer
    # than 32 characters and was incorrectly accepted by the old installer.
    authority.write_text(
        "AICC_WORKSPACE_AUTHORITY_KEY=base64:YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFh\n",  # pragma: allowlist secret
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="decode to 32"):
        load_workspace_authority_environment(authority, require_root_owned=False)


@pytest.mark.skipif(os.name == "nt", reason="Linux worker dirfd boundary")
def test_private_authority_write_replaces_final_symlink_without_following(tmp_path):
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve")
    marker = tmp_path / "authority.json"
    marker.symlink_to(victim)

    wp._atomic_write_private(marker, b"signed\n")

    assert victim.read_bytes() == b"preserve"
    assert stat.S_ISREG(marker.lstat().st_mode)
    assert marker.read_bytes() == b"signed\n"


@pytest.mark.skipif(os.name == "nt", reason="Linux worker dirfd boundary")
def test_private_authority_write_refuses_precreated_temp_symlink(tmp_path, monkeypatch):
    victim = tmp_path / "victim"
    victim.write_bytes(b"preserve")
    marker = tmp_path / "authority.json"
    monkeypatch.setattr(wp.secrets, "token_hex", lambda _length: "fixed")
    (tmp_path / ".authority.json.fixed.tmp").symlink_to(victim)

    with pytest.raises(FileExistsError):
        wp._atomic_write_private(marker, b"signed\n")

    assert victim.read_bytes() == b"preserve"
    assert not marker.exists()


@pytest.mark.skipif(os.name == "nt", reason="Linux worker dirfd boundary")
def test_private_authority_write_fsyncs_file_and_parent(tmp_path, monkeypatch):
    marker = tmp_path / "authority.json"
    observed: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd):
        mode = os.fstat(fd).st_mode
        observed.append("dir" if stat.S_ISDIR(mode) else "file")
        return real_fsync(fd)

    monkeypatch.setattr(wp.os, "fsync", recording_fsync)

    wp._atomic_write_private(marker, b"signed\n")

    assert observed == ["file", "dir"]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


def _make_repo(path: Path, *, default_branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "test")
    (path / "f.txt").write_text("hello\n")
    _git(path, "add", "f.txt")
    _git(path, "commit", "-q", "-m", "init")
    _git(path, "branch", "-M", default_branch)
    return path


def _current_branch(path: Path) -> str:
    return git_info.get_status(path)["branch"]


# --------------------------------------------------------------------------
# Provisioning
# --------------------------------------------------------------------------


def test_missing_workspace_is_created_automatically(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "worktrees" / "audit"
    spec = wp.WorkspaceSpec(
        workspace_path=str(workspace),
        expected_branch="audit/execution-queue",
        base_branch="main",
        repository_path=str(repo),
    )

    assert not workspace.exists()
    evidence = wp.provision_and_verify(spec)

    assert workspace.is_dir()
    assert evidence.provision_outcome == "created"
    assert _current_branch(workspace) == "audit/execution-queue"
    assert evidence.is_isolated_worktree is True


def test_standalone_clone_under_canonical_worker_root_is_exact_and_reusable(
    tmp_path, monkeypatch
):
    repo = _make_repo(tmp_path / "publisher" / "repo")
    canonical_root = tmp_path / "srv" / "aicc-workspaces"
    canonical_root.mkdir(parents=True)
    branch = "backlog/VOYN-W0-CANONICAL"
    workspace = wp.task_workspace_path(repo, branch, clone_root=canonical_root)
    monkeypatch.setenv("AICC_WORKSPACE_AUTHORITY_KEY", "hex:" + "42" * 32)
    spec = wp.WorkspaceSpec(
        workspace_path=str(workspace),
        expected_branch=branch,
        base_branch="main",
        repository_path=str(repo),
        task_local_git_metadata=True,
        task_clone_root=str(canonical_root),
    )

    evidence = wp.provision_and_verify(spec)
    assert evidence.provision_outcome == "cloned"
    assert workspace.is_relative_to(canonical_root)
    assert (workspace / ".git").is_dir()
    assert wp.provision_and_verify(spec).provision_outcome == "reused"

    wrong = replace(spec, workspace_path=str(canonical_root / "attacker"))
    with pytest.raises(wp.WorkspaceVerificationError, match="trusted path"):
        wp.verify_workspace(wrong)


def test_branch_is_created_from_base_branch(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    # A distinct base commit so we can prove the worktree forked from it.
    _git(repo, "checkout", "-q", "-b", "release")
    (repo / "release.txt").write_text("release\n")
    _git(repo, "add", "release.txt")
    _git(repo, "commit", "-q", "-m", "release work")
    _git(repo, "checkout", "-q", "main")

    workspace = tmp_path / "wt" / "feat"
    spec = wp.WorkspaceSpec(
        workspace_path=str(workspace),
        expected_branch="feature/x",
        base_branch="release",
        repository_path=str(repo),
    )
    wp.provision_and_verify(spec)

    # Forked from `release`: the base-only file is present in the new worktree.
    assert (workspace / "release.txt").exists()
    assert "feature/x" in git_info.get_branches(repo)


def test_remote_only_branch_is_attached_without_recreating_from_base(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    _git(repo, "checkout", "-q", "-b", "remote-source")
    (repo / "remote.txt").write_text("remote history\n")
    _git(repo, "add", "remote.txt")
    _git(repo, "commit", "-q", "-m", "remote history")
    remote_commit = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    _git(repo, "checkout", "-q", "main")
    _git(repo, "branch", "-D", "remote-source")
    _git(repo, "remote", "add", "origin", str(repo))
    _git(repo, "update-ref", "refs/remotes/origin/feature/remote", remote_commit)

    workspace = tmp_path / "wt" / "remote"
    evidence = wp.provision_and_verify(
        wp.WorkspaceSpec(
            workspace_path=str(workspace),
            expected_branch="feature/remote",
            base_branch="main",
            repository_path=str(repo),
        )
    )

    assert evidence.provision_outcome == "attached"
    assert _current_branch(workspace) == "feature/remote"
    assert (workspace / "remote.txt").read_text() == "remote history\n"


def test_existing_correct_worktree_is_reused_untouched(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "wt" / "reuse"
    _git(repo, "worktree", "add", "-b", "audit/execution-queue", str(workspace), "main")
    marker = workspace / "user_work.txt"
    marker.write_text("in progress\n")

    spec = wp.WorkspaceSpec(
        workspace_path=str(workspace),
        expected_branch="audit/execution-queue",
        base_branch="main",
        repository_path=str(repo),
    )
    evidence = wp.provision_and_verify(spec)

    assert evidence.provision_outcome == "reused"
    # Reuse never removes/resets: the user's in-progress file survives.
    assert marker.read_text() == "in progress\n"


def test_parallel_tasks_get_separate_worktrees(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    ws_a = tmp_path / "wt" / "a"
    ws_b = tmp_path / "wt" / "b"
    spec_a = wp.WorkspaceSpec(
        workspace_path=str(ws_a),
        expected_branch="task/a",
        base_branch="main",
        repository_path=str(repo),
    )
    spec_b = wp.WorkspaceSpec(
        workspace_path=str(ws_b),
        expected_branch="task/b",
        base_branch="main",
        repository_path=str(repo),
    )
    wp.provision_and_verify(spec_a)
    wp.provision_and_verify(spec_b)

    assert ws_a.resolve() != ws_b.resolve()
    assert _current_branch(ws_a) == "task/a"
    assert _current_branch(ws_b) == "task/b"


# --------------------------------------------------------------------------
# Verification — fail-closed gates
# --------------------------------------------------------------------------


def test_branch_mismatch_blocks_launch_exact_observed_case(tmp_path):
    """The exact production failure: expected `audit/execution-queue`, actual
    `main`, in the primary repository working tree."""
    repo = _make_repo(tmp_path / "repo")  # primary worktree on `main`
    spec = wp.WorkspaceSpec(
        workspace_path=str(repo),
        expected_branch="audit/execution-queue",
        base_branch="main",
        repository_path=str(repo),
        allow_provision=False,  # the workspace already exists (the bug's fallback)
    )
    with pytest.raises(wp.WorkspaceVerificationError) as exc_info:
        wp.verify_workspace(spec)

    err = exc_info.value
    assert err.failed_step == "branch_matches"
    assert err.expected_branch == "audit/execution-queue"
    assert err.actual_branch == "main"
    assert err.remediation
    structured = err.as_dict()
    assert structured["expected_branch"] == "audit/execution-queue"
    assert structured["actual_branch"] == "main"


def test_main_repository_cannot_be_used_for_a_feature_task(tmp_path):
    """Even when the feature branch is checked out *in the main repo* (branch
    matches), the primary working tree is refused for feature/audit work."""
    repo = _make_repo(tmp_path / "repo")
    _git(
        repo, "checkout", "-q", "-b", "audit/execution-queue"
    )  # primary tree, feature branch

    spec = wp.WorkspaceSpec(
        workspace_path=str(repo),
        expected_branch="audit/execution-queue",
        base_branch="main",
        repository_path=str(repo),
        allow_provision=False,
    )
    with pytest.raises(wp.WorkspaceVerificationError) as exc_info:
        wp.verify_workspace(spec)
    assert exc_info.value.failed_step == "isolated_worktree_required"


def test_wrong_repository_workspace_blocks_launch(tmp_path):
    repo_a = _make_repo(tmp_path / "repo_a")
    repo_b = _make_repo(tmp_path / "repo_b")
    # A worktree of repo_a, but the spec claims it belongs to repo_b.
    workspace = tmp_path / "wt_a"
    _git(repo_a, "worktree", "add", "-b", "feature/x", str(workspace), "main")

    spec = wp.WorkspaceSpec(
        workspace_path=str(workspace),
        expected_branch="feature/x",
        base_branch="main",
        repository_path=str(repo_b),  # wrong repo
        allow_provision=False,
    )
    with pytest.raises(wp.WorkspaceVerificationError) as exc_info:
        wp.verify_workspace(spec)
    assert exc_info.value.failed_step == "workspace_belongs_to_repository"


def test_conflicting_worktree_blocks_launch(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    existing = tmp_path / "wt" / "existing"
    _git(repo, "worktree", "add", "-b", "feature/x", str(existing), "main")

    # Ask to provision a *second* worktree for the same, already-checked-out branch.
    spec = wp.WorkspaceSpec(
        workspace_path=str(tmp_path / "wt" / "second"),
        expected_branch="feature/x",
        base_branch="main",
        repository_path=str(repo),
    )
    with pytest.raises(wp.WorkspaceVerificationError) as exc_info:
        wp.provision_and_verify(spec)
    assert exc_info.value.failed_step == "no_conflicting_worktree"


def test_missing_workspace_that_cannot_be_provisioned_fails_closed(tmp_path):
    """No fallback to repository_path: if the workspace is absent and cannot be
    provisioned (provisioning disabled), it is a hard failure, never a silent
    substitution of the source repository."""
    repo = _make_repo(tmp_path / "repo")
    spec = wp.WorkspaceSpec(
        workspace_path=str(tmp_path / "does_not_exist"),
        expected_branch="audit/execution-queue",
        base_branch="main",
        repository_path=str(repo),
        allow_provision=False,
    )
    with pytest.raises(wp.WorkspaceVerificationError) as exc_info:
        wp.provision_and_verify(spec)
    assert exc_info.value.failed_step == "workspace_exists"


def test_base_branch_must_exist(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "wt" / "x"
    _git(repo, "worktree", "add", "-b", "feature/x", str(workspace), "main")
    spec = wp.WorkspaceSpec(
        workspace_path=str(workspace),
        expected_branch="feature/x",
        base_branch="nonexistent-base",
        repository_path=str(repo),
        allow_provision=False,
    )
    with pytest.raises(wp.WorkspaceVerificationError) as exc_info:
        wp.verify_workspace(spec)
    assert exc_info.value.failed_step == "base_branch_exists"


def test_status_policy_clean_blocks_dirty_worktree(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "wt" / "x"
    _git(repo, "worktree", "add", "-b", "feature/x", str(workspace), "main")
    (workspace / "untracked.txt").write_text("dirty\n")

    strict = wp.WorkspaceSpec(
        workspace_path=str(workspace),
        expected_branch="feature/x",
        repository_path=str(repo),
        allow_provision=False,
        status_policy=wp.STATUS_POLICY_CLEAN,
    )
    with pytest.raises(wp.WorkspaceVerificationError) as exc_info:
        wp.verify_workspace(strict)
    assert exc_info.value.failed_step == "status_policy_satisfied"

    # Default policy tolerates a dirty tree.
    lenient = wp.WorkspaceSpec(
        workspace_path=str(workspace),
        expected_branch="feature/x",
        repository_path=str(repo),
        allow_provision=False,
    )
    evidence = wp.verify_workspace(lenient)
    assert evidence.actual_branch == "feature/x"


def test_main_line_task_in_primary_worktree_is_allowed(tmp_path):
    """A legitimate main-branch task running in the primary working tree must
    NOT be blocked — the isolation gate is only for feature/audit work."""
    repo = _make_repo(tmp_path / "repo")
    spec = wp.WorkspaceSpec(
        workspace_path=str(repo),
        expected_branch="main",
        base_branch="main",
        repository_path=str(repo),
        allow_provision=False,
    )
    evidence = wp.verify_workspace(spec)
    assert evidence.actual_branch == "main"
    assert all(check["passed"] for check in evidence.checks)


def test_verification_never_modifies_the_repository(tmp_path):
    """A failed verification against the main repo leaves it untouched — no
    stray files (the observed failure left 3 untracked files behind)."""
    repo = _make_repo(tmp_path / "repo")
    before = git_info.get_status(repo)
    spec = wp.WorkspaceSpec(
        workspace_path=str(repo),
        expected_branch="audit/execution-queue",
        base_branch="main",
        repository_path=str(repo),
        allow_provision=False,
    )
    with pytest.raises(wp.WorkspaceVerificationError):
        wp.verify_workspace(spec)
    after = git_info.get_status(repo)
    assert after["dirty"] is False
    assert after["status_lines"] == before["status_lines"] == []


# --------------------------------------------------------------------------
# Teardown — remove_workspace (VOYN-W0-AICC-ISOLATED-WORKTREE-PER-ATTEMPT)
# --------------------------------------------------------------------------


def test_remove_workspace_removes_a_clean_pipeline_owned_worktree(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "wt" / "task-a"
    spec = wp.WorkspaceSpec(
        workspace_path=str(workspace),
        expected_branch="task/a",
        base_branch="main",
        repository_path=str(repo),
    )
    wp.provision_and_verify(spec)
    assert workspace.is_dir()

    outcome = wp.remove_workspace(workspace, repo)

    assert outcome == "removed"
    assert not workspace.exists()
    # No dangling `.git/worktrees/<name>` entry left behind for `task/a`.
    assert all(
        entry.get("branch") != "task/a" for entry in git_info.get_worktrees(repo)
    )


def test_remove_workspace_on_an_already_removed_path_does_not_raise(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "wt" / "never-existed"

    assert wp.remove_workspace(workspace, repo) == "not_found"


def test_remove_workspace_twice_in_a_row_is_safe(tmp_path):
    """A second cleanup call (e.g. a retried handler, or a crash-recovery
    sweep) after the worktree is already gone must not raise or misreport."""
    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "wt" / "task-b"
    wp.provision_and_verify(
        wp.WorkspaceSpec(
            workspace_path=str(workspace),
            expected_branch="task/b",
            base_branch="main",
            repository_path=str(repo),
        )
    )

    first = wp.remove_workspace(workspace, repo)
    second = wp.remove_workspace(workspace, repo)

    assert first == "removed"
    assert second == "not_found"


def test_remove_workspace_refuses_the_primary_working_tree(tmp_path):
    """The safety boundary this shares with `is_pipeline_owned_worktree`:
    never remove the primary checkout, however it is asked."""
    repo = _make_repo(tmp_path / "repo")

    outcome = wp.remove_workspace(repo, repo)

    assert outcome == "not_owned"
    assert repo.is_dir() and (repo / "f.txt").exists()


def test_remove_workspace_refuses_a_different_repositorys_worktree(tmp_path):
    repo_a = _make_repo(tmp_path / "repo_a")
    repo_b = _make_repo(tmp_path / "repo_b")
    workspace = tmp_path / "wt_a"
    _git(repo_a, "worktree", "add", "-b", "feature/x", str(workspace), "main")

    outcome = wp.remove_workspace(workspace, repo_b)

    assert outcome == "not_owned"
    assert workspace.is_dir()


def test_remove_workspace_leaves_a_dirty_worktree_for_the_next_reuse(tmp_path):
    """A dirty worktree (uncommitted/untracked leftovers) refuses a plain
    `git worktree remove` -- deliberately not force-removed here, so an
    operator can still inspect it, and the identical path is simply reused
    ("reused") on the next provision_workspace call for the same branch."""
    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "wt" / "task-c"
    spec = wp.WorkspaceSpec(
        workspace_path=str(workspace),
        expected_branch="task/c",
        base_branch="main",
        repository_path=str(repo),
    )
    wp.provision_and_verify(spec)
    (workspace / "leftover.txt").write_text("uncommitted\n")

    outcome = wp.remove_workspace(workspace, repo)

    assert outcome == "remove_failed"
    assert workspace.is_dir()
    assert (workspace / "leftover.txt").exists()

    # The next provision call for the same branch reuses it untouched.
    reused = wp.provision_and_verify(spec)
    assert reused.provision_outcome == "reused"
    assert (workspace / "leftover.txt").exists()


# --------------------------------------------------------------------------
# prune_repository (periodic sweep primitive)
# --------------------------------------------------------------------------


def test_prune_repository_reconciles_metadata_left_by_a_directory_that_vanished(
    tmp_path,
):
    """Simulates the gap `remove_workspace`'s own inline prune cannot reach:
    a worker killed after `provision_workspace` but before any cleanup call,
    which leaves the worktree directory deleted (e.g. by the host reclaiming
    disk) but its `.git/worktrees/<name>` entry still registered."""
    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "wt" / "task-d"
    _git(repo, "worktree", "add", "-b", "task/d", str(workspace), "main")
    assert any(
        entry.get("branch") == "task/d" for entry in git_info.get_worktrees(repo)
    )

    shutil.rmtree(workspace)  # directory gone; metadata not yet reconciled

    outcome = wp.prune_repository(repo)

    assert outcome == "pruned"
    assert all(
        entry.get("branch") != "task/d" for entry in git_info.get_worktrees(repo)
    )


def test_prune_repository_is_a_noop_when_nothing_is_dangling(tmp_path):
    repo = _make_repo(tmp_path / "repo")

    assert wp.prune_repository(repo) == "pruned"


def test_prune_repository_never_touches_a_live_worktree(tmp_path):
    repo = _make_repo(tmp_path / "repo")
    workspace = tmp_path / "wt" / "task-e"
    _git(repo, "worktree", "add", "-b", "task/e", str(workspace), "main")

    outcome = wp.prune_repository(repo)

    assert outcome == "pruned"
    assert workspace.is_dir()
    assert any(
        entry.get("branch") == "task/e" for entry in git_info.get_worktrees(repo)
    )


def test_prune_repository_refuses_a_non_repository_path(tmp_path):
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()

    assert wp.prune_repository(not_a_repo) == "not_a_repository"


def test_prune_repository_refuses_a_missing_path(tmp_path):
    assert wp.prune_repository(tmp_path / "does-not-exist") == "not_a_repository"
