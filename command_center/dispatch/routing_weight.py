"""Turns a rating + cost into a routing weight per executor
(VOYN-W0-AGENT-MARKETPLACE), and enforces a diversity floor so a strong
incumbent can never capture the whole sample.

`dispatch.rating.compute_ratings` measures competence; `dispatch.policy`
selects the cheapest eligible executor within budget and does not read a
rating at all. Nothing in the codebase yet turns "measured pricing" (score +
cost) into the "routing по измеренной пригодности и цене" the owning idea
calls for — this module is that arithmetic, kept pure and separate from
`plan_dispatch` for the same reason `rating.py` and `task_class.py` were: it
is real, testable logic that a live selection decision has no real ledger
feed to consume yet, so wiring it into `plan_dispatch` now would be the
decorative "витрина" the idea explicitly warns against. Once a real ledger
feed exists, a caller multiplies `plan_dispatch`'s eligible-executor set
through `routing_weights` instead of picking the single cheapest one.

Two rules the idea's own stated risks require, both enforced structurally:

1. **An unconfident rating is treated as neutral, not as bad.** A rating
   below `rating.DEFAULT_SIGNIFICANCE_THRESHOLD` is exactly as informative as
   no rating at all (`usable_score` already returns `None` for both) — this
   module scores that case as `NEUTRAL_SCORE`, the same weight every executor
   starts at, rather than as a low score. Scoring it low would mean a new or
   rarely-tried executor never accumulates the samples significance
   requires, because the market would starve it of the very attempts that
   could make its rating confident.
2. **No eligible executor's weight can fall below `diversity_floor`,
   regardless of how dominant another executor's score is.** This is risk #2
   from the owning idea, stated directly: "специализация снижает живучесть
   ... нужен явный минимум разнообразия в маршрутизации, иначе система
   схлопнется в одного «чемпиона»." A floor of `0.1` with two eligible
   executors means the weaker one still gets routed roughly one attempt in
   ten even when the stronger one is cheaper and rated perfectly — enough to
   keep its own rating alive and to notice if the market shifts under the
   incumbent.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from command_center.dispatch.rating import AgentRating, usable_score

#: The weight an executor with no significant rating for this task class
#: gets, instead of a real (and possibly noisy, possibly zero) score. Chosen
#: as the midpoint of the `[0, 1]` score range so an unproven executor starts
#: neither favoured nor penalised against one with a proven track record.
NEUTRAL_SCORE = 0.5

#: Minimum share of the routing weight guaranteed to every eligible executor
#: when two or more are eligible, no matter how it scores. See module
#: docstring rule 2.
DEFAULT_DIVERSITY_FLOOR = 0.1

#: A cost of zero or less is not a real price (free or malformed input); this
#: is the floor used in its place so a single free/malformed entry cannot
#: divide by zero or, worse, receive infinite weight.
_MIN_COST_USD = 0.01


def routing_weights(
    *,
    task_class: str,
    eligible_executor_ids: Sequence[str],
    ratings: Mapping[tuple[str, str], AgentRating],
    cost_by_executor: Mapping[str, float],
    diversity_floor: float = DEFAULT_DIVERSITY_FLOOR,
) -> dict[str, float]:
    """A probability-like weight per eligible executor for one task class.

    Total and pure: never raises on well-typed input, and the returned
    weights always sum to `1.0` (up to floating-point rounding) whenever
    `eligible_executor_ids` is non-empty. Duplicate ids are deduplicated,
    keeping the first occurrence's position.

    Higher `usable_score` and lower cost both raise an executor's share, but
    never below `diversity_floor` while at least two executors are eligible —
    see the module docstring for why both directions matter.
    """
    ids = list(dict.fromkeys(eligible_executor_ids))
    if not ids:
        return {}
    if len(ids) == 1:
        return {ids[0]: 1.0}

    floor = min(max(diversity_floor, 0.0), 1.0)
    if floor * len(ids) >= 1.0:
        # The requested floor cannot fit every executor at once; the only
        # allocation consistent with "everyone gets at least the floor" is
        # uniform.
        share = 1.0 / len(ids)
        return {executor_id: share for executor_id in ids}

    raw_weight: dict[str, float] = {}
    for executor_id in ids:
        rating = ratings.get((executor_id, task_class))
        score = usable_score(rating)
        if score is None:
            score = NEUTRAL_SCORE
        cost = cost_by_executor.get(executor_id)
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or cost <= 0:
            cost = _MIN_COST_USD
        raw_weight[executor_id] = score / cost

    total_raw = sum(raw_weight.values())
    remaining_share = 1.0 - floor * len(ids)
    weights: dict[str, float] = {}
    for executor_id in ids:
        proportional = (
            raw_weight[executor_id] / total_raw if total_raw > 0 else 1.0 / len(ids)
        )
        weights[executor_id] = floor + remaining_share * proportional
    return weights
