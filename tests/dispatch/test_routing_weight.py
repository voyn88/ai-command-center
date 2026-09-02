"""Unit tests for the pure routing-weight engine (`dispatch.routing_weight`).

Every property is asserted directly against `routing_weights` with plain
data — no database, no filesystem, matching the rest of `dispatch`.
"""

from __future__ import annotations

import pytest

from command_center.dispatch.rating import AgentRating
from command_center.dispatch.routing_weight import (
    DEFAULT_DIVERSITY_FLOOR,
    NEUTRAL_SCORE,
    routing_weights,
)

TASK_CLASS = "AICC:migration"


def _rating(
    executor: str,
    *,
    score: float,
    confident: bool = True,
    attempted_count: int = 10,
    accepted_count: int = 5,
) -> AgentRating:
    return AgentRating(
        executor_id=executor,
        task_class=TASK_CLASS,
        attempted_count=attempted_count,
        accepted_count=accepted_count,
        score=score,
        avg_cost_usd=1.0,
        confident=confident,
    )


def _weights(ratings, costs, *, ids=("claude", "codex"), floor=DEFAULT_DIVERSITY_FLOOR):
    return routing_weights(
        task_class=TASK_CLASS,
        eligible_executor_ids=list(ids),
        ratings=ratings,
        cost_by_executor=costs,
        diversity_floor=floor,
    )


# --------------------------------------------------------------------------
# Trivial shapes
# --------------------------------------------------------------------------


def test_no_eligible_executors_yields_no_weights():
    assert routing_weights(
        task_class=TASK_CLASS,
        eligible_executor_ids=[],
        ratings={},
        cost_by_executor={},
    ) == {}


def test_single_eligible_executor_always_gets_the_whole_weight():
    weights = _weights({}, {"claude": 5.0}, ids=("claude",))
    assert weights == {"claude": 1.0}


def test_duplicate_ids_are_deduplicated():
    weights = _weights({}, {"claude": 1.0}, ids=("claude", "claude"))
    assert weights == {"claude": 1.0}


def test_weights_always_sum_to_one():
    ratings = {("claude", TASK_CLASS): _rating("claude", score=0.95)}
    costs = {"claude": 0.2, "codex": 4.0}
    weights = _weights(ratings, costs)
    assert weights["claude"] + weights["codex"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Unconfident ratings are neutral, not bad (rule 1)
# --------------------------------------------------------------------------


def test_no_rating_at_all_is_neutral_on_both_sides():
    weights = _weights({}, {"claude": 1.0, "codex": 1.0})
    assert weights["claude"] == pytest.approx(weights["codex"])


def test_unconfident_rating_is_not_scored_as_its_raw_noisy_value():
    # One accepted out of one attempt is a perfect score by arithmetic, but
    # far below the significance threshold -- it must be treated exactly
    # like "no rating at all", not as proof of excellence.
    unconfident = {
        ("claude", TASK_CLASS): _rating(
            "claude", score=1.0, confident=False, attempted_count=1, accepted_count=1
        )
    }
    weights_unconfident = _weights(unconfident, {"claude": 1.0, "codex": 1.0})
    weights_no_data = _weights({}, {"claude": 1.0, "codex": 1.0})
    assert weights_unconfident == pytest.approx(weights_no_data)


def test_neutral_score_constant_is_the_midpoint():
    assert NEUTRAL_SCORE == 0.5


# --------------------------------------------------------------------------
# Confident ratings and cost both move the weight (the "market" arithmetic)
# --------------------------------------------------------------------------


def test_higher_confident_score_wins_more_weight_at_equal_cost():
    ratings = {
        ("claude", TASK_CLASS): _rating("claude", score=0.9),
        ("codex", TASK_CLASS): _rating("codex", score=0.3),
    }
    weights = _weights(ratings, {"claude": 1.0, "codex": 1.0})
    assert weights["claude"] > weights["codex"]


def test_cheaper_executor_wins_more_weight_at_equal_score():
    ratings = {
        ("claude", TASK_CLASS): _rating("claude", score=0.5),
        ("codex", TASK_CLASS): _rating("codex", score=0.5),
    }
    weights = _weights(ratings, {"claude": 1.0, "codex": 10.0})
    assert weights["claude"] > weights["codex"]


def test_non_positive_or_missing_cost_does_not_crash_or_explode():
    ratings = {("claude", TASK_CLASS): _rating("claude", score=0.5)}
    for bad_cost in (0.0, -1.0):
        weights = _weights(ratings, {"claude": bad_cost, "codex": 1.0})
        assert weights["claude"] + weights["codex"] == pytest.approx(1.0)
        assert all(w >= 0.0 for w in weights.values())
    # codex missing from cost_by_executor entirely
    weights = _weights(ratings, {"claude": 1.0})
    assert weights["claude"] + weights["codex"] == pytest.approx(1.0)


# --------------------------------------------------------------------------
# Diversity floor (rule 2 / the idea's risk #2)
# --------------------------------------------------------------------------


def test_dominant_incumbent_never_drops_the_weaker_executor_below_the_floor():
    # claude is cheap and rated perfectly; codex is expensive and rated
    # terribly. Without a floor codex's share would collapse toward zero.
    ratings = {
        ("claude", TASK_CLASS): _rating("claude", score=1.0),
        ("codex", TASK_CLASS): _rating("codex", score=0.01),
    }
    weights = _weights(ratings, {"claude": 0.01, "codex": 100.0})
    assert weights["codex"] == pytest.approx(DEFAULT_DIVERSITY_FLOOR, abs=1e-3)
    assert weights["claude"] > weights["codex"]


def test_floor_of_zero_lets_a_dominant_incumbent_approach_the_whole_weight():
    ratings = {
        ("claude", TASK_CLASS): _rating("claude", score=1.0),
        ("codex", TASK_CLASS): _rating("codex", score=0.01),
    }
    weights = _weights(ratings, {"claude": 0.01, "codex": 100.0}, floor=0.0)
    assert weights["codex"] < DEFAULT_DIVERSITY_FLOOR
    assert weights["claude"] > 0.9


def test_floor_too_large_to_fit_every_executor_falls_back_to_uniform():
    ratings = {("claude", TASK_CLASS): _rating("claude", score=1.0)}
    weights = _weights(
        ratings,
        {"claude": 0.01, "codex": 100.0},
        ids=("claude", "codex", "copilot"),
        floor=0.5,
    )
    assert weights == {"claude": pytest.approx(1 / 3), "codex": pytest.approx(1 / 3), "copilot": pytest.approx(1 / 3)}


def test_negative_floor_is_clamped_to_zero():
    weights = _weights({}, {"claude": 1.0, "codex": 1.0}, floor=-1.0)
    assert weights["claude"] == pytest.approx(weights["codex"])
    assert weights["claude"] + weights["codex"] == pytest.approx(1.0)
