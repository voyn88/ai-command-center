"""Unit tests for :class:`command_center.advisor.scorer.ProposalScorer`."""

from __future__ import annotations

from command_center.advisor.scorer import ProposalScorer
from command_center.advisor.types import Candidate


def _cand(**signals) -> Candidate:
    return Candidate(kind="optimization", title="t", project_ref="AICC", signals=signals)


def test_missing_signals_fall_back_to_neutral_midpoints() -> None:
    score = ProposalScorer().score(Candidate(kind="ux", title="t", project_ref="AICC"))
    assert 0.0 <= score.value <= 1.0
    assert score.effort == 0.5
    assert score.risk == 0.3


def test_high_value_low_effort_low_risk_scores_high_priority() -> None:
    scorer = ProposalScorer()
    strong = scorer.score(_cand(impact=1.0, frequency=1.0, effort=0.0, risk=0.0))
    weak = scorer.score(_cand(impact=0.1, frequency=0.1, effort=1.0, risk=1.0))
    assert strong.priority > weak.priority
    assert strong.value_bucket == "high"
    assert strong.priority == 1.0


def test_effort_and_risk_discount_priority() -> None:
    scorer = ProposalScorer()
    base = scorer.score(_cand(impact=1.0, frequency=1.0, effort=0.0, risk=0.0))
    costly = scorer.score(_cand(impact=1.0, frequency=1.0, effort=1.0, risk=0.0))
    risky = scorer.score(_cand(impact=1.0, frequency=1.0, effort=0.0, risk=1.0))
    assert costly.priority < base.priority
    assert risky.priority < base.priority


def test_signals_are_clamped_into_unit_range() -> None:
    score = ProposalScorer().score(_cand(impact=5.0, frequency=-3.0, effort=9.0, risk=-1.0))
    assert score.value == 0.6  # impact clamps to 1.0, frequency to 0.0 -> 0.6*1
    assert score.effort == 1.0
    assert score.risk == 0.0


def test_buckets_partition_the_range() -> None:
    scorer = ProposalScorer()
    assert scorer.score(_cand(impact=0.0, frequency=0.0)).value_bucket == "low"
    assert scorer.score(_cand(impact=0.5, frequency=0.5)).value_bucket == "medium"
    assert scorer.score(_cand(impact=1.0, frequency=1.0)).value_bucket == "high"
