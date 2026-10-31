"""Scoring and ranking for the agent hard-bench-set (VOYN-AGT-HARD-BENCH).

Every number produced here is explainable: `RankedAgent.reasons` states in
words what produced `stable_score`, matching this repo's scoring convention
(`advisor/scorer.py`, `recommend.py`) of never handing back an opaque number.

The acceptance criterion asks for a "stable scale" — a weekly report that
does not reorder agents on the back of one noisy run. Two mechanisms give
that:

* `wilson_lower_bound` — a 95%-confidence lower bound on a category's pass
  rate, so a 1/1 pass reads as less certain than a 10/10 pass at the same raw
  rate, instead of the two looking identical.
* `ewma` — this week's composite score is blended with the prior stable
  score (see `rank_agents`), so one bad week dents the score without being
  able to flip the ranking outright; it takes a sustained trend.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence

from command_center.bench.cases import CASES_BY_ID
from command_center.bench.types import BenchCase, CaseResult, CategoryScore, RankedAgent

#: Critical and security cases weigh more: a failure there is not
#: equivalent to a UX nit, even before per-case `severity` is applied.
CATEGORY_WEIGHTS: dict[str, float] = {
    "critical": 1.5,
    "incident": 1.0,
    "code": 1.0,
    "ux": 1.0,
    "security": 1.5,
}

#: EWMA smoothing factor for the stable score. 0.4 means this week's raw
#: score moves the stable score 40% of the remaining distance — three
#: consecutive weeks moving the same direction are needed to mostly close
#: the gap; one outlier week cannot.
STABILITY_ALPHA = 0.4

#: An agent's rank is flagged `provisional` until it has this many weekly
#: runs behind it.
MIN_RUNS_FOR_STABLE = 3

#: z for a 95% Wilson score interval.
_Z = 1.959963984540054


def wilson_lower_bound(successes: int, n: int, *, z: float = _Z) -> float:
    """95%-confidence lower bound on a pass rate, as a percentage (0..100).

    Deterministic — `z` is a fixed constant, not sampled.
    """
    if n <= 0:
        return 0.0
    phat = successes / n
    denom = 1 + z * z / n
    center = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return max(0.0, min(1.0, (center - margin) / denom)) * 100.0


def _category_score(
    category: str, results: Sequence[CaseResult], cases_by_id: Mapping[str, BenchCase]
) -> CategoryScore:
    total_weight = 0.0
    weighted_sum = 0.0
    passed = 0
    for result in results:
        case = cases_by_id[result.case_id]
        total_weight += case.severity
        weighted_sum += case.severity * result.score
        if result.passed:
            passed += 1
    weighted_rate = (weighted_sum / total_weight * 100.0) if total_weight else 0.0
    return CategoryScore(
        category=category,
        weighted_rate=weighted_rate,
        cases_run=len(results),
        cases_passed=passed,
        lower_bound=wilson_lower_bound(passed, len(results)),
    )


def score_categories(
    results: Sequence[CaseResult],
    *,
    cases_by_id: Mapping[str, BenchCase] = CASES_BY_ID,
) -> tuple[CategoryScore, ...]:
    """Group one agent's case results by category and score each category.

    `cases_by_id` defaults to the real, fixed hard-bench-set; tests may pass
    a smaller stand-in set. Raises if a result names a case id outside it —
    a grader bug should surface immediately, not silently drop the result.
    """
    by_category: dict[str, list[CaseResult]] = defaultdict(list)
    for result in results:
        case = cases_by_id.get(result.case_id)
        if case is None:
            raise ValueError(f"unknown bench case id {result.case_id!r}")
        by_category[case.category].append(result)
    return tuple(
        _category_score(category, by_category[category], cases_by_id)
        for category in sorted(by_category)
    )


def composite_score(categories: Sequence[CategoryScore]) -> float:
    """Weighted mean of category rates, renormalized over categories present.

    A partial run (not every category attempted) still produces a comparable
    0..100 number instead of being dragged toward zero by categories nobody
    scored this week.
    """
    total_weight = 0.0
    weighted_sum = 0.0
    for cat in categories:
        weight = CATEGORY_WEIGHTS.get(cat.category, 1.0)
        total_weight += weight
        weighted_sum += weight * cat.weighted_rate
    return (weighted_sum / total_weight) if total_weight else 0.0


def ewma(previous: float | None, current: float, *, alpha: float = STABILITY_ALPHA) -> float:
    if previous is None:
        return current
    return alpha * current + (1 - alpha) * previous


def rank_agents(
    results_by_agent: Mapping[str, Sequence[CaseResult]],
    *,
    previous_stable_scores: Mapping[str, float] | None = None,
    previous_run_counts: Mapping[str, int] | None = None,
    cases_by_id: Mapping[str, BenchCase] = CASES_BY_ID,
) -> list[RankedAgent]:
    """Rank agents for one weekly pass.

    `previous_stable_scores`/`previous_run_counts` come from persisted
    history (`db.bench_store`); an agent seen for the first time gets
    `stable_score == raw_score` and is marked `provisional`. Ties on
    `stable_score` break on `agent_id` so the ordering is deterministic.
    """
    previous_stable_scores = previous_stable_scores or {}
    previous_run_counts = previous_run_counts or {}

    ranked: list[RankedAgent] = []
    for agent_id in sorted(results_by_agent):
        results = results_by_agent[agent_id]
        categories = score_categories(results, cases_by_id=cases_by_id)
        raw = composite_score(categories)
        previous = previous_stable_scores.get(agent_id)
        stable = ewma(previous, raw)
        run_count = previous_run_counts.get(agent_id, 0) + 1
        provisional = run_count < MIN_RUNS_FOR_STABLE

        reasons = [
            f"raw score this week: {raw:.1f}/100 across {len(results)} case(s) "
            f"in {len(categories)} categor{'y' if len(categories) == 1 else 'ies'}",
        ]
        if previous is None:
            reasons.append(
                "no prior week for this agent: stable score equals this week's raw score"
            )
        else:
            reasons.append(
                f"stable score blends this week ({STABILITY_ALPHA:.0%} weight) with "
                f"the prior stable score of {previous:.1f} ({1 - STABILITY_ALPHA:.0%} "
                "weight), so a single noisy week cannot flip the leaderboard on its own"
            )
        if provisional:
            reasons.append(
                f"provisional: only {run_count} weekly run(s) on record "
                f"(need {MIN_RUNS_FOR_STABLE}) — rank is indicative, not final"
            )
        worst = min(categories, key=lambda c: c.lower_bound, default=None)
        if worst is not None:
            reasons.append(
                f"weakest category: {worst.category} "
                f"({worst.cases_passed}/{worst.cases_run} passed, "
                f"95% lower bound {worst.lower_bound:.1f})"
            )

        ranked.append(
            RankedAgent(
                agent_id=agent_id,
                stable_score=stable,
                raw_score=raw,
                provisional=provisional,
                categories=categories,
                reasons=tuple(reasons),
            )
        )

    ranked.sort(key=lambda r: (-r.stable_score, r.agent_id))
    return ranked
