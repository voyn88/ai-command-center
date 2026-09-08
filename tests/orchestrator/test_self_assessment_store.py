"""Persistence for the quarterly self-assessment cadence (VOYN-MIN-AGT-EVO2):
the SQLite store remembers when each domain was last assessed and what it
recommended, across separate process runs — the memory
``self_assessment.py``'s pure functions deliberately do not carry
themselves."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from command_center.orchestrator.routing import ROUTING_MATRIX, cascade_for
from command_center.orchestrator.self_assessment import AttemptOutcome, Quarter
from command_center.orchestrator.self_assessment_store import (
    SelfAssessmentStore,
    run_quarterly_self_assessment,
)


@pytest.fixture
def store(tmp_path: Path) -> SelfAssessmentStore:
    return SelfAssessmentStore(tmp_path / "self_assessment.sqlite3")


def _outcomes(*rows: tuple[str, str, bool]) -> list[AttemptOutcome]:
    return [
        AttemptOutcome(
            task_class=tc, executor=ex, succeeded=ok,
            occurred_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        for tc, ex, ok in rows
    ]


def test_a_fresh_store_has_never_been_assessed(store: SelfAssessmentStore):
    assert store.last_assessed_at() is None
    assert store.latest_configuration("implementation") is None
    assert store.history("implementation") == []


def test_record_and_read_back_a_recommendation(store: SelfAssessmentStore):
    current = cascade_for("review")
    rotated = list(reversed(current))
    store.record(
        "review", Quarter(2026, 3), datetime(2026, 8, 1, tzinfo=timezone.utc), rotated
    )
    assert store.latest_configuration("review") == rotated
    assert store.last_assessed_at() == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_re_recording_the_same_quarter_replaces_rather_than_appends(
    store: SelfAssessmentStore,
):
    q = Quarter(2026, 3)
    store.record("review", q, datetime(2026, 8, 1, tzinfo=timezone.utc), [{"executor": "a"}])
    store.record("review", q, datetime(2026, 8, 2, tzinfo=timezone.utc), [{"executor": "b"}])
    history = store.history("review")
    assert len(history) == 1
    assert history[0].recommendation == [{"executor": "b"}]


def test_withheld_recommendation_is_recorded_as_none_not_dropped(
    store: SelfAssessmentStore,
):
    store.record("review", Quarter(2026, 3), datetime(2026, 8, 1, tzinfo=timezone.utc), None)
    assert store.latest_configuration("review") is None
    history = store.history("review")
    assert len(history) == 1
    assert history[0].recommendation is None
    # A withheld assessment still counts as having happened.
    assert store.last_assessed_at() == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_run_quarterly_self_assessment_is_a_noop_when_not_due(
    store: SelfAssessmentStore,
):
    store.record(
        "implementation", Quarter(2026, 3), datetime(2026, 7, 1, tzinfo=timezone.utc), None
    )
    report = run_quarterly_self_assessment(
        store, outcomes=[], routing_matrix=ROUTING_MATRIX, now=date(2026, 8, 1)
    )
    assert report == {"due": False, "quarter": None, "recommendations": {}}
    # Nothing new was written.
    assert store.history("implementation") == [
        store.history("implementation")[0]
    ]


def test_run_quarterly_self_assessment_persists_every_domain_when_due(
    store: SelfAssessmentStore,
):
    outcomes = _outcomes(
        *[("review", "codex", True)] * 25,
        *[("review", "copilot", True)] * 25,
        *[("review", "claude", False)] * 25,
    )
    report = run_quarterly_self_assessment(
        store, outcomes=outcomes, routing_matrix=ROUTING_MATRIX, now=date(2026, 10, 1)
    )
    assert report["due"] is True
    assert set(report["recommendations"]) == set(ROUTING_MATRIX)

    # Persisted for every domain, matching what the report returned.
    for task_class, recommendation in report["recommendations"].items():
        assert store.history(task_class)[-1].recommendation == recommendation
    review_config = store.latest_configuration("review")
    assert review_config is not None
    assert [link["executor"] for link in review_config] == ["codex", "copilot", "claude"]

    # implementation had no evidence at all: withheld, but still on record.
    assert store.latest_configuration("implementation") is None
    assert store.history("implementation")[-1].recommendation is None


def test_run_quarterly_self_assessment_advances_the_cadence_gate(
    store: SelfAssessmentStore,
):
    first = run_quarterly_self_assessment(
        store, outcomes=[], routing_matrix=ROUTING_MATRIX, now=date(2026, 8, 1)
    )
    assert first["due"] is True

    # Same quarter, later date: no longer due because the store now has a
    # last-assessed timestamp inside this quarter.
    second = run_quarterly_self_assessment(
        store, outcomes=[], routing_matrix=ROUTING_MATRIX, now=date(2026, 9, 30)
    )
    assert second["due"] is False

    # Next quarter: due again.
    third = run_quarterly_self_assessment(
        store, outcomes=[], routing_matrix=ROUTING_MATRIX, now=date(2026, 10, 1)
    )
    assert third["due"] is True
    assert third["quarter"] == Quarter(2026, 4)
