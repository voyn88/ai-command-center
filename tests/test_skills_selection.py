"""Unit tests for the measurable candidate-selection logic
(``command_center.skills.selection``) -- the "не «по названию»" acceptance
criterion. Pure functions, no db, no service.
"""

from __future__ import annotations

from command_center.skills.finder import CandidateMetrics, CandidateProposal
from command_center.skills.selection import select_candidate

_HASH_A = "a" * 64
_HASH_B = "b" * 64
_HASH_C = "c" * 64


def _candidate(name: str, content_hash: str, metrics: CandidateMetrics | None) -> CandidateProposal:
    return CandidateProposal(
        name=name, kind="mcp_server", version="1.0.0", content_hash=content_hash,
        source_id="src-1", metrics=metrics,
    )


def test_refuses_to_choose_with_no_measurable_evidence_at_all() -> None:
    candidates = [
        _candidate("z-alphabetically-last", _HASH_A, None),
        _candidate("a-alphabetically-first", _HASH_B, None),
    ]
    result = select_candidate(candidates)
    assert result.winner is None
    assert result.rationale["method"] == "measurable-history-required"
    assert result.rationale["candidates_considered"] == 2


def test_picks_the_scored_candidate_over_an_unscored_one_regardless_of_name() -> None:
    scored = _candidate(
        "z-worse-name", _HASH_A,
        CandidateMetrics(success_rate=0.9, avg_cost=1.0, avg_latency_seconds=1.0),
    )
    unscored = _candidate("a-better-name", _HASH_B, None)
    result = select_candidate([unscored, scored])
    assert result.winner is scored
    assert result.rationale["unscored_excluded"] == ["a-better-name"]


def test_picks_by_weighted_success_cost_latency_not_order() -> None:
    worse = _candidate(
        "listed-first", _HASH_A,
        CandidateMetrics(success_rate=0.5, avg_cost=10.0, avg_latency_seconds=10.0),
    )
    better = _candidate(
        "listed-second", _HASH_B,
        CandidateMetrics(success_rate=0.95, avg_cost=1.0, avg_latency_seconds=1.0),
    )
    result = select_candidate([worse, better])
    assert result.winner is better
    assert result.rationale["method"] == "weighted-historical-score"
    assert result.rationale["winner_content_hash"] == _HASH_B
    scores = {row["content_hash"]: row["score"] for row in result.rationale["scored"]}
    assert scores[_HASH_B] > scores[_HASH_A]


def test_tie_break_is_deterministic_by_content_hash_not_name_or_order() -> None:
    identical_metrics = CandidateMetrics(success_rate=0.8, avg_cost=1.0, avg_latency_seconds=1.0)
    first = _candidate("aaa-name", _HASH_C, identical_metrics)
    second = _candidate("zzz-name", _HASH_A, identical_metrics)
    result = select_candidate([first, second])
    # Lower content_hash wins the tie, independent of name or list order.
    assert result.winner.content_hash == _HASH_A

    # Reversed input order produces the identical winner.
    result_reordered = select_candidate([second, first])
    assert result_reordered.winner.content_hash == _HASH_A


def test_constant_metrics_across_pool_do_not_bias_normalization() -> None:
    same = CandidateMetrics(success_rate=0.7, avg_cost=5.0, avg_latency_seconds=5.0)
    a = _candidate("a", _HASH_A, same)
    b = _candidate("b", _HASH_B, same)
    result = select_candidate([a, b])
    scores = {row["content_hash"]: row["score"] for row in result.rationale["scored"]}
    assert scores[_HASH_A] == scores[_HASH_B]


def test_higher_success_rate_can_be_outweighed_by_much_worse_cost_and_latency() -> None:
    cheap_reliable_enough = _candidate(
        "cheap", _HASH_A,
        CandidateMetrics(success_rate=0.85, avg_cost=1.0, avg_latency_seconds=1.0),
    )
    slightly_better_but_expensive = _candidate(
        "expensive", _HASH_B,
        CandidateMetrics(success_rate=0.90, avg_cost=100.0, avg_latency_seconds=100.0),
    )
    result = select_candidate([cheap_reliable_enough, slightly_better_but_expensive])
    assert result.winner.content_hash == _HASH_A
