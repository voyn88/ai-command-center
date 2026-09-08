"""The scheduled entrypoint that closes the self-assessment loop
(VOYN-MIN-AGT-EVO2).

Verifies ``self_assessment_runner`` actually wires real fleet reads
(``WorkQueueReadStore``'s ``list_items``/``get_item`` shape) through
``self_assessment_source.outcomes_from_work_items`` into ``run_quarterly_
self_assessment`` -- the missing call site between the three already-tested
pure/durable pieces and something that runs against live data on a
schedule.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from command_center.orchestrator.routing import ROUTING_MATRIX, cascade_for
from command_center.orchestrator.self_assessment import Quarter
from command_center.orchestrator.self_assessment_runner import (
    collect_outcomes,
    resolve_db_path,
    run_self_assessment,
)
from command_center.orchestrator.self_assessment_store import SelfAssessmentStore


class _FakeReadStore:
    """A minimal stand-in for ``WorkQueueReadStore`` shaped exactly like its
    real ``list_items``/``get_item`` contract, so ``collect_outcomes`` is
    exercised against the same method surface it calls in production
    without needing a live PostgreSQL connection."""

    def __init__(self, items_by_queue: dict[str, list[dict]]) -> None:
        self._items_by_queue = items_by_queue
        self._by_id = {
            item["work_item_id"]: item
            for items in items_by_queue.values()
            for item in items
        }

    def list_items(self, *, queue=None, state=None, limit=100):
        items = self._items_by_queue.get(queue, [])
        if state is not None:
            items = [item for item in items if item["state"] == state]
        return [
            {"work_item_id": item["work_item_id"], "state": item["state"]}
            for item in items[:limit]
        ]

    def get_item(self, work_item_id):
        return self._by_id.get(work_item_id)


def _finished_item(work_item_id: str, state: str, attempts: list[dict]) -> dict:
    return {"work_item_id": work_item_id, "state": state, "attempts": attempts}


def test_collect_outcomes_reads_every_configured_task_class():
    implementation_cascade = cascade_for("implementation")
    review_cascade = cascade_for("review")
    read_store = _FakeReadStore(
        {
            "implementation": [
                _finished_item(
                    "wi-1",
                    "succeeded",
                    [
                        {
                            "attempt_no": 1,
                            "state": "succeeded",
                            "updated_at": "2026-08-01T00:00:00+00:00",
                        }
                    ],
                ),
                _finished_item(
                    "wi-2",
                    "dead",
                    [
                        {
                            "attempt_no": 1,
                            "state": "dead",
                            "updated_at": "2026-08-02T00:00:00+00:00",
                        },
                        {
                            "attempt_no": 2,
                            "state": "ready",
                            "updated_at": "2026-08-02T01:00:00+00:00",
                        },
                    ],
                ),
            ],
            "review": [
                _finished_item(
                    "wi-3",
                    "succeeded",
                    [
                        {
                            "attempt_no": 1,
                            "state": "succeeded",
                            "updated_at": "2026-08-03T00:00:00+00:00",
                        }
                    ],
                ),
            ],
        }
    )

    outcomes = collect_outcomes(read_store)

    by_task_class = {}
    for outcome in outcomes:
        by_task_class.setdefault(outcome.task_class, []).append(outcome)

    assert {o.succeeded for o in by_task_class["implementation"]} == {True, False}
    assert all(
        o.executor == implementation_cascade[0]["executor"]
        for o in by_task_class["implementation"]
    )
    assert by_task_class["review"][0].executor == review_cascade[0]["executor"]
    # The in-flight (ready) second attempt of wi-2 must never surface as
    # evidence -- collect_outcomes delegates that filter to
    # outcomes_from_work_items, and this asserts the delegation actually
    # happened rather than the runner re-deriving its own (possibly looser)
    # filter.
    assert len(by_task_class["implementation"]) == 2


def test_collect_outcomes_restricts_to_requested_task_classes():
    read_store = _FakeReadStore(
        {
            "implementation": [
                _finished_item(
                    "wi-1",
                    "succeeded",
                    [
                        {
                            "attempt_no": 1,
                            "state": "succeeded",
                            "updated_at": "2026-08-01T00:00:00+00:00",
                        }
                    ],
                )
            ],
            "review": [
                _finished_item(
                    "wi-2",
                    "succeeded",
                    [
                        {
                            "attempt_no": 1,
                            "state": "succeeded",
                            "updated_at": "2026-08-01T00:00:00+00:00",
                        }
                    ],
                )
            ],
        }
    )

    outcomes = collect_outcomes(read_store, task_classes=["review"])

    assert {o.task_class for o in outcomes} == {"review"}


def test_run_self_assessment_persists_a_report_through_the_real_store(tmp_path: Path):
    store = SelfAssessmentStore(tmp_path / "self_assessment.sqlite3")
    cascade = cascade_for("implementation")
    assert len(cascade) == 3, "test evidence below assumes a 3-link cascade"
    best_executor = cascade[2]["executor"]

    # Every link needs >= min_samples (default 20) observed attempts for a
    # recommendation to be produced at all (recommend_cascade withholds
    # otherwise). Each work item below burns through links 1 and 2 (always
    # failing) before link 3 finally succeeds, so 20 items give every one of
    # the three links exactly 20 samples, and link 3's 100% success rate
    # against the other two's 0% gives an unambiguous rotation to the front.
    items: dict[str, list[dict]] = {"implementation": []}
    for i in range(20):
        items["implementation"].append(
            _finished_item(
                f"wi-{i}",
                "succeeded",
                [
                    {
                        "attempt_no": 1,
                        "state": "dead",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    },
                    {
                        "attempt_no": 2,
                        "state": "dead",
                        "updated_at": "2026-01-01T01:00:00+00:00",
                    },
                    {
                        "attempt_no": 3,
                        "state": "succeeded",
                        "updated_at": "2026-01-01T02:00:00+00:00",
                    },
                ],
            )
        )
    read_store = _FakeReadStore(items)

    report = run_self_assessment(
        store=store,
        read_store=read_store,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )

    assert report["due"] is True
    assert store.last_assessed_at() == datetime(2026, 8, 15, tzinfo=timezone.utc)
    recommendation = store.latest_configuration("implementation")
    assert recommendation is not None
    assert recommendation[0]["executor"] == best_executor


def test_run_self_assessment_is_a_cheap_no_op_when_not_yet_due(tmp_path: Path):
    store = SelfAssessmentStore(tmp_path / "self_assessment.sqlite3")
    store.record(
        "implementation",
        Quarter(2026, 3),
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        None,
    )
    read_store = _FakeReadStore({})

    report = run_self_assessment(
        store=store,
        read_store=read_store,
        now=datetime(2026, 8, 15, tzinfo=timezone.utc),
    )

    assert report["due"] is False
    assert report["recommendations"] == {}


def test_resolve_db_path_honors_aicc_data_dir(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("AICC_DATA_DIR", str(tmp_path / "custom"))
    assert resolve_db_path() == tmp_path / "custom" / "self_assessment.db"
