"""Unit tests for the pure agent-rating engine (`dispatch.rating`).

Every acceptance property is asserted directly against `compute_ratings` /
`usable_score` with plain data — no database, no filesystem.
"""

from __future__ import annotations

from command_center.dispatch.rating import (
    ACCEPTED_VERDICTS,
    AgentRating,
    LedgerEntry,
    compute_ratings,
    usable_score,
)


def _entry(
    executor: str = "claude",
    task_class: str = "AICC:migration",
    *,
    merged_sha: str | None = "deadbeef",
    review_verdict: str | None = "approved",
    cost_usd: float = 1.0,
    **kwargs,
) -> LedgerEntry:
    return LedgerEntry(
        executor_id=executor,
        task_class=task_class,
        merged_sha=merged_sha,
        review_verdict=review_verdict,
        cost_usd=cost_usd,
        **kwargs,
    )


# --------------------------------------------------------------------------
# LedgerEntry.accepted
# --------------------------------------------------------------------------


def test_accepted_requires_both_merge_and_independent_verdict():
    assert _entry().accepted is True
    assert _entry(merged_sha=None).accepted is False
    assert _entry(review_verdict=None).accepted is False
    assert _entry(review_verdict="self_approved").accepted is False


def test_accepted_verdicts_are_exactly_approved_and_accepted():
    assert ACCEPTED_VERDICTS == frozenset({"approved", "accepted"})
    assert _entry(review_verdict="accepted").accepted is True
    assert _entry(review_verdict="rejected").accepted is False


# --------------------------------------------------------------------------
# compute_ratings: grouping and arithmetic
# --------------------------------------------------------------------------


def test_empty_ledger_yields_no_ratings():
    assert compute_ratings([]) == {}


def test_groups_by_executor_and_task_class_independently():
    entries = [
        _entry("claude", "AICC:migration"),
        _entry("claude", "AICC:frontend"),
        _entry("codex", "AICC:migration"),
    ]
    ratings = compute_ratings(entries, significance_threshold=1)
    assert set(ratings) == {
        ("claude", "AICC:migration"),
        ("claude", "AICC:frontend"),
        ("codex", "AICC:migration"),
    }
    # A strong rating on one task class must not leak into another for the
    # same agent — no implicit transfer across classes.
    assert ratings[("claude", "AICC:migration")].accepted_count == 1
    assert ratings[("claude", "AICC:frontend")].accepted_count == 1


def test_score_is_acceptance_rate_over_all_attempts():
    entries = [
        _entry(),  # accepted
        _entry(),  # accepted
        _entry(merged_sha=None),  # not accepted (never merged)
        _entry(review_verdict="rejected"),  # not accepted (rejected)
    ]
    rating = compute_ratings(entries, significance_threshold=1)[("claude", "AICC:migration")]
    assert rating.attempted_count == 4
    assert rating.accepted_count == 2
    assert rating.score == 0.5


def test_only_completed_runs_never_merged_score_zero_not_missing():
    entries = [_entry(merged_sha=None), _entry(merged_sha=None)]
    rating = compute_ratings(entries, significance_threshold=1)[("claude", "AICC:migration")]
    assert rating.attempted_count == 2
    assert rating.accepted_count == 0
    assert rating.score == 0.0


# --------------------------------------------------------------------------
# XP must be a landed, independently-verified change — not a completed run.
# --------------------------------------------------------------------------


def test_unmerged_runs_do_not_count_as_experience_even_if_many():
    """An agent that merely completes runs, without landing any of them, must
    not out-score an agent with a few real accepted changes — otherwise the
    rating optimizes for throughput of attempts rather than for the outcome,
    which is the exact metrics-become-the-target risk the idea calls out."""
    prolific_but_unmerged = [_entry("prolific", merged_sha=None) for _ in range(50)]
    modest_but_landed = [_entry("modest") for _ in range(5)]
    ratings = compute_ratings(
        prolific_but_unmerged + modest_but_landed, significance_threshold=5
    )
    prolific = ratings[("prolific", "AICC:migration")]
    modest = ratings[("modest", "AICC:migration")]
    assert prolific.score == 0.0
    assert modest.score == 1.0
    assert usable_score(prolific) is None
    assert usable_score(modest) == 1.0


def test_self_approved_verdict_does_not_count_as_acceptance():
    entries = [_entry(review_verdict="self_approved") for _ in range(10)]
    rating = compute_ratings(entries, significance_threshold=1)[("claude", "AICC:migration")]
    assert rating.accepted_count == 0
    assert rating.score == 0.0


# --------------------------------------------------------------------------
# avg_cost_usd: the price paid for the accepted changes only.
# --------------------------------------------------------------------------


def test_avg_cost_usd_covers_accepted_attempts_only():
    entries = [
        _entry(cost_usd=2.0),  # accepted
        _entry(cost_usd=4.0),  # accepted
        _entry(cost_usd=100.0, merged_sha=None),  # rework, never landed
    ]
    rating = compute_ratings(entries, significance_threshold=1)[("claude", "AICC:migration")]
    assert rating.avg_cost_usd == 3.0


def test_avg_cost_usd_is_zero_when_nothing_accepted():
    entries = [_entry(cost_usd=9.0, merged_sha=None)]
    rating = compute_ratings(entries, significance_threshold=1)[("claude", "AICC:migration")]
    assert rating.avg_cost_usd == 0.0


# --------------------------------------------------------------------------
# significance threshold: rating on a small sample is noise, not a level.
# --------------------------------------------------------------------------


def test_confident_is_false_below_significance_threshold():
    entries = [_entry() for _ in range(4)]
    rating = compute_ratings(entries, significance_threshold=5)[("claude", "AICC:migration")]
    assert rating.accepted_count == 4
    assert rating.confident is False


def test_confident_is_true_at_or_above_significance_threshold():
    entries = [_entry() for _ in range(5)]
    rating = compute_ratings(entries, significance_threshold=5)[("claude", "AICC:migration")]
    assert rating.accepted_count == 5
    assert rating.confident is True


def test_threshold_counts_accepted_changes_not_raw_attempts():
    """A pile of unmerged attempts must never manufacture confidence — the
    threshold is defined over accepted_count, per the acceptance criterion
    that rating comes from accepted changes, not from runs."""
    entries = [_entry() for _ in range(2)] + [_entry(merged_sha=None) for _ in range(20)]
    rating = compute_ratings(entries, significance_threshold=5)[("claude", "AICC:migration")]
    assert rating.attempted_count == 22
    assert rating.accepted_count == 2
    assert rating.confident is False


def test_default_significance_threshold_is_five():
    entries = [_entry() for _ in range(5)]
    rating = compute_ratings(entries)[("claude", "AICC:migration")]
    assert rating.confident is True
    entries = [_entry() for _ in range(4)]
    rating = compute_ratings(entries)[("claude", "AICC:migration")]
    assert rating.confident is False


# --------------------------------------------------------------------------
# usable_score: the only thing a router should ever read.
# --------------------------------------------------------------------------


def test_usable_score_is_none_for_missing_rating():
    assert usable_score(None) is None


def test_usable_score_is_none_when_not_confident():
    unconfident = AgentRating(
        executor_id="claude",
        task_class="AICC:migration",
        attempted_count=1,
        accepted_count=1,
        score=1.0,
        avg_cost_usd=1.0,
        confident=False,
    )
    assert usable_score(unconfident) is None


def test_usable_score_returns_the_score_when_confident():
    confident = AgentRating(
        executor_id="claude",
        task_class="AICC:migration",
        attempted_count=10,
        accepted_count=8,
        score=0.8,
        avg_cost_usd=1.5,
        confident=True,
    )
    assert usable_score(confident) == 0.8


def test_compute_ratings_is_pure_and_deterministic():
    entries = [_entry("claude", "AICC:migration") for _ in range(3)]
    first = compute_ratings(entries, significance_threshold=1)
    second = compute_ratings(entries, significance_threshold=1)
    assert first == second
