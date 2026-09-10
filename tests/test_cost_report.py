"""Tests for `command_center.runtime.cost_report` (VOYN-MIN-AGT-COST-METRIC).

Cost-per-meaningful-task is a FinOps acceptance metric: for each agent
(`run.provider_id`), how much its reported spend cost per task that actually
reached `completion.CompletionState.COMPLETED` — never raw run count or raw
dollars alone.
"""

from __future__ import annotations

from pathlib import Path

from command_center.runtime import db as runtime_db
from command_center.runtime.completion import CompletionState
from command_center.runtime.cost_report import (
    cost_per_meaningful_task,
    render_markdown,
)


def _make_run(db_path: Path, *, provider_id: str, project: str = "AIOS"):
    if not db_path.exists():
        runtime_db.migrate(db_path)
    task = runtime_db.create_task(db_path, project=project, title="t", task_type="implementation")
    session = runtime_db.create_session(
        db_path, task_id=task["id"], project=project, repository_path="/tmp/x"
    )
    run = runtime_db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project=project,
        task_type="implementation",
        repository_path="/tmp/x",
        prompt="do thing",
        is_resume=False,
        provider_id=provider_id,
    )
    return task, run


def _complete(db_path: Path, *, task_id: str, run_id: str, state: str = CompletionState.COMPLETED):
    runtime_db.create_completion(
        db_path,
        run_id=run_id,
        task_id=task_id,
        project="AIOS",
        repository_path="/tmp/x",
        completion_state=state,
    )


def test_agent_with_no_runs_at_all_is_absent(tmp_path):
    db_path = tmp_path / "runtime.db"
    runtime_db.migrate(db_path)
    assert cost_per_meaningful_task(db_path) == []


def test_cost_divided_by_meaningful_tasks_per_agent(tmp_path):
    db_path = tmp_path / "runtime.db"

    # claude_code: two completed (meaningful) runs costing $1.00 and $2.00 ->
    # $3.00 / 2 meaningful tasks = $1.50 per meaningful task.
    task1, run1 = _make_run(db_path, provider_id="claude_code")
    runtime_db.append_run_event(
        db_path, run1["id"], "stream_event", {"type": "result", "total_cost_usd": 1.0}
    )
    _complete(db_path, task_id=task1["id"], run_id=run1["id"])

    task2, run2 = _make_run(db_path, provider_id="claude_code")
    runtime_db.append_run_event(
        db_path, run2["id"], "stream_event", {"type": "result", "total_cost_usd": 2.0}
    )
    _complete(db_path, task_id=task2["id"], run_id=run2["id"])

    reports = {r.agent_id: r for r in cost_per_meaningful_task(db_path)}
    claude = reports["claude_code"]
    assert claude.total_cost_usd == 3.0
    assert claude.run_count == 2
    assert claude.meaningful_task_count == 2
    assert claude.cost_per_meaningful_task_usd == 1.5


def test_non_meaningful_completion_states_do_not_count(tmp_path):
    db_path = tmp_path / "runtime.db"

    task, run = _make_run(db_path, provider_id="ollama")
    runtime_db.append_run_event(
        db_path, run["id"], "stream_event", {"type": "result", "total_cost_usd": 5.0}
    )
    _complete(db_path, task_id=task["id"], run_id=run["id"], state=CompletionState.VALIDATION_FAILED)

    report = cost_per_meaningful_task(db_path)[0]
    assert report.agent_id == "ollama"
    assert report.total_cost_usd == 5.0
    assert report.meaningful_task_count == 0
    # Never a fabricated 0.0 or infinity: an idle/failing agent has no rate.
    assert report.cost_per_meaningful_task_usd is None


def test_run_with_no_reported_cost_contributes_zero(tmp_path):
    db_path = tmp_path / "runtime.db"

    task, run = _make_run(db_path, provider_id="claude_code")
    _complete(db_path, task_id=task["id"], run_id=run["id"])

    report = cost_per_meaningful_task(db_path)[0]
    assert report.total_cost_usd == 0.0
    assert report.meaningful_task_count == 1
    assert report.cost_per_meaningful_task_usd == 0.0


def test_agents_are_isolated_from_each_other(tmp_path):
    db_path = tmp_path / "runtime.db"

    task_a, run_a = _make_run(db_path, provider_id="agent_a")
    runtime_db.append_run_event(
        db_path, run_a["id"], "stream_event", {"type": "result", "total_cost_usd": 10.0}
    )
    _complete(db_path, task_id=task_a["id"], run_id=run_a["id"])

    task_b, run_b = _make_run(db_path, provider_id="agent_b")
    runtime_db.append_run_event(
        db_path, run_b["id"], "stream_event", {"type": "result", "total_cost_usd": 0.5}
    )
    _complete(db_path, task_id=task_b["id"], run_id=run_b["id"])

    reports = {r.agent_id: r for r in cost_per_meaningful_task(db_path)}
    assert reports["agent_a"].cost_per_meaningful_task_usd == 10.0
    assert reports["agent_b"].cost_per_meaningful_task_usd == 0.5


def test_render_markdown_lists_every_agent_and_handles_empty(tmp_path):
    db_path = tmp_path / "runtime.db"
    task, run = _make_run(db_path, provider_id="claude_code")
    runtime_db.append_run_event(
        db_path, run["id"], "stream_event", {"type": "result", "total_cost_usd": 3.0}
    )
    _complete(db_path, task_id=task["id"], run_id=run["id"])

    reports = cost_per_meaningful_task(db_path)
    markdown = render_markdown(reports)
    assert "claude_code" in markdown
    assert "3.0000" in markdown

    assert "no runs recorded" in render_markdown([])
