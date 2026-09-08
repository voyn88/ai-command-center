"""Real work-attempt evidence -> AttemptOutcome (VOYN-MIN-AGT-EVO2).

Verifies ``outcomes_from_work_items`` reconstructs the executor a finished
attempt used purely from ``attempt_no`` and ``routing.cascade_for``, mirrors
the worker's own clamped-index resolution, ignores in-flight attempts, and
never raises for a domain with no configured cascade.
"""

from __future__ import annotations

from command_center.orchestrator.routing import ROUTING_MATRIX, cascade_for
from command_center.orchestrator.self_assessment_source import (
    FINISHED_ATTEMPT_STATES,
    outcomes_from_work_items,
)


def test_maps_each_finished_attempt_to_its_cascade_position():
    cascade = cascade_for("implementation")
    items = [
        {
            "work_item_id": "wi-1",
            "attempts": [
                {"attempt_no": 1, "state": "dead", "updated_at": "2026-08-01T00:00:00+00:00"},
                {
                    "attempt_no": 2,
                    "state": "succeeded",
                    "updated_at": "2026-08-01T01:00:00+00:00",
                },
            ],
        }
    ]

    outcomes = outcomes_from_work_items(items, task_class="implementation")

    assert [(o.executor, o.succeeded) for o in outcomes] == [
        (cascade[0]["executor"], False),
        (cascade[1]["executor"], True),
    ]
    assert all(o.task_class == "implementation" for o in outcomes)


def test_clamps_attempt_no_beyond_cascade_length():
    cascade = cascade_for("implementation")
    items = [
        {
            "attempts": [
                {"attempt_no": 99, "state": "succeeded", "updated_at": "2026-08-01T00:00:00+00:00"}
            ]
        }
    ]

    outcomes = outcomes_from_work_items(items, task_class="implementation")

    assert outcomes[0].executor == cascade[-1]["executor"]


def test_ignores_in_flight_attempts():
    items = [
        {
            "attempts": [
                {"attempt_no": 1, "state": "claimed", "updated_at": "2026-08-01T00:00:00+00:00"},
                {"attempt_no": 1, "state": "ready", "updated_at": "2026-08-01T00:00:00+00:00"},
            ]
        }
    ]

    assert outcomes_from_work_items(items, task_class="implementation") == []


def test_finished_states_match_terminal_vocabulary():
    assert FINISHED_ATTEMPT_STATES == frozenset({"succeeded", "dead"})


def test_unknown_task_class_yields_no_evidence_without_raising():
    assert "does-not-exist" not in ROUTING_MATRIX

    assert outcomes_from_work_items([{"attempts": []}], task_class="does-not-exist") == []


def test_missing_attempt_no_defaults_to_first_cascade_link():
    cascade = cascade_for("implementation")
    items = [{"attempts": [{"state": "succeeded", "updated_at": "2026-08-01T00:00:00+00:00"}]}]

    outcomes = outcomes_from_work_items(items, task_class="implementation")

    assert outcomes[0].executor == cascade[0]["executor"]
