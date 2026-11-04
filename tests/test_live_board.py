"""The Live Execution Center's board model: bucketing, the launch gate, and
the project dependency tree.

Pure-function tests — `command_center.ui.live_board` imports no Streamlit and
touches no database, which is the whole reason the board's behaviour can be
pinned down here instead of through an `AppTest` harness.
"""

from __future__ import annotations

import pytest

from command_center.runtime import session_view
from command_center.ui import live_board


def _task(task_id: str, **fields) -> dict:
    return {"id": task_id, "title": f"Задача {task_id}", "status": "Backlog", **fields}


def _by_id(*tasks: dict) -> dict[str, dict]:
    return {t["id"]: t for t in tasks}


# --------------------------------------------------------------------------
# Bucketing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (session_view.STATUS_RUNNING, live_board.BUCKET_LIVE),
        (session_view.STATUS_STARTING, live_board.BUCKET_LIVE),
        (session_view.STATUS_STALE, live_board.BUCKET_LIVE),
        (session_view.STATUS_WAITING, live_board.BUCKET_WAITING),
        (session_view.STATUS_LAUNCHING, live_board.BUCKET_WAITING),
        (session_view.STATUS_FAILED, live_board.BUCKET_ATTENTION),
        (session_view.STATUS_BLOCKED, live_board.BUCKET_ATTENTION),
        (session_view.STATUS_INCOMPLETE, live_board.BUCKET_ATTENTION),
        (session_view.STATUS_REQUIRES_ATTENTION, live_board.BUCKET_ATTENTION),
        (session_view.STATUS_COMPLETED, live_board.BUCKET_DONE),
        (session_view.STATUS_CANCELLED, live_board.BUCKET_DONE),
    ],
)
def test_every_display_status_lands_in_its_bucket(status, expected):
    assert live_board.bucket_for_status(status) == expected


def test_unknown_status_surfaces_as_attention_rather_than_vanishing():
    """A status this module has not been taught about must be visible, not
    silently dropped from the board."""
    assert live_board.bucket_for_status("Совершенно новый статус") == live_board.BUCKET_ATTENTION


def test_split_board_always_has_every_bucket_and_sorts_newest_first():
    sessions = [
        {"status": session_view.STATUS_RUNNING, "started_at": "2026-07-24T10:00:00"},
        {"status": session_view.STATUS_RUNNING, "started_at": "2026-07-24T12:00:00"},
    ]
    board = live_board.split_board(sessions)

    assert set(board) == set(live_board.BUCKET_ORDER)
    assert board[live_board.BUCKET_WAITING] == []
    assert [s["started_at"] for s in board[live_board.BUCKET_LIVE]] == [
        "2026-07-24T12:00:00",
        "2026-07-24T10:00:00",
    ]


def test_split_board_reads_the_display_status_key_it_is_given():
    """`app.py` guards the display status (never `Completed` below 100 %) and
    carries the result on the session; the board must bucket by that, not by
    the raw one."""
    sessions = [{"status": session_view.STATUS_COMPLETED, "display_status": session_view.STATUS_REQUIRES_ATTENTION}]
    board = live_board.split_board(sessions, display_status="display_status")

    assert board[live_board.BUCKET_ATTENTION] == sessions
    assert board[live_board.BUCKET_DONE] == []


# --------------------------------------------------------------------------
# Launch gate
# --------------------------------------------------------------------------


def test_ready_task_may_launch(tmp_path):
    task = _task("T1", workspace_path=str(tmp_path))
    gate = live_board.launch_gate(task, tasks_by_id=_by_id(task), active_runs=[])

    assert gate.allowed
    assert gate.code == live_board.GATE_OK


def test_done_task_is_not_launchable():
    task = _task("T1", status="Done")
    gate = live_board.launch_gate(task, tasks_by_id=_by_id(task), active_runs=[])

    assert not gate.allowed
    assert gate.code == live_board.GATE_ALREADY_DONE


def test_master_projection_task_is_not_launchable(tmp_path):
    """A master-projection record (the read-only backlog fallback view) must
    never gate as launchable, even when every other condition (workspace,
    dependencies, no active run) would otherwise allow it — `save_tasks`
    drops such records structurally on write, so the gate must refuse them
    before the write path ever sees the launch (VOYN-W0-AICC-READONLY-
    LAUNCH-SURFACES)."""
    task = _task("T1", workspace_path=str(tmp_path), source="master", read_only=True)
    gate = live_board.launch_gate(task, tasks_by_id=_by_id(task), active_runs=[])

    assert not gate.allowed
    assert gate.code == live_board.GATE_READ_ONLY


def test_unmet_dependency_blocks_launch_and_names_the_dependency(tmp_path):
    dep = _task("DEP", title="Сначала это", status="Backlog")
    task = _task("T1", depends_on=["DEP"], workspace_path=str(tmp_path))
    gate = live_board.launch_gate(task, tasks_by_id=_by_id(dep, task), active_runs=[])

    assert not gate.allowed
    assert gate.code == live_board.GATE_WAITING_DEPENDENCY
    assert "Сначала это" in gate.reason
    assert gate.unmet == ("DEP",)


def test_dependency_satisfied_by_done_unblocks_launch(tmp_path):
    dep = _task("DEP", status="Done")
    task = _task("T1", depends_on=["DEP"], workspace_path=str(tmp_path))
    gate = live_board.launch_gate(task, tasks_by_id=_by_id(dep, task), active_runs=[])

    assert gate.allowed


def test_task_with_an_active_attempt_is_blocked_as_duplicate(tmp_path):
    task = _task("T1", workspace_path=str(tmp_path))
    active = [{"id": "run-1", "task_id": "T1", "repository_path": str(tmp_path)}]
    gate = live_board.launch_gate(task, tasks_by_id=_by_id(task), active_runs=active)

    assert not gate.allowed
    assert gate.code == live_board.GATE_DUPLICATE
    assert gate.blocking_run_id == "run-1"
    assert gate.is_conflict


def test_workspace_held_by_another_task_is_blocked_as_busy(tmp_path):
    """Two agents must never be launched against one working tree — the
    condition the autopilot planner reports as `workspace_busy`."""
    task = _task("T1", workspace_path=str(tmp_path))
    active = [{"id": "run-9", "task_id": "OTHER", "repository_path": str(tmp_path)}]
    gate = live_board.launch_gate(task, tasks_by_id=_by_id(task), active_runs=active)

    assert not gate.allowed
    assert gate.code == live_board.GATE_WORKSPACE_BUSY
    assert gate.blocking_run_id == "run-9"
    assert gate.is_conflict


def test_a_run_in_a_different_workspace_does_not_block(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    task = _task("T1", workspace_path=str(tmp_path))
    gate = live_board.launch_gate(
        task, tasks_by_id=_by_id(task), active_runs=[{"id": "r", "task_id": "X", "repository_path": str(other)}]
    )

    assert gate.allowed


def test_workspace_busy_is_detected_through_path_normalisation(tmp_path):
    """The same directory reached by a non-normalised path is the same
    directory: a gate that compared raw strings would let a second agent into
    an occupied worktree."""
    task = _task("T1", workspace_path=str(tmp_path))
    noisy = str(tmp_path / "sub" / "..")
    (tmp_path / "sub").mkdir()
    gate = live_board.launch_gate(
        task, tasks_by_id=_by_id(task), active_runs=[{"id": "r", "task_id": "X", "repository_path": noisy}]
    )

    assert not gate.allowed
    assert gate.code == live_board.GATE_WORKSPACE_BUSY


def test_missing_workspace_is_reported_rather_than_launched():
    task = _task("T1")
    gate = live_board.launch_gate(task, tasks_by_id=_by_id(task), active_runs=[])

    assert not gate.allowed
    assert gate.code == live_board.GATE_WORKSPACE_NOT_CONFIGURED
    assert not gate.is_conflict


def test_project_repository_path_is_used_when_the_task_has_none(tmp_path):
    task = _task("T1")
    gate = live_board.launch_gate(
        task, tasks_by_id=_by_id(task), active_runs=[], project_repository_path=str(tmp_path)
    )

    assert gate.allowed


def test_every_blocking_code_carries_an_action(tmp_path):
    """A disabled button that does not say what to do about it is the defect
    this gate exists to avoid."""
    cases = [
        _task("A", status="Done"),
        _task("B", depends_on=["MISSING"]),
        _task("C"),
    ]
    for task in cases:
        gate = live_board.launch_gate(task, tasks_by_id=_by_id(task), active_runs=[])
        assert not gate.allowed
        assert gate.action, f"{gate.code} без рекомендации"


# --------------------------------------------------------------------------
# Dependency tree (one task's neighbourhood)
# --------------------------------------------------------------------------


def test_dependency_tree_walks_upstream_transitively():
    a = _task("A")
    b = _task("B", depends_on=["A"])
    c = _task("C", depends_on=["B"])
    nodes = live_board.dependency_tree(c, _by_id(a, b, c))

    assert [(n.task_id, n.depth) for n in nodes] == [("B", 1), ("A", 2)]


def test_dependency_tree_respects_max_depth():
    a, b, c = _task("A"), _task("B", depends_on=["A"]), _task("C", depends_on=["B"])
    nodes = live_board.dependency_tree(c, _by_id(a, b, c), max_depth=1)

    assert [n.task_id for n in nodes] == ["B"]


def test_dependency_tree_terminates_on_a_cycle():
    a = _task("A", depends_on=["B"])
    b = _task("B", depends_on=["A"])
    nodes = live_board.dependency_tree(a, _by_id(a, b))

    assert [n.task_id for n in nodes] == ["B"]


def test_dependency_tree_marks_done_dependencies():
    dep = _task("DEP", status="Done")
    task = _task("T", depends_on=["DEP"])
    (node,) = live_board.dependency_tree(task, _by_id(dep, task))

    assert node.done is True


def test_dependency_tree_is_empty_without_relations():
    task = _task("T")
    assert live_board.dependency_tree(task, _by_id(task)) == []


# --------------------------------------------------------------------------
# Project tree (a project's plan as levels)
# --------------------------------------------------------------------------


def test_project_tree_assigns_dependency_levels():
    a = _task("A")
    b = _task("B", depends_on=["A"])
    c = _task("C", depends_on=["B"])
    nodes = live_board.project_tree([a, b, c], _by_id(a, b, c))

    assert {n.task_id: n.level for n in nodes} == {"A": 0, "B": 1, "C": 2}


def test_project_tree_level_is_the_longest_path_not_the_shortest():
    """A task is only reachable once its *slowest* prerequisite chain is
    done, so it belongs on the level after the deepest dependency."""
    a = _task("A")
    b = _task("B", depends_on=["A"])
    c = _task("C", depends_on=["A", "B"])
    nodes = live_board.project_tree([a, b, c], _by_id(a, b, c))

    assert {n.task_id: n.level for n in nodes}["C"] == 2


def test_project_tree_terminates_on_a_dependency_cycle():
    a = _task("A", depends_on=["B"])
    b = _task("B", depends_on=["A"])
    nodes = live_board.project_tree([a, b], _by_id(a, b))

    assert len(nodes) == 2


def test_project_tree_states_reflect_what_a_task_is_doing():
    done = _task("D", status="Done")
    running = _task("R")
    blocked = _task("BL", status="Blocked")
    waiting = _task("W", depends_on=["BL"])
    ready = _task("RD")
    by_id = _by_id(done, running, blocked, waiting, ready)

    nodes = {
        n.task_id: n
        for n in live_board.project_tree(
            [done, running, blocked, waiting, ready], by_id, running_task_ids=frozenset({"R"})
        )
    }

    assert nodes["D"].state == live_board.NODE_DONE
    assert nodes["R"].state == live_board.NODE_RUNNING
    assert nodes["BL"].state == live_board.NODE_BLOCKED
    assert nodes["W"].state == live_board.NODE_WAITING
    assert nodes["RD"].state == live_board.NODE_READY


def test_running_beats_blocked_status_because_a_process_is_actually_up():
    task = _task("T", status="Blocked")
    (node,) = live_board.project_tree([task], _by_id(task), running_task_ids=frozenset({"T"}))

    assert node.state == live_board.NODE_RUNNING


def test_cross_project_dependency_makes_a_task_wait_without_adding_a_level():
    """A dependency outside this project still blocks the task, but the level
    axis stays this project's own sequence."""
    outside = _task("OUT", status="Backlog")
    task = _task("T", depends_on=["OUT"])
    (node,) = live_board.project_tree([task], _by_id(outside, task))

    assert node.level == 0
    assert node.state == live_board.NODE_WAITING


def test_project_tree_flags_the_next_task_to_start():
    a = _task("A", status="Done")
    b = _task("B", depends_on=["A"])
    c = _task("C", depends_on=["B"])
    nodes = live_board.project_tree([a, b, c], _by_id(a, b, c))

    assert [n.task_id for n in nodes if n.is_next] == ["B"]


def test_no_next_flag_when_nothing_is_ready():
    blocked = _task("BL", status="Blocked")
    waiting = _task("W", depends_on=["BL"])
    nodes = live_board.project_tree([blocked, waiting], _by_id(blocked, waiting))

    assert not any(n.is_next for n in nodes)


def test_project_progress_counts_only_merged_work():
    a = _task("A", status="Done")
    b = _task("B")
    nodes = live_board.project_tree([a, b], _by_id(a, b))

    assert live_board.project_progress(nodes) == (1, 2)


def test_every_node_state_has_a_colour_mark_and_label():
    for state in (
        live_board.NODE_DONE,
        live_board.NODE_RUNNING,
        live_board.NODE_BLOCKED,
        live_board.NODE_READY,
        live_board.NODE_WAITING,
    ):
        node = live_board.ProjectNode("id", "t", "s", 0, state)
        assert node.color and node.mark and node.state_label


# --------------------------------------------------------------------------
# Capacity summary — load and free agents
# --------------------------------------------------------------------------


def test_capacity_summary_reports_free_global_slots():
    summary = live_board.capacity_summary(
        running_by_agent={"claude_code": 1},
        global_running=1,
        global_limit=3,
        agents=[("claude_code", 3, True)],
    )
    assert summary.global_running == 1
    assert summary.global_free == 2
    assert summary.free_agent_count == 1
    assert not summary.saturated


def test_capacity_summary_is_saturated_when_every_agent_is_full():
    summary = live_board.capacity_summary(
        running_by_agent={"claude_code": 3},
        global_running=3,
        global_limit=3,
        agents=[("claude_code", 3, True)],
    )
    assert summary.saturated
    assert summary.global_free == 0
    assert summary.free_agent_count == 0


def test_capacity_global_free_never_exceeds_agent_free():
    # Global limit has room (4-1=3) but the single agent is nearly full
    # (2-2=0 free) — usable capacity is the tighter of the two, not the global.
    summary = live_board.capacity_summary(
        running_by_agent={"claude_code": 2},
        global_running=1,
        global_limit=4,
        agents=[("claude_code", 2, True)],
    )
    assert summary.agents_free == 0
    assert summary.global_free == 0


def test_capacity_unavailable_agent_contributes_no_free_slots():
    summary = live_board.capacity_summary(
        running_by_agent={},
        global_running=0,
        global_limit=3,
        agents=[("ollama", 2, False)],
    )
    assert summary.agents_free == 0
    assert summary.free_agent_count == 0
    assert summary.saturated  # global has room but no agent can take work


def test_capacity_spreads_free_count_across_multiple_agents():
    summary = live_board.capacity_summary(
        running_by_agent={"claude_code": 1},
        global_running=1,
        global_limit=8,
        agents=[("claude_code", 2, True), ("codex", 2, True), ("ollama", 2, False)],
    )
    assert summary.free_agent_count == 2  # claude_code + codex, not the down ollama
    assert summary.agents_free == 3       # 1 + 2
    assert summary.global_free == 3       # bounded by agent capacity, not the 7 global slots


# --------------------------------------------------------------------------
# Superseded runs — an old failed attempt is dropped once a newer run exists
# --------------------------------------------------------------------------


def test_superseded_run_ids_marks_older_attempts_of_a_task():
    sessions = [
        {"run_id": "old", "task_id": "T", "started_at": "2026-01-01T00:00:00"},
        {"run_id": "new", "task_id": "T", "started_at": "2026-01-01T01:00:00"},
    ]
    assert live_board.superseded_run_ids(sessions) == frozenset({"old"})


def test_completed_task_run_ids_removes_resolved_failures_from_attention():
    sessions = [
        {"run_id": "failed-done", "task_id": "done-task"},
        {"run_id": "failed-open", "task_id": "open-task"},
        {"run_id": "ad-hoc", "task_id": None},
    ]
    tasks_by_id = {
        "done-task": {"id": "done-task", "status": "Done"},
        "open-task": {"id": "open-task", "status": "Backlog"},
    }

    assert live_board.completed_task_run_ids(sessions, tasks_by_id) == frozenset(
        {"failed-done"}
    )


def test_superseded_leaves_a_single_run_alone():
    sessions = [{"run_id": "only", "task_id": "T", "started_at": "2026-01-01T00:00:00"}]
    assert live_board.superseded_run_ids(sessions) == frozenset()


def test_superseded_never_touches_ad_hoc_runs_without_a_task():
    sessions = [
        {"run_id": "a", "task_id": None, "started_at": "2026-01-01T00:00:00"},
        {"run_id": "b", "task_id": None, "started_at": "2026-01-01T01:00:00"},
    ]
    assert live_board.superseded_run_ids(sessions) == frozenset()
