from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

from command_center import execution_queue, git_info, portfolio_launch, worktree_launcher
from command_center.portfolio_models import PortfolioTask, parse_card
from command_center.runtime import api as runtime_api

CARD_TEMPLATE = """---
schema_version: "1.0"
task_id: "{task_id}"
title: "Test task {task_id}"
project: "{project}"
type: "implementation"
capability: "none"
priority: "medium"
status: "{status}"
repository: "{repository}"
base_branch: "{base_branch}"
branch: {branch}
worktree: {worktree}
agent: null
autonomy: "confirmed"
parallel_group: null
requires: {requires}
blocks: []
conflicts_with: []
deliverables: ["a thing gets done"]
validation: ["true"]
stop_conditions: ["Stop once done."]
evidence: []
confidence: null
gated_by: []
---

# Test task {task_id}

## Objective

Do the test thing.
"""


def _current_branch(repo: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"], cwd=repo, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _yaml_str_or_null(value: str | None) -> str:
    return "null" if value is None else f'"{value}"'


def _write_card(
    tmp_path,
    *,
    task_id: str = "AICC-TEST-001",
    project: str = "AICC",
    status: str = "ready",
    repository: str = "~/Projects/ai-command-center",
    base_branch: str = "main",
    requires: str = "[]",
    branch: str | None = None,
    worktree: str | None = None,
):
    lane = status if status in ("ready", "review", "blocked", "backlog") else "ready"
    lane_dir = tmp_path / "portfolio" / "tasks" / lane / project
    lane_dir.mkdir(parents=True, exist_ok=True)
    path = lane_dir / f"{task_id}.md"
    text = CARD_TEMPLATE.format(
        task_id=task_id,
        project=project,
        status=status,
        repository=repository,
        base_branch=base_branch,
        requires=requires,
        branch=_yaml_str_or_null(branch),
        worktree=_yaml_str_or_null(worktree),
    )
    path.write_text(text, encoding="utf-8")
    return parse_card(path, lane=lane)


def _add_worktree(repo: Path, *, path: Path, branch: str, base: str = "HEAD") -> None:
    subprocess.run(["git", "worktree", "add", "-b", branch, str(path), base], cwd=repo, check=True)


@pytest.fixture
def portfolio_worktrees_root(tmp_path, monkeypatch):
    root = tmp_path / "worktrees"
    monkeypatch.setenv(portfolio_launch.WORKTREES_ROOT_ENV, str(root))
    return root


# --------------------------------------------------------------------------
# Naming convention
# --------------------------------------------------------------------------


def test_branch_and_worktree_naming_is_deterministic_and_lowercased(portfolio_worktrees_root):
    assert portfolio_launch.branch_name_for("AICC-UI-001") == "task/aicc-ui-001"
    assert portfolio_launch.worktree_path_for("AICC-UI-001") == portfolio_worktrees_root / "aicc-ui-001"


# --------------------------------------------------------------------------
# task_id path-safety (Founder Gate re-review Blocker 1) — `task_id` becomes
# a filesystem path component (branch name, worktree directory, per-task
# lock filename) in this module. `parse_card` already rejects an unsafe
# `task_id` at parse time (see `test_portfolio_models.py`); everything below
# proves the *second*, independent gate inside this module also holds, by
# calling the low-level helpers directly — bypassing the parser entirely, as
# a hand-built `PortfolioTask` or a future call site that forgets to go
# through `parse_card` would.
# --------------------------------------------------------------------------

_UNSAFE_TASK_IDS = [
    "../escaped_dir/evil",
    "../../OUTSIDE_LOCK_ESCAPE",
    "foo/bar",
    "foo\\bar",
    ".",
    "..",
    " leading-space",
    "trailing-space ",
    "foo bar",
    "/absolute/path",
]


@pytest.mark.parametrize("bad_task_id", _UNSAFE_TASK_IDS)
def test_branch_name_for_rejects_unsafe_task_id(portfolio_worktrees_root, bad_task_id):
    with pytest.raises(portfolio_launch.PortfolioLaunchError):
        portfolio_launch.branch_name_for(bad_task_id)


@pytest.mark.parametrize("bad_task_id", _UNSAFE_TASK_IDS)
def test_worktree_path_for_rejects_unsafe_task_id_and_creates_nothing_outside_sandbox(
    portfolio_worktrees_root, bad_task_id
):
    """Direct repro of the Founder Gate re-review Blocker 1 path traversal:
    `worktree_path_for("../escaped_dir/evil")` used to return
    `<worktrees_root>/../escaped_dir/evil`, and `create_worktree` would then
    `mkdir(parents=True)` that escaped directory into existence before git
    ever validated the (also escaped) branch name. Confirms both the raise
    and that nothing new appears anywhere near the sandbox root."""
    parent = portfolio_worktrees_root.parent
    siblings_before = set(parent.iterdir()) if parent.exists() else set()

    with pytest.raises(portfolio_launch.PortfolioLaunchError):
        portfolio_launch.worktree_path_for(bad_task_id)

    siblings_after = set(parent.iterdir()) if parent.exists() else set()
    assert siblings_after == siblings_before


def test_claim_rejects_unsafe_task_id_and_creates_no_lock_file(tmp_path):
    """Direct repro for the per-task claim lock path, which is built
    independently of `branch_name_for`/`worktree_path_for` and does not go
    through git validation at all — `os.open(..., O_CREAT)` would otherwise
    unconditionally create a real file at the escaped path."""
    root = tmp_path / "aicc_root"
    with pytest.raises(portfolio_launch.PortfolioLaunchError):
        portfolio_launch._claim(root, "../../OUTSIDE_LOCK_ESCAPE")
    assert not root.exists()
    assert not (tmp_path / "OUTSIDE_LOCK_ESCAPE.lock").exists()


def test_release_rejects_unsafe_task_id(tmp_path):
    root = tmp_path / "aicc_root"
    with pytest.raises(portfolio_launch.PortfolioLaunchError):
        portfolio_launch._release(root, "../../OUTSIDE_LOCK_ESCAPE")


def test_build_launch_plan_blocks_unsafe_task_id_without_raising(git_repo, tmp_path, portfolio_worktrees_root):
    """`build_launch_plan` is documented pure/non-raising even for a
    `task_id` invalid enough that generated-default resolution rejects it —
    surfaced as an ordinary plan blocker, never an unhandled exception, and
    never `launchable=True`. Uses a hand-built `PortfolioTask` since
    `parse_card` itself already refuses to produce one with this `task_id`."""
    base_branch = _current_branch(git_repo)
    task = PortfolioTask(
        lane="ready",
        source_path=tmp_path / "malicious.md",
        frontmatter={
            "task_id": "../escaped_dir/evil",
            "project": "AICC",
            "title": "malicious card",
            "repository": str(git_repo),
            "base_branch": base_branch,
            "status": "ready",
        },
        body="",
        raw_text="",
    )

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert plan.launchable is False
    assert plan.blockers
    assert not (portfolio_worktrees_root.parent / "escaped_dir").exists()


def test_assert_within_root_rejects_crafted_path_outside_root(tmp_path):
    """Unit test of the containment check itself, independent of `task_id`
    validation — the belt-and-suspenders layer for a `task_id`-derived path
    construction that changes in the future, or a symlinked ancestor
    directory that redirects an otherwise valid-looking candidate outside
    its intended root."""
    root = tmp_path / "root"
    root.mkdir()
    escaping_candidate = tmp_path / "elsewhere" / "evil"
    with pytest.raises(portfolio_launch.PortfolioLaunchError):
        portfolio_launch._assert_within_root(escaping_candidate, root, what="worktree")


def test_assert_within_root_accepts_contained_path(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    contained_candidate = root / "task-001"
    portfolio_launch._assert_within_root(contained_candidate, root, what="worktree")  # must not raise


@pytest.mark.parametrize("good_task_id", ["TASK-001", "task_001", "task-001", "A1", "portfolio_task_7"])
def test_branch_name_for_and_worktree_path_for_accept_safe_task_id(portfolio_worktrees_root, good_task_id):
    assert portfolio_launch.branch_name_for(good_task_id) == f"task/{good_task_id.lower()}"
    assert portfolio_launch.worktree_path_for(good_task_id) == portfolio_worktrees_root / good_task_id.lower()


# --------------------------------------------------------------------------
# Dry-run planning — pure, read-only
# --------------------------------------------------------------------------


def test_build_launch_plan_happy_path(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert plan.launchable, plan.blockers
    assert plan.branch == "task/aicc-test-001"
    assert plan.worktree == str(portfolio_worktrees_root / "aicc-test-001")
    expected_sha = subprocess.run(
        ["git", "rev-parse", base_branch], cwd=git_repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    assert plan.base_sha == expected_sha
    assert plan.repository_root == str(git_repo.resolve())


def test_build_launch_plan_ignores_dirty_base_repository(git_repo, tmp_path, portfolio_worktrees_root):
    (git_repo / "dirty.txt").write_text("uncommitted", encoding="utf-8")
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert plan.launchable is True


def test_build_launch_plan_blocks_when_project_not_mapped(tmp_path, portfolio_worktrees_root):
    task = _write_card(tmp_path)
    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={})
    assert not plan.launchable
    assert any("не сопоставлен" in b for b in plan.blockers)


def test_build_launch_plan_blocks_when_repository_path_missing(tmp_path, portfolio_worktrees_root):
    task = _write_card(tmp_path)
    missing_path = str(tmp_path / "does-not-exist")
    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": missing_path})
    assert not plan.launchable
    assert any("не существует" in b for b in plan.blockers)


def test_build_launch_plan_blocks_when_path_is_not_a_git_repository(tmp_path, portfolio_worktrees_root):
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    task = _write_card(tmp_path)
    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(not_a_repo)})
    assert not plan.launchable
    assert any("не является git-репозиторием" in b for b in plan.blockers)


def test_build_launch_plan_blocks_when_base_branch_ref_missing(git_repo, tmp_path, portfolio_worktrees_root):
    task = _write_card(tmp_path, base_branch="does-not-exist-branch", repository=str(git_repo))
    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert not plan.launchable
    assert any("не найдена" in b for b in plan.blockers)


def test_build_launch_plan_blocks_non_ready_lane(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, status="blocked", base_branch=base_branch, repository=str(git_repo))
    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert not plan.launchable
    assert any("ready" in b for b in plan.blockers)


def test_build_launch_plan_blocks_unmet_requirements(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    blocker_task = _write_card(
        tmp_path, task_id="AICC-BLOCKER", status="blocked", base_branch=base_branch, repository=str(git_repo)
    )
    dependent = _write_card(
        tmp_path,
        task_id="AICC-DEPENDENT",
        base_branch=base_branch,
        repository=str(git_repo),
        requires='["AICC-BLOCKER"]',
    )
    tasks_by_id = {"AICC-BLOCKER": blocker_task, "AICC-DEPENDENT": dependent}

    plan = portfolio_launch.build_launch_plan(
        dependent, tasks_by_id=tasks_by_id, repository_paths={"AICC": str(git_repo)}
    )
    assert not plan.launchable
    assert any("AICC-BLOCKER" in b for b in plan.blockers)


def test_build_launch_plan_blocks_already_registered_task(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    registry = {task.task_id: {"run_id": "r1"}}

    plan = portfolio_launch.build_launch_plan(
        task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)}, registry=registry
    )
    assert not plan.launchable
    assert any("уже была запущена" in b for b in plan.blockers)


def test_build_launch_plan_blocks_existing_branch(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    subprocess.run(["git", "branch", "task/aicc-test-001"], cwd=git_repo, check=True)

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert not plan.launchable
    assert any("ветка уже существует" in b for b in plan.blockers)


def test_build_launch_plan_blocks_existing_worktree_path_that_is_not_a_git_repo(
    git_repo, tmp_path, portfolio_worktrees_root
):
    """A bare directory sitting at the resolved (generated-default) worktree
    path is treated as "existing worktree mode" (never re-created via `git
    worktree add`), but fails validation because it isn't a git repository at
    all — a different, more specific blocker than the old blanket "path
    already exists"."""
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    portfolio_launch.worktree_path_for(task.task_id).mkdir(parents=True)

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert not plan.launchable
    assert plan.worktree_mode == portfolio_launch.WORKTREE_MODE_EXISTING
    assert any("не является git-репозиторием" in b for b in plan.blockers)


def test_build_launch_plan_dry_run_makes_no_mutation(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    branches_before = set(git_info.get_branches(git_repo))

    portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert set(git_info.get_branches(git_repo)) == branches_before
    assert not portfolio_launch.worktree_path_for(task.task_id).exists()
    assert execution_queue.load_queue(tmp_path) == []


# --------------------------------------------------------------------------
# Prompt generation
# --------------------------------------------------------------------------


def test_build_agent_prompt_contains_card_and_safety_instructions(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    prompt = portfolio_launch.build_agent_prompt(task, plan)

    assert task.raw_text in prompt
    assert plan.branch in prompt
    assert plan.worktree in prompt
    assert plan.base_sha in prompt
    assert "git merge" in prompt
    assert "git push" in prompt
    assert "## Verdict" in prompt
    assert "## Findings" in prompt
    assert "Branch:" in prompt


# --------------------------------------------------------------------------
# Real launch — worktree/branch creation, queue/launcher reuse, rollback
# --------------------------------------------------------------------------


def test_launch_portfolio_task_requires_explicit_confirmation(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    with pytest.raises(portfolio_launch.PortfolioLaunchError):
        portfolio_launch.launch_portfolio_task(
            tmp_path, task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
            execution_center_api=api, confirmed=False,
        )


def test_launch_portfolio_task_creates_worktree_branch_and_launches(
    git_repo, tmp_path, fake_claude, portfolio_worktrees_root
):
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    result = portfolio_launch.launch_portfolio_task(
        tmp_path, task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )

    try:
        assert result.launched is True, result.message
        assert result.run_id is not None

        worktree_path = Path(result.plan.worktree)
        assert worktree_path.is_dir()
        assert (worktree_path / ".git").exists()
        assert result.plan.branch in git_info.get_branches(git_repo)

        registry = portfolio_launch.load_registry(tmp_path)
        assert task.task_id in registry
        assert registry[task.task_id]["run_id"] == result.run_id
        assert registry[task.task_id]["branch"] == result.plan.branch

        entries = execution_queue.load_queue(tmp_path)
        entry = next(e for e in entries if e["task_id"] == task.task_id)
        assert entry["state"] == execution_queue.STATE_LAUNCHED
        assert entry["run_id"] == result.run_id
    finally:
        if result.run_id:
            api.request_cancel(result.run_id, confirmed=True)


def test_launch_portfolio_task_is_blocked_a_second_time_for_same_task(
    git_repo, tmp_path, fake_claude, portfolio_worktrees_root
):
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    first = portfolio_launch.launch_portfolio_task(
        tmp_path, task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )
    assert first.launched is True

    # Re-parse the same card fresh (simulating a second click reading the
    # card again) rather than reusing `task`, to prove the guard is durable
    # state, not an artifact of Python object identity.
    task_again = parse_card(task.source_path, lane=task.lane)
    second = portfolio_launch.launch_portfolio_task(
        tmp_path, task_again, tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )

    assert second.launched is False
    assert "уже была запущена" in second.message
    assert len(git_info.get_worktrees(git_repo)) == 2  # primary + the one worktree from `first`

    api.request_cancel(first.run_id, confirmed=True)


def test_claim_prevents_concurrent_double_launch(tmp_path):
    assert portfolio_launch._claim(tmp_path, "AICC-X") is True
    assert portfolio_launch._claim(tmp_path, "AICC-X") is False
    portfolio_launch._release(tmp_path, "AICC-X")
    assert portfolio_launch._claim(tmp_path, "AICC-X") is True
    portfolio_launch._release(tmp_path, "AICC-X")


def test_orphaned_claim_is_detected_and_recovered_automatically(tmp_path, monkeypatch):
    task_id = "AICC-CRASHED"
    lock_path = portfolio_launch._claim_lock_path(tmp_path, task_id)
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(
        json.dumps(
            {
                "version": portfolio_launch.CLAIM_LOCK_VERSION,
                "pid": 424242,
                "hostname": portfolio_launch.socket.gethostname(),
                "process_identity": "dead-process",
                "created_at": 1.0,
                "token": "orphan-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(portfolio_launch, "_pid_is_alive", lambda pid: False)

    status = portfolio_launch.inspect_claim(tmp_path, task_id, now=100.0)
    assert status.stale is True
    assert status.recoverable is True
    assert status.owner_pid == 424242

    # The ordinary claim path performs recovery; no file deletion or other
    # operator action is required after the simulated process crash.
    assert portfolio_launch._claim(tmp_path, task_id) is True
    replacement = json.loads(lock_path.read_text(encoding="utf-8"))
    assert replacement["pid"] == os.getpid()
    assert replacement["token"] != "orphan-token"
    portfolio_launch._release(tmp_path, task_id)


def test_live_owner_claim_is_never_recovered_even_when_old(tmp_path, monkeypatch):
    task_id = "AICC-LIVE"
    lock_path = portfolio_launch._claim_lock_path(tmp_path, task_id)
    lock_path.parent.mkdir(parents=True)
    metadata = {
        "version": portfolio_launch.CLAIM_LOCK_VERSION,
        "pid": 31337,
        "hostname": portfolio_launch.socket.gethostname(),
        "process_identity": "same-live-process",
        "created_at": 1.0,
        "token": "live-token",
    }
    lock_path.write_text(json.dumps(metadata), encoding="utf-8")
    os.utime(lock_path, (1, 1))
    monkeypatch.setattr(portfolio_launch, "_pid_is_alive", lambda pid: True)
    monkeypatch.setattr(portfolio_launch, "_process_identity", lambda pid: "same-live-process")

    status = portfolio_launch.inspect_claim(tmp_path, task_id, now=10_000_000.0)
    assert status.age_seconds and status.age_seconds > 1_000_000
    assert status.stale is False
    assert status.recoverable is False
    assert portfolio_launch.recover_stale_claim(tmp_path, task_id) is False
    assert portfolio_launch._claim(tmp_path, task_id) is False
    assert json.loads(lock_path.read_text(encoding="utf-8"))["token"] == "live-token"


def test_claim_from_a_different_host_is_never_recovered(tmp_path, monkeypatch):
    # `inspect_claim`'s own docstring names this exact scenario: a claim
    # written by a different host has a PID that cannot be checked locally,
    # so it must be reported unrecoverable regardless of age -- the same
    # "a live owner is never displaced" guarantee the same-host tests above
    # pin, extended across the cross-host branch that had no coverage at all.
    task_id = "AICC-OTHER-HOST"
    lock_path = portfolio_launch._claim_lock_path(tmp_path, task_id)
    lock_path.parent.mkdir(parents=True)
    metadata = {
        "version": portfolio_launch.CLAIM_LOCK_VERSION,
        "pid": 31337,
        "hostname": "some-other-worker-host",
        "process_identity": "remote-process",
        "created_at": 1.0,
        "token": "remote-token",
    }
    lock_path.write_text(json.dumps(metadata), encoding="utf-8")
    os.utime(lock_path, (1, 1))

    def _fail_if_called(pid):
        raise AssertionError("a foreign host's PID must never be checked locally")

    monkeypatch.setattr(portfolio_launch, "_pid_is_alive", _fail_if_called)

    status = portfolio_launch.inspect_claim(tmp_path, task_id, now=10_000_000.0)
    assert status.exists is True
    assert status.age_seconds and status.age_seconds > 1_000_000
    assert status.stale is False
    assert status.recoverable is False
    assert status.owner_pid == 31337
    assert "some-other-worker-host" in status.reason

    assert portfolio_launch.recover_stale_claim(tmp_path, task_id) is False
    assert portfolio_launch._claim(tmp_path, task_id) is False
    assert json.loads(lock_path.read_text(encoding="utf-8"))["token"] == "remote-token"


def test_claim_with_reused_pid_but_different_identity_is_recovered(tmp_path, monkeypatch):
    # The PID is alive, but the process running under it is a DIFFERENT one
    # than the claim recorded (PID reuse after a crash). This is the entire
    # reason process_identity exists; it must make the claim recoverable.
    task_id = "AICC-PID-REUSE"
    lock_path = portfolio_launch._claim_lock_path(tmp_path, task_id)
    lock_path.parent.mkdir(parents=True)
    metadata = {
        "version": portfolio_launch.CLAIM_LOCK_VERSION,
        "pid": 31337,
        "hostname": portfolio_launch.socket.gethostname(),
        "process_identity": "original-crashed-process",
        "created_at": 1.0,
        "token": "orphan-token",
    }
    lock_path.write_text(json.dumps(metadata), encoding="utf-8")
    monkeypatch.setattr(portfolio_launch, "_pid_is_alive", lambda pid: True)
    monkeypatch.setattr(
        portfolio_launch, "_process_identity", lambda pid: "a-different-live-process"
    )

    status = portfolio_launch.inspect_claim(tmp_path, task_id)
    assert status.recoverable is True
    assert "переиспользован" in status.reason
    # And a fresh claim succeeds, replacing the orphaned token.
    assert portfolio_launch._claim(tmp_path, task_id) is True
    assert json.loads(lock_path.read_text(encoding="utf-8"))["token"] != "orphan-token"


def test_old_owner_release_cannot_delete_recovered_replacement_claim(tmp_path):
    task_id = "AICC-TOKEN-RACE"
    lock_path = portfolio_launch._claim_lock_path(tmp_path, task_id)
    assert portfolio_launch._claim(tmp_path, task_id) is True
    old_token = json.loads(lock_path.read_text(encoding="utf-8"))["token"]

    # Simulate recovery by another claimant after the original process died,
    # then a delayed finally-block from the original owner. Token matching is
    # the backstop that protects the replacement claim.
    replacement = {
        "version": portfolio_launch.CLAIM_LOCK_VERSION,
        "pid": os.getpid(),
        "hostname": portfolio_launch.socket.gethostname(),
        "process_identity": portfolio_launch._process_identity(os.getpid()),
        "created_at": 2.0,
        "token": "replacement-token",
    }
    lock_path.write_text(json.dumps(replacement), encoding="utf-8")
    assert old_token != replacement["token"]

    portfolio_launch._release(tmp_path, task_id)
    assert lock_path.exists()
    assert json.loads(lock_path.read_text(encoding="utf-8"))["token"] == "replacement-token"


def test_legacy_claim_is_reported_but_not_unsafely_removed_by_age(tmp_path):
    task_id = "AICC-LEGACY"
    lock_path = portfolio_launch._claim_lock_path(tmp_path, task_id)
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("", encoding="utf-8")
    os.utime(lock_path, (1, 1))

    status = portfolio_launch.inspect_claim(tmp_path, task_id, now=10_000_000.0)
    assert status.exists is True
    assert status.recoverable is False
    assert portfolio_launch._claim(tmp_path, task_id) is False
    assert lock_path.exists()


def test_create_worktree_raises_portfolio_launch_error_on_git_failure(git_repo, tmp_path):
    with pytest.raises(portfolio_launch.PortfolioLaunchError):
        portfolio_launch.create_worktree(
            git_repo, branch="task/x", worktree_path=tmp_path / "wt", base_branch="does-not-exist-branch"
        )


def test_remove_worktree_and_delete_branch_are_best_effort_and_never_raise(git_repo, tmp_path):
    portfolio_launch.remove_worktree(git_repo, tmp_path / "nonexistent-worktree")
    portfolio_launch.delete_branch(git_repo, "no-such-branch")


def test_launch_portfolio_task_rolls_back_worktree_and_branch_on_launch_failure(
    git_repo, tmp_path, monkeypatch, portfolio_worktrees_root
):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    persisted_calls: list[str] = []
    real_enqueue = execution_queue.enqueue_and_persist
    real_dequeue = execution_queue.dequeue_and_persist

    def spy_enqueue(root, synthetic_task, tasks_by_id):
        persisted_calls.append("enqueue")
        return real_enqueue(root, synthetic_task, tasks_by_id)

    def spy_dequeue(root, entry_id):
        persisted_calls.append("dequeue")
        return real_dequeue(root, entry_id)

    def failing_launch_ready(root, entries, tasks, tasks_by_id, project_configs, execution_center_api, *, entry_ids=None):
        updated = [dict(e) for e in entries]
        results = [
            execution_queue.LaunchAttemptResult(entry_ids[0], task.task_id, False, message="forced failure for test")
        ]
        return updated, results

    monkeypatch.setattr(portfolio_launch.execution_queue, "launch_ready", failing_launch_ready)
    monkeypatch.setattr(portfolio_launch.execution_queue, "enqueue_and_persist", spy_enqueue)
    monkeypatch.setattr(portfolio_launch.execution_queue, "dequeue_and_persist", spy_dequeue)

    result = portfolio_launch.launch_portfolio_task(
        tmp_path, task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )

    assert result.launched is False
    assert "forced failure" in result.message
    assert not Path(result.plan.worktree).exists()
    assert result.plan.branch not in git_info.get_branches(git_repo)
    assert task.task_id not in portfolio_launch.load_registry(tmp_path)
    assert execution_queue.load_queue(tmp_path) == []
    assert persisted_calls == ["enqueue", "dequeue"]

    # The claim must have been released so a retry is possible.
    assert portfolio_launch._claim(tmp_path, task.task_id) is True
    portfolio_launch._release(tmp_path, task.task_id)


def test_concurrent_rollback_does_not_lose_a_parallel_successful_registration(
    git_repo, tmp_path, monkeypatch, fake_claude, portfolio_worktrees_root
):
    """Founder Gate Major-1 rollback/release-path regression: a launch that
    fails and rolls back must never be able to clobber a *different* task's
    registry entry. The rollback path itself never touches the registry
    (only a fully successful launch does), so this test's two tasks never
    actually contend for `_registry_lock` at the same time — the failing
    task's `launch_ready` is forced to fail before it would ever reach
    `_persist_registry_entry`. What this proves is that running a failing
    launch and a succeeding launch concurrently (real threads, real
    rollback) doesn't let the failure's cleanup path interfere with the
    other task's registration. The actual concurrent-registry-write race,
    with genuine write contention, is covered separately in
    `test_portfolio_registry_concurrency.py` (Founder Gate re-review
    Minor 1)."""
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    base_branch = _current_branch(git_repo)
    ok_task = _write_card(tmp_path, task_id="AICC-RACE-OK", base_branch=base_branch, repository=str(git_repo))
    fail_task = _write_card(tmp_path, task_id="AICC-RACE-FAIL", base_branch=base_branch, repository=str(git_repo))
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    real_launch_ready = execution_queue.launch_ready

    def selective_launch_ready(root, entries, tasks, tasks_by_id, project_configs, execution_center_api, *, entry_ids=None):
        if any(t["id"] == "AICC-RACE-FAIL" for t in tasks):
            updated = [dict(e) for e in entries]
            results = [
                execution_queue.LaunchAttemptResult(
                    entry_ids[0], "AICC-RACE-FAIL", False, message="forced failure for test"
                )
            ]
            return updated, results
        return real_launch_ready(
            root, entries, tasks, tasks_by_id, project_configs, execution_center_api, entry_ids=entry_ids
        )

    monkeypatch.setattr(portfolio_launch.execution_queue, "launch_ready", selective_launch_ready)

    start = threading.Barrier(2)
    results: dict[str, portfolio_launch.PortfolioLaunchResult] = {}

    def run(task, key):
        start.wait()
        results[key] = portfolio_launch.launch_portfolio_task(
            tmp_path, task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
            execution_center_api=api, confirmed=True,
        )

    threads = [
        threading.Thread(target=run, args=(ok_task, "ok")),
        threading.Thread(target=run, args=(fail_task, "fail")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    try:
        assert results["ok"].launched is True, results["ok"].message
        assert results["fail"].launched is False

        registry = portfolio_launch.load_registry(tmp_path)
        assert "AICC-RACE-OK" in registry
        assert "AICC-RACE-FAIL" not in registry
        assert not Path(results["fail"].plan.worktree).exists()
        assert results["fail"].plan.branch not in git_info.get_branches(git_repo)
    finally:
        if results.get("ok") and results["ok"].run_id:
            api.request_cancel(results["ok"].run_id, confirmed=True)


def test_launch_batch_persists_entries_via_the_shared_registry_lock_mechanism(
    git_repo, tmp_path, fake_claude, portfolio_worktrees_root, monkeypatch
):
    """Founder Gate Major-1 requirement: batch launch must reuse the exact
    same atomic registry write path as a single launch (`_persist_registry_
    entry`, backed by `_registry_lock`), not a separate, potentially unsafe
    implementation."""
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    base_branch = _current_branch(git_repo)
    task_a = _write_card(tmp_path, task_id="AICC-BATCH-LOCK-A", base_branch=base_branch, repository=str(git_repo))
    task_b = _write_card(tmp_path, task_id="AICC-BATCH-LOCK-B", base_branch=base_branch, repository=str(git_repo))
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    calls: list[str] = []
    real_persist = portfolio_launch._persist_registry_entry

    def spy_persist(root, task_id, entry):
        calls.append(task_id)
        return real_persist(root, task_id, entry)

    monkeypatch.setattr(portfolio_launch, "_persist_registry_entry", spy_persist)

    results = portfolio_launch.launch_batch(
        tmp_path, [task_a, task_b], tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )

    try:
        assert all(r.launched for r in results), [r.message for r in results]
        assert sorted(calls) == ["AICC-BATCH-LOCK-A", "AICC-BATCH-LOCK-B"]
        registry = portfolio_launch.load_registry(tmp_path)
        assert "AICC-BATCH-LOCK-A" in registry
        assert "AICC-BATCH-LOCK-B" in registry
    finally:
        for r in results:
            if r.run_id:
                api.request_cancel(r.run_id, confirmed=True)


# --------------------------------------------------------------------------
# Batch launch
# --------------------------------------------------------------------------


def test_launch_batch_with_valid_and_invalid_tasks(git_repo, tmp_path, fake_claude, portfolio_worktrees_root):
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    base_branch = _current_branch(git_repo)
    valid = _write_card(tmp_path, task_id="AICC-VALID-1", base_branch=base_branch, repository=str(git_repo))
    invalid = _write_card(
        tmp_path, task_id="AICC-INVALID-1", status="blocked", base_branch=base_branch, repository=str(git_repo)
    )
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    results = portfolio_launch.launch_batch(
        tmp_path, [valid, invalid], tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )

    assert results[0].launched is True
    assert results[1].launched is False
    assert any("ready" in b for b in results[1].plan.blockers)

    api.request_cancel(results[0].run_id, confirmed=True)


def test_launch_batch_respects_concurrency_limit(git_repo, tmp_path, fake_claude, portfolio_worktrees_root):
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    base_branch = _current_branch(git_repo)
    task_a = _write_card(tmp_path, task_id="AICC-BATCH-A", base_branch=base_branch, repository=str(git_repo))
    task_b = _write_card(tmp_path, task_id="AICC-BATCH-B", base_branch=base_branch, repository=str(git_repo))
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    results = portfolio_launch.launch_batch(
        tmp_path, [task_a, task_b], tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True, max_concurrent=1,
    )

    assert results[0].launched is True
    assert results[1].launched is False
    assert "лимит" in results[1].message

    api.request_cancel(results[0].run_id, confirmed=True)


# --------------------------------------------------------------------------
# Branch / worktree override resolution
# --------------------------------------------------------------------------


def test_explicit_branch_override_is_used(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(
        tmp_path, base_branch=base_branch, repository=str(git_repo), branch="feature/my-explicit-branch"
    )

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert plan.launchable, plan.blockers
    assert plan.branch == "feature/my-explicit-branch"
    assert plan.requested_branch == "feature/my-explicit-branch"
    assert plan.branch_source == portfolio_launch.SOURCE_CARD_OVERRIDE


def test_explicit_worktree_override_is_used(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    override_path = tmp_path / "my-explicit-worktree"
    task = _write_card(
        tmp_path, base_branch=base_branch, repository=str(git_repo), worktree=str(override_path)
    )

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert plan.launchable, plan.blockers
    assert plan.worktree == str(override_path)
    assert plan.requested_worktree == str(override_path)
    assert plan.worktree_source == portfolio_launch.SOURCE_CARD_OVERRIDE
    assert plan.worktree_mode == portfolio_launch.WORKTREE_MODE_NEW


def test_absent_branch_falls_back_to_generated_default(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))  # branch=None

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert plan.branch == "task/aicc-test-001"
    assert plan.branch_source == portfolio_launch.SOURCE_GENERATED_DEFAULT
    assert plan.requested_branch is None


def test_absent_worktree_falls_back_to_generated_default(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))  # worktree=None

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert plan.worktree == str(portfolio_worktrees_root / "aicc-test-001")
    assert plan.worktree_source == portfolio_launch.SOURCE_GENERATED_DEFAULT
    assert plan.requested_worktree is None


def test_whitespace_only_branch_override_is_blocked_not_silently_defaulted(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo), branch="   ")

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert not plan.launchable
    assert plan.branch == ""
    assert any("branch" in b for b in plan.blockers)


def test_whitespace_only_worktree_override_is_blocked_not_silently_defaulted(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo), worktree="   ")

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert not plan.launchable
    assert plan.worktree == ""
    assert any("worktree" in b for b in plan.blockers)


def test_malformed_branch_override_is_blocked(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo), branch="bad..name")

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert not plan.launchable
    assert any("недопустим" in b for b in plan.blockers)


@pytest.mark.parametrize(
    "protected_branch",
    [
        *sorted(portfolio_launch.PROTECTED_BRANCH_NAMES),
        # Case variants resolve to the same loose ref on a case-insensitive
        # filesystem, so they must be caught by the explicit guard too — not
        # left to `git worktree add`'s "a branch named 'Main' already exists".
        *sorted(name.capitalize() for name in portfolio_launch.PROTECTED_BRANCH_NAMES),
        *sorted(name.upper() for name in portfolio_launch.PROTECTED_BRANCH_NAMES),
    ],
)
def test_launch_rejects_protected_branch_before_worktree_add(
    git_repo, tmp_path, portfolio_worktrees_root, monkeypatch, protected_branch
):
    task = _write_card(
        tmp_path,
        base_branch=_current_branch(git_repo),
        repository=str(git_repo),
        branch=protected_branch,
    )
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    def unexpected_create_worktree(*args, **kwargs):
        pytest.fail("create_worktree must not be called for a protected branch")

    monkeypatch.setattr(portfolio_launch, "create_worktree", unexpected_create_worktree)

    result = portfolio_launch.launch_portfolio_task(
        tmp_path,
        task,
        tasks_by_id={},
        repository_paths={"AICC": str(git_repo)},
        execution_center_api=api,
        confirmed=True,
    )

    assert result.launched is False
    assert not result.plan.launchable
    assert "защищённую ветку" in result.message
    assert f"«{protected_branch}»" in result.message
    assert not Path(result.plan.worktree).exists()


def test_relative_worktree_override_is_blocked_as_ambiguous(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo), worktree="relative/path")

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert not plan.launchable
    assert any("абсолютным" in b for b in plan.blockers)


def test_existing_valid_worktree_is_accepted_and_causes_no_worktree_add(
    git_repo, tmp_path, fake_claude, portfolio_worktrees_root
):
    existing_path = tmp_path / "existing-worktree"
    _add_worktree(git_repo, path=existing_path, branch="existing-branch")
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"

    base_branch = _current_branch(git_repo)
    task = _write_card(
        tmp_path, base_branch=base_branch, repository=str(git_repo),
        branch="existing-branch", worktree=str(existing_path),
    )

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert plan.launchable, plan.blockers
    assert plan.worktree_mode == portfolio_launch.WORKTREE_MODE_EXISTING

    worktrees_before = git_info.get_worktrees(git_repo)
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")
    result = portfolio_launch.launch_portfolio_task(
        tmp_path, task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )

    try:
        assert result.launched is True, result.message
        # No new worktree was created — `git worktree add` was never called.
        assert git_info.get_worktrees(git_repo) == worktrees_before
    finally:
        if result.run_id:
            api.request_cancel(result.run_id, confirmed=True)


def test_existing_worktree_on_wrong_branch_is_blocked(git_repo, tmp_path, portfolio_worktrees_root):
    existing_path = tmp_path / "existing-worktree"
    _add_worktree(git_repo, path=existing_path, branch="some-other-branch")

    base_branch = _current_branch(git_repo)
    task = _write_card(
        tmp_path, base_branch=base_branch, repository=str(git_repo),
        branch="a-different-expected-branch", worktree=str(existing_path),
    )

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert not plan.launchable
    assert any("сменил ветку" in b or "на ветке" in b for b in plan.blockers)


def test_existing_worktree_for_wrong_repository_is_blocked(git_repo, tmp_path, portfolio_worktrees_root):
    other_repo = tmp_path / "other-repo"
    other_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=other_repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=other_repo, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=other_repo, check=True)
    (other_repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "f.txt"], cwd=other_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=other_repo, check=True)

    existing_path = tmp_path / "existing-worktree-other-repo"
    _add_worktree(other_repo, path=existing_path, branch="some-branch")

    base_branch = _current_branch(git_repo)
    task = _write_card(
        tmp_path, base_branch=base_branch, repository=str(git_repo),
        branch="some-branch", worktree=str(existing_path),
    )

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert not plan.launchable
    assert any("другому репозиторию" in b for b in plan.blockers)


def test_dirty_existing_worktree_is_blocked(git_repo, tmp_path, portfolio_worktrees_root):
    existing_path = tmp_path / "existing-worktree"
    _add_worktree(git_repo, path=existing_path, branch="existing-branch")
    (existing_path / "dirty.txt").write_text("uncommitted")

    base_branch = _current_branch(git_repo)
    task = _write_card(
        tmp_path, base_branch=base_branch, repository=str(git_repo),
        branch="existing-branch", worktree=str(existing_path),
    )

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert not plan.launchable
    assert any("не чист" in b for b in plan.blockers)


def test_existing_directory_that_is_not_a_registered_worktree_is_blocked(git_repo, tmp_path, portfolio_worktrees_root):
    """An existing directory that shares the mapped repository's git-common-dir
    but is not actually a *registered* worktree (a hand-built `.git` gitfile /
    a copied worktree dir) must be blocked — the AICC-LAUNCH-001 registered-
    worktree guarantee, stronger than the git-common-dir equality check."""
    existing_path = tmp_path / "existing-worktree"
    _add_worktree(git_repo, path=existing_path, branch="existing-branch")

    # Copy the linked worktree to a *new* path git never registered, but whose
    # `.git` gitfile still points at the same common dir. `git worktree list`
    # does not know about this copy, so it must be rejected as unregistered.
    unregistered = tmp_path / "unregistered-copy"
    shutil.copytree(existing_path, unregistered)

    base_branch = _current_branch(git_repo)
    task = _write_card(
        tmp_path, base_branch=base_branch, repository=str(git_repo),
        branch="existing-branch", worktree=str(unregistered),
    )

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert not plan.launchable
    assert any("зарегистрированным worktree" in b for b in plan.blockers), plan.blockers


def test_launch_plan_exposes_launch_and_permission_profiles(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert plan.executor_id == "claude_code"
    assert plan.launch_profile_label == "Claude Code · Implementation"
    assert plan.permission_profile_key == worktree_launcher.PROFILE_IMPLEMENTATION
    assert plan.permission_profile_label == "Implementation"
    assert plan.permission_profile_summary


def test_read_only_card_type_maps_to_read_only_permission_profile(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    # Re-shape the parsed card's frontmatter to a read-only type without
    # rewriting the whole template — `type` drives `_map_task_type`.
    task.frontmatter["type"] = "review"
    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert plan.task_type == "review"
    assert plan.permission_profile_key == worktree_launcher.PROFILE_READ_ONLY
    assert plan.launch_profile_label == "Claude Code · Read-only"


def test_launch_cwd_is_the_selected_worktree_not_the_main_repository(
    git_repo, tmp_path, fake_claude, portfolio_worktrees_root
):
    """The launched run's working directory must be the resolved worktree,
    never the main repository — the central AICC-LAUNCH-001 guarantee (cwd ==
    selected worktree; never silently falls back to main)."""
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    result = portfolio_launch.launch_portfolio_task(
        tmp_path, task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )
    try:
        assert result.launched is True, result.message
        worktree_path = Path(result.plan.worktree)
        assert worktree_path != git_repo  # not the main repo

        run = api.get_run(result.run_id)
        assert run is not None
        assert Path(run["repository_path"]).resolve() == worktree_path.resolve()
    finally:
        if result.run_id:
            api.request_cancel(result.run_id, confirmed=True)


def test_existing_worktree_is_never_removed_during_rollback(git_repo, tmp_path, monkeypatch, portfolio_worktrees_root):
    existing_path = tmp_path / "existing-worktree"
    _add_worktree(git_repo, path=existing_path, branch="existing-branch")

    base_branch = _current_branch(git_repo)
    task = _write_card(
        tmp_path, base_branch=base_branch, repository=str(git_repo),
        branch="existing-branch", worktree=str(existing_path),
    )
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    def failing_launch_ready(root, entries, tasks, tasks_by_id, project_configs, execution_center_api, *, entry_ids=None):
        updated = [dict(e) for e in entries]
        results = [
            execution_queue.LaunchAttemptResult(entry_ids[0], task.task_id, False, message="forced failure for test")
        ]
        return updated, results

    monkeypatch.setattr(portfolio_launch.execution_queue, "launch_ready", failing_launch_ready)

    result = portfolio_launch.launch_portfolio_task(
        tmp_path, task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )

    assert result.launched is False
    # The pre-existing worktree and its branch must both survive untouched.
    assert existing_path.is_dir()
    assert "existing-branch" in git_info.get_branches(git_repo)
    assert any(wt.get("path") == str(existing_path.resolve()) for wt in git_info.get_worktrees(git_repo))


def test_generated_worktree_and_branch_are_removed_during_rollback_on_failure(
    git_repo, tmp_path, monkeypatch, portfolio_worktrees_root
):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo))
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    def failing_launch_ready(root, entries, tasks, tasks_by_id, project_configs, execution_center_api, *, entry_ids=None):
        updated = [dict(e) for e in entries]
        results = [
            execution_queue.LaunchAttemptResult(entry_ids[0], task.task_id, False, message="forced failure for test")
        ]
        return updated, results

    monkeypatch.setattr(portfolio_launch.execution_queue, "launch_ready", failing_launch_ready)

    result = portfolio_launch.launch_portfolio_task(
        tmp_path, task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )

    assert result.launched is False
    assert not Path(result.plan.worktree).exists()
    assert result.plan.branch not in git_info.get_branches(git_repo)


def test_attaching_existing_branch_to_new_worktree_does_not_delete_branch_on_rollback(
    git_repo, tmp_path, monkeypatch, portfolio_worktrees_root
):
    """A card explicitly names a branch that already exists (but has no
    worktree of its own yet) — this module attaches it to a brand-new
    worktree rather than treating the pre-existing branch as a conflict. On
    rollback, the newly created worktree is removed but the pre-existing
    branch itself must survive."""
    subprocess.run(["git", "branch", "pre-existing-branch"], cwd=git_repo, check=True)
    base_branch = _current_branch(git_repo)
    task = _write_card(
        tmp_path, base_branch=base_branch, repository=str(git_repo), branch="pre-existing-branch"
    )
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert plan.launchable, plan.blockers
    assert plan.worktree_mode == portfolio_launch.WORKTREE_MODE_NEW
    assert any("привязана" in w for w in plan.warnings)

    def failing_launch_ready(root, entries, tasks, tasks_by_id, project_configs, execution_center_api, *, entry_ids=None):
        updated = [dict(e) for e in entries]
        results = [
            execution_queue.LaunchAttemptResult(entry_ids[0], task.task_id, False, message="forced failure for test")
        ]
        return updated, results

    monkeypatch.setattr(portfolio_launch.execution_queue, "launch_ready", failing_launch_ready)

    result = portfolio_launch.launch_portfolio_task(
        tmp_path, task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )

    assert result.launched is False
    assert not Path(result.plan.worktree).exists()  # the new worktree was rolled back
    assert "pre-existing-branch" in git_info.get_branches(git_repo)  # the branch was not


def test_prompt_uses_resolved_branch_and_worktree(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    override_worktree = tmp_path / "explicit-worktree"
    task = _write_card(
        tmp_path, base_branch=base_branch, repository=str(git_repo),
        branch="feature/explicit", worktree=str(override_worktree),
    )
    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    prompt = portfolio_launch.build_agent_prompt(task, plan)

    assert "feature/explicit" in prompt
    assert str(override_worktree) in prompt


def test_prompt_identifies_existing_worktree_mode(git_repo, tmp_path, portfolio_worktrees_root):
    existing_path = tmp_path / "existing-worktree"
    _add_worktree(git_repo, path=existing_path, branch="existing-branch")
    base_branch = _current_branch(git_repo)
    task = _write_card(
        tmp_path, base_branch=base_branch, repository=str(git_repo),
        branch="existing-branch", worktree=str(existing_path),
    )
    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})
    assert plan.worktree_mode == portfolio_launch.WORKTREE_MODE_EXISTING

    prompt = portfolio_launch.build_agent_prompt(task, plan)

    assert "УЖЕ СУЩЕСТВОВАЛ" in prompt
    assert "НЕ переключай ветку" in prompt


def test_dry_run_exposes_resolution_source(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo), branch="feature/explicit")

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert plan.branch_source == portfolio_launch.SOURCE_CARD_OVERRIDE
    assert plan.worktree_source == portfolio_launch.SOURCE_GENERATED_DEFAULT


def test_duplicate_conflict_detected_via_resolved_branch(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    registry = {
        "AICC-OTHER": {"branch": "feature/shared", "worktree": str(tmp_path / "somewhere-else")}
    }
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo), branch="feature/shared")

    plan = portfolio_launch.build_launch_plan(
        task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)}, registry=registry
    )

    assert not plan.launchable
    assert any("AICC-OTHER" in b for b in plan.blockers)


def test_duplicate_conflict_detected_via_resolved_worktree(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    shared_worktree = tmp_path / "shared-worktree-path"
    registry = {
        "AICC-OTHER": {"branch": "task/aicc-other", "worktree": str(shared_worktree)}
    }
    task = _write_card(tmp_path, base_branch=base_branch, repository=str(git_repo), worktree=str(shared_worktree))

    plan = portfolio_launch.build_launch_plan(
        task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)}, registry=registry
    )

    assert not plan.launchable
    assert any("AICC-OTHER" in b for b in plan.blockers)


def test_batch_detects_two_cards_requesting_the_same_branch(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    task_a = _write_card(
        tmp_path, task_id="AICC-DUP-A", base_branch=base_branch, repository=str(git_repo), branch="feature/shared"
    )
    task_b = _write_card(
        tmp_path, task_id="AICC-DUP-B", base_branch=base_branch, repository=str(git_repo), branch="feature/shared"
    )

    plans = portfolio_launch.build_batch_plan(
        [task_a, task_b], tasks_by_id={}, repository_paths={"AICC": str(git_repo)}
    )
    conflicts = portfolio_launch._detect_batch_conflicts(plans)

    assert "AICC-DUP-A" in conflicts
    assert "AICC-DUP-B" in conflicts


def test_batch_detects_two_cards_requesting_the_same_worktree(git_repo, tmp_path, portfolio_worktrees_root):
    base_branch = _current_branch(git_repo)
    shared_worktree = tmp_path / "shared-worktree"
    task_a = _write_card(
        tmp_path, task_id="AICC-DUP-A", base_branch=base_branch, repository=str(git_repo),
        worktree=str(shared_worktree),
    )
    task_b = _write_card(
        tmp_path, task_id="AICC-DUP-B", base_branch=base_branch, repository=str(git_repo),
        worktree=str(shared_worktree),
    )

    plans = portfolio_launch.build_batch_plan(
        [task_a, task_b], tasks_by_id={}, repository_paths={"AICC": str(git_repo)}
    )
    conflicts = portfolio_launch._detect_batch_conflicts(plans)

    assert "AICC-DUP-A" in conflicts
    assert "AICC-DUP-B" in conflicts


def test_launch_batch_skips_tasks_with_conflicting_overrides(git_repo, tmp_path, fake_claude, portfolio_worktrees_root):
    fake_claude["FAKE_CLAUDE_EXTRA_SLEEP"] = "5"
    base_branch = _current_branch(git_repo)
    shared_worktree = tmp_path / "shared-worktree"
    task_a = _write_card(
        tmp_path, task_id="AICC-DUP-A", base_branch=base_branch, repository=str(git_repo),
        worktree=str(shared_worktree),
    )
    task_b = _write_card(
        tmp_path, task_id="AICC-DUP-B", base_branch=base_branch, repository=str(git_repo),
        worktree=str(shared_worktree),
    )
    api = runtime_api.ExecutionCenterAPI(db_path=tmp_path / "runtime.db")

    results = portfolio_launch.launch_batch(
        tmp_path, [task_a, task_b], tasks_by_id={}, repository_paths={"AICC": str(git_repo)},
        execution_center_api=api, confirmed=True,
    )

    assert all(not r.launched for r in results)
    assert not shared_worktree.exists()


def test_real_aicc_ui_001_card_plans_according_to_declared_intent(tmp_path, portfolio_worktrees_root, git_repo):
    """The real `AICC-UI-001` card (the case that motivated this
    remediation) declares `worktree: "~/Projects/ai-command-center"` — the
    same path as its own `repository` field — expressing "do this work in
    the primary worktree, do not create a new one." With override support,
    planning against that exact declared intent must resolve to the
    project's own repository path in `existing` mode, not a freshly
    generated `worktrees/aicc-ui-001` directory."""
    task = _write_card(
        tmp_path,
        task_id="AICC-UI-001",
        base_branch=_current_branch(git_repo),
        repository=str(git_repo),
        branch="fix/kanban-full-width-columns",
        worktree=str(git_repo),
    )

    plan = portfolio_launch.build_launch_plan(task, tasks_by_id={}, repository_paths={"AICC": str(git_repo)})

    assert plan.requested_worktree == str(git_repo)
    assert plan.worktree_source == portfolio_launch.SOURCE_CARD_OVERRIDE
    assert plan.worktree_mode == portfolio_launch.WORKTREE_MODE_EXISTING
    assert Path(plan.worktree) == git_repo.resolve()
    # Blocked here only because the primary worktree is still on its default
    # branch, not `fix/kanban-full-width-columns` — exactly the "existing
    # worktree on the wrong branch" guard doing its job for this real card;
    # an operator would create that branch (or check it out) before AICC
    # Command Center could safely launch against the primary worktree.
    assert not plan.launchable
    assert any("на ветке" in b or "сменил ветку" in b for b in plan.blockers)


# --------------------------------------------------------------------------
# Card presentation — single, unambiguous launch-state (duplicate-status fix)
# --------------------------------------------------------------------------
#
# `build_card_presentation` is pure: it never touches the filesystem, the
# registry file, or the execution database — every `LaunchPlan` below is
# hand-built with only the fields these tests actually exercise, and
# `existing_run` is just a plain dict standing in for whatever
# `ExecutionCenterAPI.get_run` would have returned.


def _plan(*, blockers: list[str] | None = None, launchable_lane: str = "ready") -> portfolio_launch.LaunchPlan:
    return portfolio_launch.LaunchPlan(
        task_id="AICC-CARD-001",
        project="AICC",
        title="Card presentation test",
        source_path="tasks/ready/AICC/AICC-CARD-001.md",
        lane=launchable_lane,
        repository_root="/tmp/aicc",
        base_branch="main",
        base_sha="deadbeef",
        branch="task/aicc-card-001",
        worktree="/tmp/worktrees/aicc-card-001",
        task_type="general",
        blockers=list(blockers or []),
    )


def test_build_card_presentation_ready_when_no_registry_entry():
    presentation = portfolio_launch.build_card_presentation(_plan(), registry_entry=None, existing_run=None)

    assert presentation.status_key == portfolio_launch.STATUS_READY
    assert presentation.status_label == "Ready"
    assert presentation.launch_allowed is True
    assert presentation.existing_run_id is None
    assert presentation.message is None


@pytest.mark.parametrize("run_state", ["PREPARED", "QUEUED", "RUNNING"])
def test_build_card_presentation_running_for_any_active_run_state(run_state):
    plan = _plan(blockers=[portfolio_launch.ALREADY_LAUNCHED_BLOCKER])
    registry_entry = {"run_id": "run-1", "launched_at": "2026-01-01T00:00:00"}

    presentation = portfolio_launch.build_card_presentation(
        plan, registry_entry=registry_entry, existing_run={"state": run_state, "started_at": "2026-01-01T00:00:05"}
    )

    assert presentation.status_key == portfolio_launch.STATUS_RUNNING
    assert presentation.status_label == "Running"
    # The exact bug this fix targets: a headline status must never look like
    # both `ready` (launchable) and `Blocked` (an error) at once.
    assert presentation.status_key not in (portfolio_launch.STATUS_READY, portfolio_launch.STATUS_BLOCKED)
    assert presentation.launch_allowed is False
    assert presentation.existing_run_id == "run-1"
    assert presentation.message_severity != "error"


def test_build_card_presentation_completed_is_not_shown_as_blocked():
    plan = _plan(blockers=[portfolio_launch.ALREADY_LAUNCHED_BLOCKER])
    registry_entry = {"run_id": "run-2", "launched_at": "2026-01-01T00:00:00"}

    presentation = portfolio_launch.build_card_presentation(
        plan, registry_entry=registry_entry, existing_run={"state": "COMPLETED", "completed_at": "2026-01-01T01:00:00"}
    )

    assert presentation.status_key == portfolio_launch.STATUS_COMPLETED
    assert presentation.status_key != portfolio_launch.STATUS_BLOCKED
    assert presentation.launch_allowed is False
    assert presentation.message_severity != "error"


@pytest.mark.parametrize("run_state", ["FAILED", "CANCELLED", "INTERRUPTED"])
def test_build_card_presentation_failed_and_cancelled_show_real_terminal_status(run_state):
    plan = _plan(blockers=[portfolio_launch.ALREADY_LAUNCHED_BLOCKER])
    registry_entry = {"run_id": "run-3", "launched_at": "2026-01-01T00:00:00"}

    presentation = portfolio_launch.build_card_presentation(
        plan, registry_entry=registry_entry, existing_run={"state": run_state}
    )

    assert presentation.status_key != portfolio_launch.STATUS_BLOCKED
    assert presentation.status_key in (portfolio_launch.STATUS_FAILED, portfolio_launch.STATUS_CANCELLED)
    assert presentation.launch_allowed is False
    # Duplicate prevention must hold even after a terminal failure/cancel —
    # no formalized retry model exists in this codebase (task description §6).
    assert presentation.message_severity != "error"


def test_build_card_presentation_falls_back_to_already_launched_when_run_state_unknown():
    plan = _plan(blockers=[portfolio_launch.ALREADY_LAUNCHED_BLOCKER])
    registry_entry = {"run_id": "run-4", "launched_at": "2026-01-01T00:00:00"}

    # `existing_run=None` covers both "run row not found" and "no
    # execution_center_api available to look it up" — the caller's job, not
    # this function's, to tell those apart.
    presentation = portfolio_launch.build_card_presentation(plan, registry_entry=registry_entry, existing_run=None)

    assert presentation.status_key == portfolio_launch.STATUS_ALREADY_LAUNCHED
    assert presentation.launch_allowed is False
    assert presentation.existing_run_id == "run-4"
    assert presentation.message_severity != "error"

    # An unrecognized `state` string (e.g. a future run state this code
    # doesn't know about yet) is exactly as safe as no run at all.
    unknown_state_presentation = portfolio_launch.build_card_presentation(
        plan, registry_entry=registry_entry, existing_run={"state": "SOME_FUTURE_STATE"}
    )
    assert unknown_state_presentation.status_key == portfolio_launch.STATUS_ALREADY_LAUNCHED


def test_build_card_presentation_real_precondition_blocker_is_not_masked_by_prior_run():
    """A genuine precondition failure (unmapped repository, bad worktree,
    ...) must still read as `Blocked` with its real reason — even when the
    task also happens to already have a registered run — never silently
    replaced by an "already launched" status (task description §6)."""
    plan = _plan(
        blockers=[
            "репозиторий для проекта «AICC» не сопоставлен — настройте его в конфигурации",
            portfolio_launch.ALREADY_LAUNCHED_BLOCKER,
        ]
    )
    registry_entry = {"run_id": "run-5", "launched_at": "2026-01-01T00:00:00"}

    presentation = portfolio_launch.build_card_presentation(
        plan, registry_entry=registry_entry, existing_run={"state": "RUNNING"}
    )

    assert presentation.status_key == portfolio_launch.STATUS_BLOCKED
    assert presentation.launch_allowed is False
    assert "не сопоставлен" in presentation.message
    # The duplicate-launch blocker text itself must not leak into the
    # user-facing Blocked reason — it has its own, separate presentation.
    assert portfolio_launch.ALREADY_LAUNCHED_BLOCKER not in presentation.message


def test_build_card_presentation_real_precondition_blocker_without_any_prior_run():
    plan = _plan(blockers=["задача находится в статусе «blocked», автозапуск разрешён только для «ready»"])

    presentation = portfolio_launch.build_card_presentation(plan, registry_entry=None, existing_run=None)

    assert presentation.status_key == portfolio_launch.STATUS_BLOCKED
    assert presentation.launch_allowed is False
    assert presentation.existing_run_id is None
    assert "автозапуск разрешён только" in presentation.message


def test_build_card_presentation_never_uses_error_message_severity_for_an_existing_run():
    """Task description §4: the alert for an existing run must be
    info/warning, never a red error box — even when the run itself failed
    (the badge can say "Failed" in red; the message stays advisory)."""
    registry_entry = {"run_id": "run-6", "launched_at": "2026-01-01T00:00:00"}
    plan = _plan(blockers=[portfolio_launch.ALREADY_LAUNCHED_BLOCKER])

    for run_state in ("PREPARED", "QUEUED", "RUNNING", "COMPLETED", "FAILED", "CANCELLED", "INTERRUPTED", None):
        existing_run = {"state": run_state} if run_state else None
        presentation = portfolio_launch.build_card_presentation(
            plan, registry_entry=registry_entry, existing_run=existing_run
        )
        assert presentation.message_severity in ("info", "warning")
