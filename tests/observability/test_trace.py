"""One trace_id per backlog task, computable independently by every stage."""

from __future__ import annotations

import json
import logging

from command_center.observability.trace import (
    PIPELINE_STAGES,
    log_span,
    new_trace_id,
    trace_id_for_task,
)


def test_trace_id_for_task_is_deterministic() -> None:
    assert trace_id_for_task("VOYN-W0-EXAMPLE") == trace_id_for_task("VOYN-W0-EXAMPLE")


def test_trace_id_for_task_differs_across_tasks() -> None:
    assert trace_id_for_task("VOYN-W0-A") != trace_id_for_task("VOYN-W0-B")


def test_new_trace_id_is_fresh_each_call() -> None:
    assert new_trace_id() != new_trace_id()


def test_log_span_emits_one_json_line_carrying_the_derived_trace_id(
    caplog,
) -> None:
    caplog.set_level(logging.INFO, logger="command_center.trace")
    returned = log_span("claim", task_id="VOYN-W0-EXAMPLE", attempt_id="atmpt_1")

    assert returned == trace_id_for_task("VOYN-W0-EXAMPLE")
    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].message)
    assert payload["trace_id"] == returned
    assert payload["stage"] == "claim"
    assert payload["task_id"] == "VOYN-W0-EXAMPLE"
    assert payload["attempt_id"] == "atmpt_1"
    assert "ts" in payload


def test_every_stage_shares_the_task_ids_trace_id_across_the_whole_pipeline(
    caplog,
) -> None:
    """The acceptance bar: one grep for one trace_id shows plan through merge."""
    caplog.set_level(logging.INFO, logger="command_center.trace")
    task_id = "VOYN-W0-EXAMPLE"
    for stage in PIPELINE_STAGES:
        log_span(stage, task_id=task_id)

    trace_ids = {json.loads(r.message)["trace_id"] for r in caplog.records}
    stages = {json.loads(r.message)["stage"] for r in caplog.records}
    assert trace_ids == {trace_id_for_task(task_id)}
    assert stages == set(PIPELINE_STAGES)


def test_log_span_accepts_an_explicit_trace_id_override(caplog) -> None:
    caplog.set_level(logging.INFO, logger="command_center.trace")
    returned = log_span("plan", task_id="VOYN-W0-EXAMPLE", trace_id="fixed-trace")
    assert returned == "fixed-trace"
    payload = json.loads(caplog.records[0].message)
    assert payload["trace_id"] == "fixed-trace"
