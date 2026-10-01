from __future__ import annotations

from command_center.runtime import db
from command_center.runtime.completion import CompletionState
from command_center.runtime.cost_report import (
    AgentCostRow,
    build_agent_cost_report,
    build_agent_cost_report_from_db,
    fetch_agent_cost_rows,
    render_agent_cost_report_markdown,
)


def _fresh_db(tmp_path):
    path = tmp_path / "runtime.db"
    db.migrate(path)
    return path


def _make_run(db_path, *, project, provider_id, task_id=None):
    task = db.create_task(
        db_path,
        project=project,
        title="agent cost fixture",
        task_type="implementation",
        task_id=task_id,
    )
    session = db.create_session(
        db_path,
        task_id=task["id"],
        project=project,
        repository_path="/worktrees/fixture/task",
    )
    run = db.create_run(
        db_path,
        session_id=session["id"],
        task_id=task["id"],
        project=project,
        task_type="implementation",
        repository_path="/worktrees/fixture/task",
        prompt="do the thing",
        is_resume=False,
        provider_id=provider_id,
    )
    return task, run


def _complete_run(db_path, run, *, completion_state):
    db.create_completion(
        db_path,
        run_id=run["id"],
        task_id=run["task_id"],
        project=run["project"],
        repository_path=run["repository_path"],
        completion_state=completion_state,
    )


def _report_cost(db_path, run, amount):
    db.append_run_event(db_path, run["id"], "result", {"total_cost_usd": amount})


# --------------------------------------------------------------------------
# Pure aggregation
# --------------------------------------------------------------------------


def test_build_report_computes_cost_per_meaningful_task():
    rows = [
        AgentCostRow("AICC", "claude_code", "t1", 2.0, is_meaningful=True),
        AgentCostRow("AICC", "claude_code", "t2", 3.0, is_meaningful=True),
        AgentCostRow("AICC", "claude_code", "t3", 1.0, is_meaningful=False),
    ]
    [entry] = build_agent_cost_report(rows)
    assert entry.project == "AICC"
    assert entry.agent == "claude_code"
    assert entry.run_count == 3
    assert entry.meaningful_task_count == 2
    assert entry.total_cost_usd == 6.0
    # Unlanded spend still inflates the price of what did land.
    assert entry.cost_per_meaningful_task_usd == 3.0


def test_retries_on_the_same_task_do_not_inflate_meaningful_count():
    rows = [
        AgentCostRow("AICC", "claude_code", "t1", 1.0, is_meaningful=False),
        AgentCostRow("AICC", "claude_code", "t1", 1.0, is_meaningful=True),
    ]
    [entry] = build_agent_cost_report(rows)
    assert entry.run_count == 2
    assert entry.meaningful_task_count == 1
    assert entry.cost_per_meaningful_task_usd == 2.0


def test_no_meaningful_task_reports_none_not_zero_or_inf():
    rows = [AgentCostRow("AICC", "claude_code", "t1", 5.0, is_meaningful=False)]
    [entry] = build_agent_cost_report(rows)
    assert entry.meaningful_task_count == 0
    assert entry.cost_per_meaningful_task_usd is None
    assert entry.as_dict()["cost_per_meaningful_task_usd"] is None


def test_report_partitions_by_project_and_agent():
    rows = [
        AgentCostRow("AICC", "claude_code", "t1", 1.0, is_meaningful=True),
        AgentCostRow("AICC", "codex", "t2", 4.0, is_meaningful=True),
        AgentCostRow("BANK", "claude_code", "t3", 2.0, is_meaningful=True),
    ]
    entries = build_agent_cost_report(rows)
    keys = [(e.project, e.agent) for e in entries]
    assert keys == [("AICC", "claude_code"), ("AICC", "codex"), ("BANK", "claude_code")]


def test_render_markdown_sorts_cheapest_meaningful_first_within_project():
    rows = [
        AgentCostRow("AICC", "expensive", "t1", 10.0, is_meaningful=True),
        AgentCostRow("AICC", "cheap", "t2", 1.0, is_meaningful=True),
        AgentCostRow("AICC", "no-data", "t3", 5.0, is_meaningful=False),
    ]
    rendered = render_agent_cost_report_markdown(build_agent_cost_report(rows))
    cheap_pos = rendered.index("| AICC | cheap |")
    expensive_pos = rendered.index("| AICC | expensive |")
    no_data_pos = rendered.index("| AICC | no-data |")
    assert cheap_pos < expensive_pos < no_data_pos


def test_render_markdown_with_no_rows_says_so():
    rendered = render_agent_cost_report_markdown([])
    assert "нет данных" in rendered


# --------------------------------------------------------------------------
# DB reader
# --------------------------------------------------------------------------


def test_fetch_agent_cost_rows_reads_provider_reported_cost_and_completion(tmp_path):
    db_path = _fresh_db(tmp_path)
    _task, landed = _make_run(db_path, project="AICC", provider_id="claude_code")
    _report_cost(db_path, landed, 1.5)
    _complete_run(db_path, landed, completion_state=CompletionState.COMPLETED)

    _task2, stuck = _make_run(db_path, project="AICC", provider_id="claude_code")
    _report_cost(db_path, stuck, 0.5)
    _complete_run(db_path, stuck, completion_state=CompletionState.REQUIRES_ATTENTION)

    rows = fetch_agent_cost_rows(db_path)
    by_run_task = {row.task_id: row for row in rows}
    assert by_run_task[landed["task_id"]].total_cost_usd == 1.5
    assert by_run_task[landed["task_id"]].is_meaningful is True
    assert by_run_task[stuck["task_id"]].total_cost_usd == 0.5
    assert by_run_task[stuck["task_id"]].is_meaningful is False


def test_fetch_agent_cost_rows_run_without_cost_event_contributes_zero(tmp_path):
    db_path = _fresh_db(tmp_path)
    _task, run = _make_run(db_path, project="AICC", provider_id="claude_code")

    [row] = fetch_agent_cost_rows(db_path)
    assert row.total_cost_usd == 0.0
    assert row.is_meaningful is False


def test_fetch_agent_cost_rows_filters_by_project(tmp_path):
    db_path = _fresh_db(tmp_path)
    _make_run(db_path, project="AICC", provider_id="claude_code")
    _make_run(db_path, project="BANK", provider_id="claude_code")

    rows = fetch_agent_cost_rows(db_path, project="BANK")
    assert {row.project for row in rows} == {"BANK"}


def test_build_agent_cost_report_from_db_end_to_end(tmp_path):
    db_path = _fresh_db(tmp_path)
    _task, run = _make_run(db_path, project="AICC", provider_id="codex")
    _report_cost(db_path, run, 3.0)
    _complete_run(db_path, run, completion_state=CompletionState.COMPLETED)

    [entry] = build_agent_cost_report_from_db(db_path)
    assert entry.project == "AICC"
    assert entry.agent == "codex"
    assert entry.meaningful_task_count == 1
    assert entry.cost_per_meaningful_task_usd == 3.0
