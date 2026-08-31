"""Isolated workspace per mutating dispatch (VOYN-W0-AICC-ISOLATED-WORKTREE-
PER-ATTEMPT, a P0 audit finding: every mutating run for a project shared ONE
checkout with no isolation at all).

`test_handlers.py` fakes `workspace_provisioning.provision_and_verify` at the
same seam as `run_claude_code`, so its ~30 outcome-discipline tests do not
need a real repository on disk. This file is the complement: real git repos,
real `git worktree add` / `git worktree remove`, exercising `_run_agent`'s
isolation wiring end to end. `run_claude_code` is still faked (no real
`claude` CLI) -- but the fake actually commits into whatever `repository_path`
it is handed, so these tests prove the worktree the agent runs in is the one
that gets published and cleaned up, not merely that the right kwargs were
recorded.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import pytest

from command_center import agent_runner
from command_center.worker.handlers import build_handlers


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
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


def _payload(**overrides):
    payload = {
        "v": 1,
        "project_id": "proj",
        "repository_path": "/configured/elsewhere",  # validate_repository is faked
        "prompt": "do the thing",
        "task_type": "implementation",
        "timeout_seconds": 120,
        "untrusted": False,
        "backlog_task_id": "VOYN-TASK-A",
    }
    payload.update(overrides)
    return payload


def _event() -> threading.Event:
    return threading.Event()


def _fake_run(*, commit: bool = True, status: str = "completed", exit_code: int | None = 0):
    """A `run_claude_code` replacement that -- unlike test_handlers.py's --
    actually writes and commits into the `repository_path` it is given, so a
    test can observe which physical directory received the work."""

    def run(*, repository_path, prompt, task_type, timeout_seconds, model=None, cancel_event=None):
        if commit:
            target = Path(repository_path)
            (target / "change.txt").write_text(f"work: {prompt}\n")
            _git(target, "add", "change.txt")
            _git(target, "commit", "-q", "-m", "agent work")
        return agent_runner.RunResult(
            status=status,
            exit_code=exit_code,
            stdout='{"result": "done"}',
            stderr="",
            duration_seconds=1.0,
            started_at="2026-08-21T12:00:00+00:00",
            completed_at="2026-08-21T12:00:01+00:00",
        )

    return run


def _never_started_run(**kwargs):
    return agent_runner.RunResult(
        status="failed",
        exit_code=None,
        stdout="",
        stderr="claude binary vanished mid-launch",
        duration_seconds=0.0,
        started_at="2026-08-21T12:00:00+00:00",
        completed_at="2026-08-21T12:00:00+00:00",
    )


@pytest.fixture
def agent(monkeypatch, tmp_path):
    """A real repo at `tmp_path/repo`, wired as the project's configured
    repository. No lease authority and no publish (AICC_PUBLISH_DEPLOY_KEY
    unset) unless a test opts in -- so the default here is exactly the
    "local commit only" mode the existing BO-S3b comment describes."""
    repo = _make_repo(tmp_path / "repo")
    monkeypatch.setattr(
        agent_runner, "validate_repository", lambda project_id, path: repo
    )
    monkeypatch.setattr(agent_runner, "claude_cli_preflight", lambda: (True, "ok"))
    monkeypatch.delenv("VOYN_LEASE_DSN", raising=False)
    monkeypatch.delenv("AICC_PUBLISH_DEPLOY_KEY", raising=False)
    return build_handlers()["agent_run"], repo


@pytest.fixture
def agent_with_publish(agent, monkeypatch, tmp_path):
    """Extends `agent` with a real bare `origin` remote plus fake `gh` and
    `voyn-lease` binaries on PATH -- the same shape as
    `tests/orchestrator/test_publish.py`'s `repo` fixture, reused rather than
    re-invented, so publish_run's actual git/lease/gh mechanics run for real
    against the isolated worktree instead of being stubbed at a higher
    level."""
    run_agent, repo = agent
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-q", "-b", "main", str(bare)],
        check=True, capture_output=True,
    )
    _git(repo, "remote", "add", "origin", str(bare))
    _git(repo, "push", "-q", "origin", "main")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    lease = bin_dir / "voyn-lease"
    lease.write_text("#!/bin/sh\nexit 0\n")
    lease.chmod(0o755)
    gh = bin_dir / "gh"
    gh.write_text(
        "#!/bin/sh\n"
        "case \"$2\" in\n"
        "  view) exit 1 ;;\n"  # no existing PR
        "  create) echo 'https://github.com/o/r/pull/1'; exit 0 ;;\n"
        "esac\n"
    )
    gh.chmod(0o755)

    import os

    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("AICC_PUBLISH_DEPLOY_KEY", "/dev/null")
    monkeypatch.setenv("VOYN_LEASE_TOOL", str(lease))
    return run_agent, repo


# --------------------------------------------------------------------------
# Isolation: which physical directory a run executes in
# --------------------------------------------------------------------------


def test_read_only_task_uses_the_shared_checkout_unchanged(agent, monkeypatch):
    """No isolation requirement for read-only work (per the task brief): it
    gets the read-only sandbox profile and cannot write, so provisioning a
    worktree for it is pure churn. Regression guard against the common case
    silently changing behaviour."""
    run_agent, repo = agent
    captured = {}
    monkeypatch.setattr(
        agent_runner,
        "run_claude_code",
        lambda **kw: (captured.update(repository_path=kw["repository_path"]) or _fake_run(commit=False)(**kw)),
    )
    outcome = run_agent(_payload(task_type="review"), _event(), 1)
    assert outcome.ok
    assert Path(captured["repository_path"]) == repo
    assert not (repo.parent / f"{repo.name}-worktrees").exists()


def test_mutating_task_gets_an_isolated_worktree_distinct_from_shared_checkout(
    agent, monkeypatch
):
    run_agent, repo = agent
    captured = {}

    def run(**kwargs):
        captured["repository_path"] = kwargs["repository_path"]
        return _fake_run()(**kwargs)

    monkeypatch.setattr(agent_runner, "run_claude_code", run)
    outcome = run_agent(_payload(), _event(), 1)
    assert outcome.ok
    used = Path(captured["repository_path"])
    assert used != repo
    assert used.parent == repo.parent / f"{repo.name}-worktrees"
    # And the primary checkout is untouched by the agent's commit.
    assert not (repo / "change.txt").exists()
    assert (used / "change.txt").exists()


def test_two_different_tasks_get_distinct_workspace_paths(agent, monkeypatch):
    """The concurrency property the audit finding is actually about: two
    DIFFERENT tasks/branches for the same project must never land in the
    same checkout. (Two attempts of the SAME task intentionally share one
    worktree -- see test_retries_of_the_same_task_share_one_worktree below
    for why, and handlers.py's naming-decision comment for the full
    reasoning: the payload carries no attempt-scoped identifier, and git
    permits only one worktree per branch, which `publish_run` always
    computes as `backlog/<task>` regardless of which attempt produced the
    commit.)"""
    run_agent, repo = agent
    seen: list[str] = []
    monkeypatch.setattr(
        agent_runner,
        "run_claude_code",
        lambda **kw: (seen.append(kw["repository_path"]) or _fake_run(commit=False)(**kw)),
    )

    assert run_agent(_payload(backlog_task_id="VOYN-TASK-A"), _event(), 1).ok
    assert run_agent(_payload(backlog_task_id="VOYN-TASK-B"), _event(), 1).ok

    assert len(seen) == 2
    assert Path(seen[0]) != Path(seen[1])


def test_retries_of_the_same_task_share_one_worktree(agent, monkeypatch):
    """Deliberate, documented behaviour: a redelivered attempt of the SAME
    task computes the SAME workspace path, because it is building toward the
    same branch and the same eventual PR."""
    run_agent, repo = agent
    seen: list[str] = []
    monkeypatch.setattr(
        agent_runner,
        "run_claude_code",
        lambda **kw: (seen.append(kw["repository_path"]) or _fake_run(commit=False)(**kw)),
    )

    assert run_agent(_payload(backlog_task_id="VOYN-TASK-A"), _event(), 1).ok
    assert run_agent(_payload(backlog_task_id="VOYN-TASK-A"), _event(), 2).ok

    assert Path(seen[0]) == Path(seen[1])


def test_project_id_backfills_the_branch_when_no_backlog_task_id(agent, monkeypatch):
    """Payload compatibility (VOYN-W0-AICC-PUBLISH-BRANCH-COLLISION's own
    fallback): a payload with no `backlog_task_id` still isolates -- keyed by
    `project_id`, matching what `publish_run` would branch to."""
    run_agent, repo = agent
    captured = {}
    monkeypatch.setattr(
        agent_runner,
        "run_claude_code",
        lambda **kw: (captured.update(repository_path=kw["repository_path"]) or _fake_run(commit=False)(**kw)),
    )
    payload = _payload()
    del payload["backlog_task_id"]
    outcome = run_agent(payload, _event(), 1)
    assert outcome.ok
    assert Path(captured["repository_path"]).name == f"backlog-{payload['project_id']}"


# --------------------------------------------------------------------------
# Publish receives the isolated path
# --------------------------------------------------------------------------


def test_publish_pushes_from_the_isolated_workspace(agent_with_publish, monkeypatch):
    run_agent, repo = agent_with_publish
    monkeypatch.setattr(agent_runner, "run_claude_code", _fake_run())

    outcome = run_agent(_payload(), _event(), 1)

    assert outcome.ok
    assert outcome.result["publish"]["ok"] is True
    assert outcome.result["publish"]["branch"] == "backlog/VOYN-TASK-A"
    assert outcome.result["pr_url"] == "https://github.com/o/r/pull/1"
    # The pushed branch carries the agent's commit -- proof publish_run ran
    # against the isolated worktree's HEAD, not the primary checkout's.
    show = _git(repo, "show", "origin/backlog/VOYN-TASK-A:change.txt")
    assert "work:" in show.stdout


# --------------------------------------------------------------------------
# Cleanup
# --------------------------------------------------------------------------


def test_cleanup_after_a_never_started_run(agent, monkeypatch):
    """Nothing executed -- the worktree holds no run output, so it is
    removed immediately rather than left behind for every failed launch."""
    run_agent, repo = agent
    monkeypatch.setattr(agent_runner, "run_claude_code", _never_started_run)

    outcome = run_agent(_payload(), _event(), 1)

    assert not outcome.ok and outcome.retryable
    worktrees_dir = repo.parent / f"{repo.name}-worktrees"
    assert not (worktrees_dir / "backlog-VOYN-TASK-A").exists()


def test_cleanup_after_publish_succeeds(agent_with_publish, monkeypatch):
    run_agent, repo = agent_with_publish
    monkeypatch.setattr(agent_runner, "run_claude_code", _fake_run())

    outcome = run_agent(_payload(), _event(), 1)

    assert outcome.ok and outcome.result["publish"]["ok"] is True
    worktrees_dir = repo.parent / f"{repo.name}-worktrees"
    assert not (worktrees_dir / "backlog-VOYN-TASK-A").exists()


def test_cleanup_after_publish_reports_nothing_to_publish(agent_with_publish, monkeypatch):
    """A run that made no commit still counts as a successful publish outcome
    (`nothing_to_publish`) -- there is nothing local to lose, so the
    worktree is disposable exactly as in the pushed case. This is this
    change's "failure path" cleanup case: the agent's run itself reports
    failed, but nothing was ever written, so cleanup is safe."""
    run_agent, repo = agent_with_publish
    monkeypatch.setattr(
        agent_runner, "run_claude_code", _fake_run(commit=False, status="failed", exit_code=1)
    )

    outcome = run_agent(_payload(), _event(), 1)

    assert outcome.ok and outcome.result["status"] == "failed"
    assert outcome.result["publish"]["reason"] == "nothing_to_publish"
    worktrees_dir = repo.parent / f"{repo.name}-worktrees"
    assert not (worktrees_dir / "backlog-VOYN-TASK-A").exists()


def test_no_cleanup_when_publish_is_not_configured(agent, monkeypatch):
    """Data-loss guard: with no AICC_PUBLISH_DEPLOY_KEY, the worktree's local
    commit is the ONLY record of the run's work -- exactly as the shared
    checkout's commits were before this change. Cleanup must never discard
    it."""
    run_agent, repo = agent
    monkeypatch.setattr(agent_runner, "run_claude_code", _fake_run())

    outcome = run_agent(_payload(), _event(), 1)

    assert outcome.ok
    worktrees_dir = repo.parent / f"{repo.name}-worktrees"
    workspace = worktrees_dir / "backlog-VOYN-TASK-A"
    assert workspace.is_dir()
    assert (workspace / "change.txt").exists()


def test_no_cleanup_when_publish_fails(agent_with_publish, monkeypatch):
    """A push/PR failure leaves the worktree in place: it may be the only
    remaining copy of the agent's commit, and deleting it on a transient
    publish failure would be unrecoverable data loss for the sake of
    tidiness."""
    run_agent, repo = agent_with_publish
    monkeypatch.setattr(agent_runner, "run_claude_code", _fake_run())

    # Break the fake gh so `pr create` fails after a successful push.
    import os

    bin_dir = None
    for entry in os.environ["PATH"].split(os.pathsep):
        if (Path(entry) / "gh").exists():
            bin_dir = Path(entry)
            break
    assert bin_dir is not None
    (bin_dir / "gh").write_text("#!/bin/sh\nexit 1\n")

    outcome = run_agent(_payload(), _event(), 1)

    assert outcome.ok  # the handler outcome is still ok=True (BO-S3b: publish failure is data)
    assert outcome.result["publish"]["ok"] is False
    worktrees_dir = repo.parent / f"{repo.name}-worktrees"
    workspace = worktrees_dir / "backlog-VOYN-TASK-A"
    assert workspace.is_dir()
    assert (workspace / "change.txt").exists()


def test_provision_lock_serializes_concurrent_same_path_provisioning(agent, monkeypatch):
    """`provision_workspace` is check-then-act; without the per-path lock two
    threads racing to provision the SAME new path could both pass
    `workspace.exists()` before either creates it. Not a realistic shape for
    the single-threaded daemon loop (see the lock's own docstring), but the
    lock must actually serialize when exercised directly."""
    run_agent, repo = agent
    monkeypatch.setattr(agent_runner, "run_claude_code", _fake_run(commit=False))
    results: list[bool] = []
    errors: list[BaseException] = []

    def worker(attempt_no: int) -> None:
        try:
            outcome = run_agent(_payload(backlog_task_id="VOYN-TASK-RACE"), _event(), attempt_no)
            results.append(outcome.ok)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in (1, 2, 3, 4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert all(results), results
