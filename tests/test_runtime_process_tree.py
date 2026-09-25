"""F4: process-group cancellation must terminate an entire process tree —
parent -> child -> grandchild — not just the direct child the Supervisor
`Popen`'d, and must never leave any level orphaned.
"""

from __future__ import annotations

import os
import time

import pytest

from command_center.runtime import db, identity, supervisor


def _wait_for_tree_pids(pidfile_base: str, timeout: float = 10.0) -> dict[str, int]:
    deadline = time.monotonic() + timeout
    paths = {role: f"{pidfile_base}.{role}" for role in ("parent", "child", "grandchild")}

    while time.monotonic() < deadline:
        if all(os.path.exists(p) for p in paths.values()):
            pids = {}
            for role, path in paths.items():
                with open(path) as handle:
                    pids[role] = int(handle.read().strip())
            return pids
        time.sleep(0.05)
    raise TimeoutError(f"process tree pid files never all appeared: {pidfile_base}")


def test_grandchild_process_tree_terminates_on_process_group_cancellation(
    git_repo, configure_project_repo, fake_claude_tree
):
    _env_overrides, pidfile_base = fake_claude_tree
    configure_project_repo("AIOS", git_repo)
    sup = supervisor.Supervisor()

    run = sup.start_raw(
        project="AIOS", repository_path=str(git_repo), task_type="implementation", prompt="p", confirmed=True
    )

    pids = _wait_for_tree_pids(pidfile_base)
    assert pids["parent"] == run["pid"], "the Supervisor-tracked pid must be the top-level process"
    assert identity.process_exists(pids["parent"])
    assert identity.process_exists(pids["child"])
    assert identity.process_exists(pids["grandchild"])

    result = sup.cancel(run["id"], confirmed=True, grace_seconds=5)

    assert result["state"] == "CANCELLED"
    assert identity.process_exists(pids["parent"]) is False, "parent must not survive cancellation"
    assert identity.process_exists(pids["child"]) is False, "child must not survive cancellation"
    assert identity.process_exists(pids["grandchild"]) is False, "grandchild must not survive cancellation (no orphan)"

    # Output received before cancellation (the two stream-json lines the
    # parent printed before settling into its long sleep) must be retained.
    events = db.list_run_events(sup.db_path, run["id"])
    result_events = [e for e in events if e["event_type"] == "result"]
    assert result_events and result_events[0]["payload"]["result"] == "tree started"


def test_grandchild_that_calls_setsid_does_not_survive_process_group_cancellation(
    git_repo, configure_project_repo, fake_claude_tree
):
    """A descendant that runs `setsid()` leaves the launch process group and
    becomes the leader of its own session — the classic technique for
    escaping a supervisor that only ever signals the pgid it started. It
    stays a live child of `child` throughout (its `ppid` is untouched), so
    cancellation must still reach and terminate it via that surviving link,
    not leave it running as an orphan."""
    env_overrides, pidfile_base = fake_claude_tree
    env_overrides["FAKE_CLAUDE_TREE_GRANDCHILD_SETSID"] = "1"
    configure_project_repo("AIOS", git_repo)
    sup = supervisor.Supervisor()

    run = sup.start_raw(
        project="AIOS", repository_path=str(git_repo), task_type="implementation", prompt="p", confirmed=True
    )

    pids = _wait_for_tree_pids(pidfile_base)
    assert identity.process_exists(pids["grandchild"])
    assert os.getpgid(pids["grandchild"]) != os.getpgid(pids["parent"]), (
        "the fixture must actually have escaped the launch process group, or this test proves nothing"
    )

    result = sup.cancel(run["id"], confirmed=True, grace_seconds=2)

    assert result["state"] == "CANCELLED"
    assert identity.process_exists(pids["parent"]) is False, "parent must not survive cancellation"
    assert identity.process_exists(pids["child"]) is False, "child must not survive cancellation"
    assert identity.process_exists(pids["grandchild"]) is False, (
        "the setsid-escaped grandchild must not survive cancellation as an orphan"
    )


def test_grandchild_process_tree_terminates_even_when_parent_ignores_sigterm(
    git_repo, configure_project_repo, fake_claude_tree
):
    """Even when the direct child (the process the Supervisor itself
    launched) ignores SIGTERM, forcing escalation to SIGKILL, every level of
    the tree must still be gone afterward — proving `killpg` reaches the
    whole process group, not just whichever process responds to SIGTERM."""
    env_overrides, pidfile_base = fake_claude_tree
    env_overrides["FAKE_CLAUDE_TREE_IGNORE_SIGTERM"] = "1"
    configure_project_repo("AIOS", git_repo)
    sup = supervisor.Supervisor()

    run = sup.start_raw(
        project="AIOS", repository_path=str(git_repo), task_type="implementation", prompt="p", confirmed=True
    )
    pids = _wait_for_tree_pids(pidfile_base)

    started = time.monotonic()
    result = sup.cancel(run["id"], confirmed=True, grace_seconds=1)
    elapsed = time.monotonic() - started

    assert result["state"] == "CANCELLED"
    assert elapsed >= 1, "SIGKILL must not fire before the grace period elapses"

    events = db.list_run_events(sup.db_path, run["id"])
    lifecycles = [e["payload"].get("lifecycle") for e in events if e["event_type"] == "lifecycle"]
    assert "cancel_sigkill_sent" in lifecycles

    for role in ("parent", "child", "grandchild"):
        assert identity.process_exists(pids[role]) is False, f"{role} must not survive a forced (SIGKILL) cancellation"


def test_natural_leader_exit_drains_sigterm_ignoring_descendants_before_release(
    git_repo, configure_project_repo, fake_claude_tree
):
    env_overrides, pidfile_base = fake_claude_tree
    env_overrides["FAKE_CLAUDE_TREE_DESCENDANTS_IGNORE_SIGTERM"] = "1"
    env_overrides["FAKE_CLAUDE_TREE_PARENT_EXIT_AFTER_START"] = "1"
    configure_project_repo("AIOS", git_repo)
    sup = supervisor.Supervisor()

    run = sup.start_raw(
        project="AIOS",
        repository_path=str(git_repo),
        task_type="implementation",
        prompt="p",
        confirmed=True,
    )
    pids = _wait_for_tree_pids(pidfile_base)
    final = sup.wait_for_run(run["id"], timeout=10)

    assert final["state"] in {"COMPLETED", "FAILED"}
    assert run["id"] not in sup.active_run_ids()
    for role, pid in pids.items():
        assert identity.process_exists(pid) is False, f"{role} survived natural leader exit cleanup"

    events = db.list_run_events(sup.db_path, run["id"])
    lifecycles = [e["payload"].get("lifecycle") for e in events if e["event_type"] == "lifecycle"]
    assert "process_group_cleanup_residual_sigterm_sent" in lifecycles
    assert "process_group_cleanup_sigkill_sent" in lifecycles


def test_launch_setup_failure_reaps_entire_spawned_process_group(
    git_repo, configure_project_repo, fake_claude_tree, monkeypatch
):
    _env_overrides, pidfile_base = fake_claude_tree
    configure_project_repo("AIOS", git_repo)

    def fail_after_tree_is_running(*, target, args, name):
        assert name.startswith("run-supervisor-")
        _wait_for_tree_pids(pidfile_base)
        raise RuntimeError("injected supervisor thread start failure")

    monkeypatch.setattr(
        supervisor.Supervisor,
        "_start_daemon_thread",
        staticmethod(fail_after_tree_is_running),
    )

    sup = supervisor.Supervisor()
    with pytest.raises(RuntimeError, match="thread start failure"):
        sup.start_raw(
            project="AIOS",
            repository_path=str(git_repo),
            task_type="implementation",
            prompt="p",
            confirmed=True,
        )

    pids = _wait_for_tree_pids(pidfile_base)
    for role, pid in pids.items():
        assert identity.process_exists(pid) is False, f"{role} survived failed launch setup"
    persisted = db.list_runs(sup.db_path)[0]
    assert persisted["state"] == "FAILED"
    assert persisted["failure_reason"] == "launch_setup_failed"
    assert sup.active_run_ids() == []
