"""Unit tests for :class:`command_center.arena.service.DuelService`."""

from __future__ import annotations

import pytest

from command_center.arena.scorer import TooFewVariantsError
from command_center.arena.service import DuelService
from command_center.arena.types import Case, SolutionVariant


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


def _case() -> Case:
    return Case(id="case-1", prompt="reverse a linked list")


def test_run_rejects_fewer_than_three_variants() -> None:
    service = DuelService()
    with pytest.raises(TooFewVariantsError):
        service.run(_case(), [_variant("a"), _variant("b")])


def test_run_ranks_best_variant_first_and_declares_winner() -> None:
    service = DuelService()
    best = _variant("best", quality=0.9, explainability=0.9, duration_seconds=1.0, cost_usd=0.001)
    middle = _variant("middle", quality=0.5, explainability=0.5)
    worst = _variant("worst", correct=False, quality=0.1, explainability=0.1)
    result = service.run(_case(), [worst, middle, best])

    assert result.winner_agent_id == "best"
    assert [r.variant.agent_id for r in result.ranking] == ["best", "middle", "worst"]
    assert [r.rank for r in result.ranking] == [1, 2, 3]


def test_ranking_scores_are_strictly_descending() -> None:
    service = DuelService()
    variants = [
        _variant("a", quality=0.9, duration_seconds=1.0, cost_usd=0.001),
        _variant("b", quality=0.5, duration_seconds=5.0, cost_usd=0.005),
        _variant("c", quality=0.1, duration_seconds=10.0, cost_usd=0.01),
    ]
    result = service.run(_case(), variants)
    composites = [r.score.composite for r in result.ranking]
    assert composites == sorted(composites, reverse=True)


def test_case_is_preserved_on_the_result() -> None:
    service = DuelService()
    case = _case()
    result = service.run(case, [_variant("a"), _variant("b"), _variant("c")])
    assert result.case is case


def test_per_variant_rationale_reports_the_verdict() -> None:
    service = DuelService()
    correct = _variant("right", correct=True)
    wrong = _variant("wrong", correct=False)
    third = _variant("third")
    result = service.run(_case(), [correct, wrong, third])

    by_agent = {r.variant.agent_id: r for r in result.ranking}
    assert "correct" in by_agent["right"].rationale
    assert "incorrect" in by_agent["wrong"].rationale


def test_overall_rationale_flags_a_winner_that_failed_correctness() -> None:
    service = DuelService()
    # All three fail correctness, so whichever wins on the other axes still
    # did not pass — the rationale must not hide that behind a good score.
    variants = [
        _variant("a", correct=False, quality=0.9, explainability=0.9),
        _variant("b", correct=False, quality=0.2, explainability=0.2),
        _variant("c", correct=False, quality=0.1, explainability=0.1),
    ]
    result = service.run(_case(), variants)
    assert "did not pass correctness" in result.rationale


def test_overall_rationale_reports_margin_over_runner_up() -> None:
    service = DuelService()
    result = service.run(
        _case(),
        [
            _variant("a", quality=0.9, explainability=0.9),
            _variant("b", quality=0.3, explainability=0.3),
            _variant("c", quality=0.1, explainability=0.1),
        ],
    )
    assert "ahead of" in result.rationale
    assert "by" in result.rationale
