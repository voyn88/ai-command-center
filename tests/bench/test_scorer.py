"""Tests for command_center.bench.scorer."""

from __future__ import annotations

import pytest

from command_center.bench.cases import CASES_BY_ID
from command_center.bench.scorer import (
    MIN_RUNS_FOR_STABLE,
    STABILITY_ALPHA,
    composite_score,
    ewma,
    rank_agents,
    score_categories,
    wilson_lower_bound,
)
from command_center.bench.types import CaseResult

CRITICAL_CASE = "critical-001"  # severity 5
SECURITY_CASE = "security-001"  # severity 5
CODE_CASE = "code-001"  # severity 3
UX_CASE = "ux-001"  # severity 3


def _result(case_id: str, agent_id: str = "agent-a", *, passed: bool, score: float) -> CaseResult:
    return CaseResult(case_id=case_id, agent_id=agent_id, passed=passed, score=score, evidence="e")


# ---------------------------------------------------------------------------
# wilson_lower_bound
# ---------------------------------------------------------------------------


def test_wilson_lower_bound_zero_samples():
    assert wilson_lower_bound(0, 0) == 0.0


def test_wilson_lower_bound_all_passed_is_high_but_not_100():
    bound = wilson_lower_bound(10, 10)
    assert 60.0 < bound < 100.0


def test_wilson_lower_bound_all_failed_is_zero():
    assert wilson_lower_bound(0, 10) == 0.0


def test_wilson_lower_bound_more_samples_is_more_confident():
    small = wilson_lower_bound(1, 1)
    large = wilson_lower_bound(10, 10)
    assert large > small


def test_wilson_lower_bound_within_bounds():
    for successes, n in [(0, 1), (1, 1), (3, 7), (50, 50)]:
        bound = wilson_lower_bound(successes, n)
        assert 0.0 <= bound <= 100.0


# ---------------------------------------------------------------------------
# score_categories / composite_score
# ---------------------------------------------------------------------------


def test_score_categories_groups_and_weights_by_severity():
    results = [
        _result(CRITICAL_CASE, passed=True, score=1.0),
        _result(CODE_CASE, passed=False, score=0.0),
    ]
    categories = {c.category: c for c in score_categories(results)}
    assert categories["critical"].weighted_rate == 100.0
    assert categories["critical"].cases_passed == 1
    assert categories["code"].weighted_rate == 0.0
    assert categories["code"].cases_passed == 0


def test_score_categories_partial_credit_within_category():
    case = CASES_BY_ID[CODE_CASE]
    assert case.severity == 3
    results = [_result(CODE_CASE, passed=False, score=0.5)]
    (category,) = score_categories(results)
    assert category.weighted_rate == pytest.approx(50.0)


def test_score_categories_unknown_case_id_raises():
    bad = CaseResult(case_id="nope", agent_id="a", passed=True, score=1.0, evidence="e")
    with pytest.raises(ValueError):
        score_categories([bad])


def test_composite_score_weights_critical_and_security_more():
    results_strong_critical = [
        _result(CRITICAL_CASE, passed=True, score=1.0),
        _result(UX_CASE, passed=False, score=0.0),
    ]
    results_strong_ux = [
        _result(CRITICAL_CASE, passed=False, score=0.0),
        _result(UX_CASE, passed=True, score=1.0),
    ]
    strong_critical = composite_score(score_categories(results_strong_critical))
    strong_ux = composite_score(score_categories(results_strong_ux))
    # critical is weighted 1.5x, ux 1.0x, so acing critical while failing ux
    # should outscore acing ux while failing critical.
    assert strong_critical > strong_ux


def test_composite_score_empty_is_zero():
    assert composite_score(()) == 0.0


# ---------------------------------------------------------------------------
# ewma
# ---------------------------------------------------------------------------


def test_ewma_no_prior_returns_current():
    assert ewma(None, 42.0) == 42.0


def test_ewma_blends_toward_current():
    blended = ewma(80.0, 20.0)
    assert 20.0 < blended < 80.0
    assert blended == pytest.approx(STABILITY_ALPHA * 20.0 + (1 - STABILITY_ALPHA) * 80.0)


def test_ewma_single_bad_week_cannot_fully_erase_a_strong_history():
    # A strong agent (stable 90) has one catastrophic week (raw 0).
    stable = ewma(90.0, 0.0)
    assert stable > 40.0  # dented hard, but not wiped out by one week


# ---------------------------------------------------------------------------
# rank_agents
# ---------------------------------------------------------------------------


def test_rank_agents_orders_by_stable_score_descending():
    results = {
        "weak": [_result(CRITICAL_CASE, "weak", passed=False, score=0.0)],
        "strong": [_result(CRITICAL_CASE, "strong", passed=True, score=1.0)],
    }
    ranked = rank_agents(results)
    assert [a.agent_id for a in ranked] == ["strong", "weak"]


def test_rank_agents_ties_break_on_agent_id():
    results = {
        "b": [_result(CRITICAL_CASE, "b", passed=True, score=1.0)],
        "a": [_result(CRITICAL_CASE, "a", passed=True, score=1.0)],
    }
    ranked = rank_agents(results)
    assert [a.agent_id for a in ranked] == ["a", "b"]


def test_rank_agents_first_run_is_provisional_and_equals_raw():
    results = {"agent-a": [_result(CRITICAL_CASE, passed=True, score=1.0)]}
    (ranked,) = rank_agents(results)
    assert ranked.provisional is True
    assert ranked.stable_score == ranked.raw_score


def test_rank_agents_matures_out_of_provisional_after_enough_runs():
    results = {"agent-a": [_result(CRITICAL_CASE, passed=True, score=1.0)]}
    ranked = rank_agents(
        results,
        previous_stable_scores={"agent-a": 90.0},
        previous_run_counts={"agent-a": MIN_RUNS_FOR_STABLE - 1},
    )
    assert ranked[0].provisional is False


def test_rank_agents_still_provisional_below_threshold():
    results = {"agent-a": [_result(CRITICAL_CASE, passed=True, score=1.0)]}
    ranked = rank_agents(
        results,
        previous_stable_scores={"agent-a": 90.0},
        previous_run_counts={"agent-a": MIN_RUNS_FOR_STABLE - 2},
    )
    assert ranked[0].provisional is True


def test_rank_agents_smoothing_cushions_a_single_bad_week():
    # "reliable" has a long strong history (stable 95) but bombs this week
    # (raw 0). "mediocre" has a long merely-average history (stable 50) and
    # turns in exactly its average week again. Raw-score-only ranking would
    # put mediocre above reliable this week; the stable scale should not.
    results = {
        "reliable": [_result(CRITICAL_CASE, "reliable", passed=False, score=0.0)],
        "mediocre": [_result(CRITICAL_CASE, "mediocre", passed=False, score=0.5)],
    }
    ranked = rank_agents(
        results,
        previous_stable_scores={"reliable": 95.0, "mediocre": 50.0},
        previous_run_counts={"reliable": 10, "mediocre": 10},
    )
    by_id = {a.agent_id: a for a in ranked}
    assert by_id["reliable"].raw_score < by_id["mediocre"].raw_score
    assert by_id["reliable"].stable_score > by_id["mediocre"].stable_score
    # the smoothing pulled reliable's stable score well above its own raw
    # score for the bad week -- one week did not erase its track record.
    assert by_id["reliable"].stable_score > by_id["reliable"].raw_score + 30


def test_rank_agents_reasons_are_nonempty_strings():
    results = {"agent-a": [_result(CRITICAL_CASE, passed=True, score=1.0)]}
    (ranked,) = rank_agents(results)
    assert ranked.reasons
    assert all(isinstance(r, str) and r for r in ranked.reasons)
