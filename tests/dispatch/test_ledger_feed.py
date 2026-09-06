"""Unit tests for the real-row adapter (`dispatch.ledger_feed`).

Every property is asserted directly against `ledger_entry_from_run` with
plain dicts shaped like the real `run` / `completion` / `run_provenance` rows
-- no database, no filesystem, matching the rest of `dispatch`.
"""

from __future__ import annotations

import json

from command_center.dispatch.ledger_feed import (
    _UNKNOWN_EXECUTOR,
    _agent_from_command,
    ledger_entry_from_run,
)


def _run(**overrides) -> dict:
    base = {
        "project": "AICC",
        "task_type": "implementation",
        "command_json": json.dumps(["claude", "-p", "do the thing"]),
        "started_at": "2026-09-01T10:00:00",
        "completed_at": "2026-09-01T10:05:00",
        "state": "COMPLETED",
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# _agent_from_command
# --------------------------------------------------------------------------


def test_agent_from_command_extracts_argv0_basename():
    assert _agent_from_command(json.dumps(["claude", "-p", "x"])) == "claude"
    assert _agent_from_command(json.dumps(["/usr/local/bin/codex"])) == "codex"


def test_agent_from_command_accepts_already_decoded_list():
    # A jsonb-backed Postgres mirror read hands back a decoded object, not
    # JSON text -- same shape `daily_spend_usd` has to tolerate.
    assert _agent_from_command(["copilot", "run"]) == "copilot"


def test_agent_from_command_none_on_missing_or_malformed_input():
    assert _agent_from_command(None) is None
    assert _agent_from_command("not-json") is None
    assert _agent_from_command(json.dumps([])) is None
    assert _agent_from_command(json.dumps({"not": "a list"})) is None
    assert _agent_from_command(json.dumps([123])) is None


# --------------------------------------------------------------------------
# ledger_entry_from_run: executor_id
# --------------------------------------------------------------------------


def test_executor_id_comes_from_the_launched_command():
    entry = ledger_entry_from_run(_run())
    assert entry.executor_id == "claude"


def test_executor_id_falls_back_to_a_sentinel_not_none():
    entry = ledger_entry_from_run(_run(command_json=None))
    assert entry.executor_id == _UNKNOWN_EXECUTOR


# --------------------------------------------------------------------------
# ledger_entry_from_run: task_class
# --------------------------------------------------------------------------


def test_task_class_is_composed_from_project_and_task_type():
    entry = ledger_entry_from_run(_run(project="AICC", task_type="review"))
    assert entry.task_class == "AICC:review"


def test_task_class_falls_back_on_missing_attributes_like_task_class_for_does():
    entry = ledger_entry_from_run(_run(project=None, task_type=None))
    assert entry.task_class == "unassigned:unspecified"


# --------------------------------------------------------------------------
# ledger_entry_from_run: merged_sha (provenance.accepted_sha over
# completion.merge_commit)
# --------------------------------------------------------------------------


def test_merged_sha_prefers_provenance_accepted_sha():
    entry = ledger_entry_from_run(
        _run(),
        completion={"merge_commit": "fromcompletion"},
        provenance={"accepted_sha": "fromprovenance"},
    )
    assert entry.merged_sha == "fromprovenance"


def test_merged_sha_falls_back_to_completion_merge_commit():
    entry = ledger_entry_from_run(_run(), completion={"merge_commit": "fromcompletion"})
    assert entry.merged_sha == "fromcompletion"


def test_merged_sha_is_none_without_either_row():
    entry = ledger_entry_from_run(_run())
    assert entry.merged_sha is None


def test_blank_accepted_sha_does_not_shadow_a_real_merge_commit():
    entry = ledger_entry_from_run(
        _run(),
        completion={"merge_commit": "fromcompletion"},
        provenance={"accepted_sha": "  "},
    )
    assert entry.merged_sha == "fromcompletion"


# --------------------------------------------------------------------------
# ledger_entry_from_run: review_verdict, outcome, duration, cost
# --------------------------------------------------------------------------


def test_review_verdict_comes_from_completion():
    entry = ledger_entry_from_run(_run(), completion={"review_verdict": "approved"})
    assert entry.review_verdict == "approved"


def test_review_verdict_is_none_without_a_completion_row():
    entry = ledger_entry_from_run(_run())
    assert entry.review_verdict is None


def test_accepted_requires_both_merged_sha_and_review_verdict_end_to_end():
    accepted = ledger_entry_from_run(
        _run(),
        completion={"merge_commit": "sha1", "review_verdict": "approved"},
    )
    assert accepted.accepted is True

    only_merged = ledger_entry_from_run(_run(), completion={"merge_commit": "sha1"})
    assert only_merged.accepted is False

    only_reviewed = ledger_entry_from_run(
        _run(), completion={"review_verdict": "approved"}
    )
    assert only_reviewed.accepted is False


def test_outcome_is_the_run_state():
    entry = ledger_entry_from_run(_run(state="FAILED"))
    assert entry.outcome == "FAILED"


def test_outcome_defaults_to_empty_string_when_state_is_missing():
    entry = ledger_entry_from_run(_run(state=None))
    assert entry.outcome == ""


def test_duration_is_computed_from_started_and_completed_at():
    entry = ledger_entry_from_run(_run())
    assert entry.duration_seconds == 300.0


def test_duration_defaults_to_zero_when_timestamps_are_missing_or_backwards():
    still_running = ledger_entry_from_run(_run(completed_at=None))
    assert still_running.duration_seconds == 0.0

    clock_skew = ledger_entry_from_run(
        _run(started_at="2026-09-01T10:05:00", completed_at="2026-09-01T10:00:00")
    )
    assert clock_skew.duration_seconds == 0.0


def test_cost_usd_is_passed_through_when_positive():
    entry = ledger_entry_from_run(_run(), cost_usd=2.5)
    assert entry.cost_usd == 2.5


def test_cost_usd_defaults_to_zero_for_non_positive_or_malformed_input():
    for bad_cost in (0.0, -1.0, "free", True, None):
        entry = ledger_entry_from_run(_run(), cost_usd=bad_cost)
        assert entry.cost_usd == 0.0


# --------------------------------------------------------------------------
# The two fields with no real source anywhere yet: honest defaults, never
# invented values.
# --------------------------------------------------------------------------


def test_tokens_skills_and_escalation_reason_are_honestly_absent():
    entry = ledger_entry_from_run(
        _run(), completion={"merge_commit": "sha1", "review_verdict": "approved"}
    )
    assert entry.tokens == 0
    assert entry.skills == ()
    assert entry.escalation_reason is None


def test_is_pure_and_deterministic():
    first = ledger_entry_from_run(_run())
    second = ledger_entry_from_run(_run())
    assert first == second
