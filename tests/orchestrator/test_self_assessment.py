"""Continuous self-assessment and role rotation (VOYN-MIN-AGT-EVO2): scoring,
cascade rotation and the quarterly cadence gate are pure and hermetic, the
same guarantee ``test_routing.py`` holds for the static matrix this module
assesses."""

from __future__ import annotations

from datetime import date, datetime

from command_center.orchestrator.routing import ROUTING_MATRIX, cascade_for
from command_center.orchestrator.self_assessment import (
    AttemptOutcome,
    Quarter,
    is_reassessment_due,
    quarter_of,
    quarterly_self_assessment,
    recommend_cascade,
    score_executors,
)

_WHEN = datetime(2026, 8, 1, 12, 0, 0)


def _outcomes(*rows: tuple[str, str, bool]) -> list[AttemptOutcome]:
    return [
        AttemptOutcome(task_class=tc, executor=ex, succeeded=ok, occurred_at=_WHEN)
        for tc, ex, ok in rows
    ]


def test_quarter_of_buckets_by_calendar_quarter():
    assert quarter_of(date(2026, 1, 1)) == Quarter(2026, 1)
    assert quarter_of(date(2026, 3, 31)) == Quarter(2026, 1)
    assert quarter_of(date(2026, 4, 1)) == Quarter(2026, 2)
    assert quarter_of(date(2026, 12, 31)) == Quarter(2026, 4)


def test_reassessment_is_due_with_no_prior_assessment():
    assert is_reassessment_due(None, date(2026, 8, 1)) is True


def test_reassessment_is_not_due_within_the_same_quarter():
    assert is_reassessment_due(date(2026, 7, 1), date(2026, 9, 30)) is False


def test_reassessment_is_due_once_the_quarter_rolls_over():
    assert is_reassessment_due(date(2026, 6, 30), date(2026, 7, 1)) is True


def test_score_executors_ranks_by_success_rate_then_evidence_then_name():
    outcomes = _outcomes(
        ("implementation", "claude", True),
        ("implementation", "claude", False),
        ("implementation", "codex", True),
        ("implementation", "codex", True),
        ("implementation", "copilot", True),
        ("review", "claude", False),  # a different domain, must not leak in
    )
    scores = score_executors(outcomes, task_class="implementation")
    assert [s.executor for s in scores] == ["codex", "copilot", "claude"]
    assert scores[0].attempts == 2 and scores[0].successes == 2
    assert scores[0].success_rate == 1.0
    assert scores[-1].success_rate == 0.5


def test_score_executors_ignores_other_task_classes():
    outcomes = _outcomes(("review", "codex", True))
    assert score_executors(outcomes, task_class="implementation") == []


def test_recommend_cascade_withheld_below_the_sample_floor():
    current = cascade_for("implementation")
    # Every executor has exactly one observation each — far short of the
    # default floor, so the assessment must not rotate on this little evidence.
    outcomes = [
        AttemptOutcome(
            task_class="implementation", executor=link["executor"], succeeded=True,
            occurred_at=_WHEN,
        )
        for link in current
    ]
    assert recommend_cascade(outcomes, "implementation", current) is None


def test_recommend_cascade_withheld_when_a_link_executor_is_unscored():
    current = cascade_for("implementation")
    # codex and copilot get plenty of samples; claude gets none at all.
    outcomes = _outcomes(
        *[("implementation", "codex", True)] * 25,
        *[("implementation", "copilot", True)] * 25,
    )
    assert recommend_cascade(outcomes, "implementation", current) is None


def test_recommend_cascade_rotates_the_better_performer_first():
    current = cascade_for("implementation")
    executors = [link["executor"] for link in current]
    assert executors == ["claude", "codex", "copilot"]

    # codex now clearly outperforms claude and copilot, with enough samples
    # on every link to trust the ranking.
    outcomes = _outcomes(
        *[("implementation", "claude", False)] * 25,
        *[("implementation", "codex", True)] * 25,
        *[("implementation", "copilot", True), ("implementation", "copilot", False)] * 12,
    )
    recommended = recommend_cascade(outcomes, "implementation", current)
    assert recommended is not None
    assert [link["executor"] for link in recommended] == ["codex", "copilot", "claude"]
    # task_type is preserved per link, only order changes.
    assert all(link["task_type"] == "implementation" for link in recommended)


def test_recommend_cascade_does_not_mutate_its_inputs():
    current = cascade_for("implementation")
    frozen = [dict(link) for link in current]
    outcomes = _outcomes(
        *[("implementation", link["executor"], True) for link in current] * 25
    )
    recommend_cascade(outcomes, "implementation", current)
    assert current == frozen


def test_quarterly_self_assessment_reports_not_due_within_the_quarter():
    report = quarterly_self_assessment(
        outcomes=[],
        routing_matrix=ROUTING_MATRIX,
        now=date(2026, 8, 1),
        last_assessed_at=date(2026, 7, 1),
    )
    assert report == {"due": False, "quarter": None, "recommendations": {}}


def test_quarterly_self_assessment_covers_every_domain_when_due():
    report = quarterly_self_assessment(
        outcomes=[],
        routing_matrix=ROUTING_MATRIX,
        now=date(2026, 10, 1),
        last_assessed_at=date(2026, 7, 1),
    )
    assert report["due"] is True
    assert report["quarter"] == Quarter(2026, 4)
    # No evidence at all yet -> every domain withheld, but every domain present.
    assert set(report["recommendations"]) == set(ROUTING_MATRIX)
    assert all(v is None for v in report["recommendations"].values())


def test_quarterly_self_assessment_recommends_once_evidence_is_strong():
    outcomes = _outcomes(
        *[("review", "codex", True)] * 25,
        *[("review", "copilot", True)] * 25,
        *[("review", "claude", False)] * 25,
    )
    report = quarterly_self_assessment(
        outcomes=outcomes,
        routing_matrix=ROUTING_MATRIX,
        now=date(2026, 10, 1),
        last_assessed_at=None,
    )
    assert report["due"] is True
    review = report["recommendations"]["review"]
    assert review is not None
    assert [link["executor"] for link in review] == ["codex", "copilot", "claude"]
