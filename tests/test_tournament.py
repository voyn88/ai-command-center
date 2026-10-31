"""Unit tests for the pure Субъектные турниры domain module
(``command_center.tournament``): category classification, monthly tallying,
ranking and the JSON round-trip.
"""

from __future__ import annotations

from command_center import tournament


def _task(task_id, *, category=None, discipline=None):
    metadata = {}
    if discipline is not None:
        metadata["discipline"] = discipline
    task = {"id": task_id, "metadata": metadata}
    if category is not None:
        task["category"] = category
    return task


def _run(*, task_id, agent, state="COMPLETED", completed_at="2026-08-15T10:00:00", created_at=None):
    return {
        "task_id": task_id,
        "agent": agent,
        "state": state,
        "completed_at": completed_at,
        "created_at": created_at or completed_at,
    }


# --------------------------------------------------------------------------
# task_category
# --------------------------------------------------------------------------


def test_task_category_reads_top_level_category_case_insensitively():
    assert tournament.task_category(_task("t1", category="dev")) == "Dev"
    assert tournament.task_category(_task("t1", category="SECURITY")) == "Security"


def test_task_category_reads_metadata_discipline_before_top_level():
    task = _task("t1", category="Ops", discipline="ux")
    # metadata is consulted before the top-level task dict, mirroring
    # `waves.wave_label`'s precedence.
    assert tournament.task_category(task) == "UX"


def test_task_category_none_for_missing_or_unrecognized_value():
    assert tournament.task_category({"id": "t1"}) is None
    assert tournament.task_category(_task("t1", category="")) is None
    assert tournament.task_category(_task("t1", category="marketing")) is None


# --------------------------------------------------------------------------
# build_monthly_protocol
# --------------------------------------------------------------------------


def test_build_monthly_protocol_has_every_category_even_when_empty():
    protocol = tournament.build_monthly_protocol([], {}, month="2026-08")
    assert protocol.month == "2026-08"
    assert set(protocol.categories) == set(tournament.CATEGORIES)
    assert all(standings == () for standings in protocol.categories.values())


def test_build_monthly_protocol_tallies_completed_runs_by_agent_and_category():
    tasks_by_id = {
        "t1": _task("t1", category="Dev"),
        "t2": _task("t2", category="Dev"),
    }
    runs = [
        _run(task_id="t1", agent="claude"),
        _run(task_id="t1", agent="claude"),
        _run(task_id="t2", agent="codex"),
    ]
    protocol = tournament.build_monthly_protocol(runs, tasks_by_id, month="2026-08")
    dev = protocol.categories["Dev"]
    assert dev[0].participant == "claude"
    assert dev[0].completed == 2
    assert dev[0].rank == 1
    assert dev[1].participant == "codex"
    assert dev[1].completed == 1
    assert dev[1].rank == 2
    assert protocol.champion("Dev").participant == "claude"


def test_build_monthly_protocol_ties_share_competition_rank():
    tasks_by_id = {"t1": _task("t1", category="Ops")}
    runs = [
        _run(task_id="t1", agent="agent-a"),
        _run(task_id="t1", agent="agent-b"),
    ]
    protocol = tournament.build_monthly_protocol(runs, tasks_by_id, month="2026-08")
    ops = protocol.categories["Ops"]
    assert {s.participant for s in ops} == {"agent-a", "agent-b"}
    assert ops[0].rank == 1
    assert ops[1].rank == 1


def test_build_monthly_protocol_excludes_runs_outside_month_or_uncategorized_or_incomplete():
    tasks_by_id = {
        "t1": _task("t1", category="Dev"),
        "t2": {"id": "t2"},  # no declared category
    }
    runs = [
        _run(task_id="t1", agent="claude", completed_at="2026-07-15T10:00:00"),  # wrong month
        _run(task_id="t2", agent="claude"),  # no category
        _run(task_id="t1", agent="claude", state="FAILED"),  # not completed
        _run(task_id="missing", agent="claude"),  # unknown task
    ]
    protocol = tournament.build_monthly_protocol(runs, tasks_by_id, month="2026-08")
    assert all(standings == () for standings in protocol.categories.values())


def test_build_monthly_protocol_defaults_month_to_now():
    from datetime import datetime

    now = datetime(2026, 8, 15, 12, 0, 0)
    protocol = tournament.build_monthly_protocol([], {}, now=now)
    assert protocol.month == "2026-08"


# --------------------------------------------------------------------------
# JSON round-trip
# --------------------------------------------------------------------------


def test_protocol_dict_round_trip():
    tasks_by_id = {"t1": _task("t1", category="Security")}
    runs = [_run(task_id="t1", agent="claude")]
    protocol = tournament.build_monthly_protocol(runs, tasks_by_id, month="2026-08")

    restored = tournament.protocol_from_dict(tournament.protocol_to_dict(protocol))

    assert restored == protocol
