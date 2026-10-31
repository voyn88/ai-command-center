"""Unit tests for :class:`command_center.arena.scorer.DuelScorer`."""

from __future__ import annotations

import pytest

from command_center.arena.scorer import DuelScorer, TooFewVariantsError
from command_center.arena.types import SolutionVariant


def _variant(agent_id: str, **overrides) -> SolutionVariant:
    defaults = dict(
        output="42",
        rationale="because",
        correct=True,
        quality=0.5,
        explainability=0.5,
        duration_seconds=10.0,
        cost_usd=0.01,
    )
    defaults.update(overrides)
    return SolutionVariant(agent_id=agent_id, **defaults)


def test_fewer_than_three_variants_is_rejected() -> None:
    scorer = DuelScorer()
    with pytest.raises(TooFewVariantsError):
        scorer.score_all([_variant("a"), _variant("b")])


def test_three_variants_is_the_minimum_allowed() -> None:
    scorer = DuelScorer()
    scores = scorer.score_all([_variant("a"), _variant("b"), _variant("c")])
    assert len(scores) == 3


def test_correctness_dominates_a_faster_wrong_answer() -> None:
    scorer = DuelScorer()
    correct = _variant(
        "slow-but-right", correct=True, duration_seconds=100.0, cost_usd=1.0
    )
    wrong = _variant(
        "fast-but-wrong", correct=False, duration_seconds=1.0, cost_usd=0.001
    )
    third = _variant("baseline")
    scores = {s.agent_id: s for s in scorer.score_all([correct, wrong, third])}
    assert scores["slow-but-right"].composite > scores["fast-but-wrong"].composite


def test_time_and_cost_are_normalized_relative_to_the_field() -> None:
    scorer = DuelScorer()
    fastest = _variant("fastest", duration_seconds=1.0, cost_usd=0.001)
    middle = _variant("middle", duration_seconds=5.0, cost_usd=0.005)
    slowest = _variant("slowest", duration_seconds=10.0, cost_usd=0.01)
    scores = {
        s.agent_id: s for s in scorer.score_all([fastest, middle, slowest])
    }
    assert scores["fastest"].time_score == 1.0
    assert scores["slowest"].time_score == 0.0
    assert scores["fastest"].cost_score == 1.0
    assert scores["slowest"].cost_score == 0.0
    assert scores["fastest"].time_score > scores["middle"].time_score > scores[
        "slowest"
    ].time_score


def test_tied_time_or_cost_penalizes_nobody() -> None:
    scorer = DuelScorer()
    variants = [_variant(f"v{i}", duration_seconds=5.0, cost_usd=0.02) for i in range(3)]
    scores = scorer.score_all(variants)
    assert all(s.time_score == 1.0 for s in scores)
    assert all(s.cost_score == 1.0 for s in scores)


def test_quality_and_explainability_are_clamped_into_unit_range() -> None:
    scorer = DuelScorer()
    variants = [
        _variant("over", quality=5.0, explainability=-3.0),
        _variant("mid"),
        _variant("under"),
    ]
    scores = {s.agent_id: s for s in scorer.score_all(variants)}
    assert scores["over"].quality == 1.0
    assert scores["over"].explainability == 0.0


def test_weights_must_sum_positive() -> None:
    with pytest.raises(ValueError):
        DuelScorer(
            correctness_weight=0.0,
            quality_weight=0.0,
            explainability_weight=0.0,
            time_weight=0.0,
            cost_weight=0.0,
        )
