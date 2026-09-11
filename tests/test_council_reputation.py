"""Unit tests for the reputation domain logic
(``command_center.council.reputation`` — VOYN-MIN-LINK-REPUTE).

Pure functions, no db/service involved: every case builds the joined
vote-with-outcome row shape by hand and asserts on the resulting score, its
``basis`` and that the explanation actually names the numbers it used.
"""

from __future__ import annotations

from command_center.council import reputation as rep


def _vote(motion_id, voter_id, choice, *, outcome=None, tally=None, vote_id=None):
    return {
        "id": vote_id or f"{motion_id}:{voter_id}",
        "motion_id": motion_id,
        "voter_id": voter_id,
        "choice": choice,
        "decision_outcome": outcome,
        "decision_tally": tally,
    }


# --- alignment / pivotal / influence primitives ----------------------------


def test_alignment_agrees_with_winning_side() -> None:
    assert rep._alignment("yes", "approved") == 1.0
    assert rep._alignment("no", "rejected") == 1.0


def test_alignment_disagrees_with_winning_side() -> None:
    assert rep._alignment("no", "approved") == 0.0
    assert rep._alignment("yes", "rejected") == 0.0


def test_alignment_neutral_on_abstain_or_deferred() -> None:
    assert rep._alignment("abstain", "approved") == 0.5
    assert rep._alignment("yes", "deferred") == 0.5
    assert rep._alignment("no", "deferred") == 0.5


def test_pivotal_vote_detected_when_removing_it_flips_outcome() -> None:
    # 2 yes / 1 no -> approved. Remove one yes -> 1/1 tie -> deferred: pivotal.
    tally = {"yes": 2, "no": 1, "abstain": 0}
    assert rep._is_pivotal("yes", tally) is True
    # Remove the lone "no" -> 2/0 -> still approved: not pivotal.
    assert rep._is_pivotal("no", tally) is False


def test_abstain_is_never_pivotal_or_influential() -> None:
    tally = {"yes": 1, "no": 0, "abstain": 1}
    assert rep._is_pivotal("abstain", tally) is False
    assert rep._influence("abstain", tally) == 0.0


def test_influence_splits_credit_across_decisive_votes_when_not_pivotal() -> None:
    # 3 yes / 0 no -> approved; removing any single yes still leaves it approved.
    tally = {"yes": 3, "no": 0, "abstain": 0}
    assert rep._is_pivotal("yes", tally) is False
    assert rep._influence("yes", tally) == round(1 / 3, 4)


# --- compute_vote_trust_score -----------------------------------------------


def test_decided_motion_uses_outcome_alignment_basis() -> None:
    tally = {"yes": 1, "no": 0, "abstain": 0}
    vote = _vote("m1", "architect", "yes", outcome="approved", tally=tally)
    score = rep.compute_vote_trust_score(vote, [])
    assert score.basis == "outcome_alignment"
    assert score.score == 100.0  # aligned (1.0) and pivotal (1.0) -> full marks
    assert "approved" in score.explanation and "pivotal" in score.explanation


def test_decided_motion_scores_low_for_the_losing_minority() -> None:
    tally = {"yes": 2, "no": 1, "abstain": 0}
    vote = _vote("m1", "product", "no", outcome="approved", tally=tally)
    score = rep.compute_vote_trust_score(vote, [])
    assert score.basis == "outcome_alignment"
    # disagreed (alignment 0.0) but still shares 1/3 influence credit (not
    # pivotal: removing the lone "no" leaves it 2-0, still approved)
    assert score.score == round(100 * 0.4 * (1 / 3), 1)


def test_open_motion_falls_back_to_voter_history_average() -> None:
    history = [
        _vote("m1", "architect", "yes", outcome="approved", tally={"yes": 1, "no": 0, "abstain": 0}),
        _vote("m2", "architect", "no", outcome="rejected", tally={"yes": 0, "no": 1, "abstain": 0}),
    ]
    open_vote = _vote("m3", "architect", "yes")
    score = rep.compute_vote_trust_score(open_vote, history)
    assert score.basis == "voter_prior"
    assert score.votes_considered == 2
    assert score.score == 100.0  # both prior votes were fully aligned + pivotal
    assert "historical average" in score.explanation


def test_open_motion_excludes_own_motion_from_history() -> None:
    # A voter can only have one vote per motion, but defensively the fallback
    # must not count a (hypothetical) row sharing the vote's own motion_id.
    own_motion_row = _vote("m1", "architect", "no", outcome="approved", tally={"yes": 1, "no": 1, "abstain": 0})
    open_vote = _vote("m1", "architect", "yes")
    score = rep.compute_vote_trust_score(open_vote, [own_motion_row])
    assert score.basis == "insufficient_data"


def test_open_motion_with_no_history_is_insufficient_data() -> None:
    open_vote = _vote("m1", "newcomer", "yes")
    score = rep.compute_vote_trust_score(open_vote, [])
    assert score.basis == "insufficient_data"
    assert score.score is None
    assert "no prior decided votes" in score.explanation


# --- compute_voter_reputation ------------------------------------------------


def test_voter_reputation_insufficient_when_no_decided_votes() -> None:
    result = rep.compute_voter_reputation("newcomer", [])
    assert result.basis == "insufficient_data"
    assert result.score is None


def test_voter_reputation_aggregates_across_decided_votes() -> None:
    decided = [
        _vote("m1", "architect", "yes", outcome="approved", tally={"yes": 1, "no": 0, "abstain": 0}),
        _vote("m2", "architect", "no", outcome="approved", tally={"yes": 2, "no": 1, "abstain": 0}),
    ]
    result = rep.compute_voter_reputation("architect", decided)
    assert result.basis == "history"
    assert result.votes_considered == 2
    # first vote: aligned+pivotal (1.0, 1.0)
    # second vote: disagreed (0.0) but shares 1/3 influence credit (not pivotal)
    assert result.alignment_rate == 0.5
    assert result.influence_rate == round((1.0 + 1 / 3) / 2, 3)
    assert result.score == round(100 * (0.6 * 0.5 + 0.4 * result.influence_rate), 1)
    assert "architect" in result.explanation


# --- reputation_coverage -----------------------------------------------------


def test_reputation_coverage_counts_only_non_insufficient_basis() -> None:
    decided_score = rep.compute_vote_trust_score(
        _vote("m1", "a", "yes", outcome="approved", tally={"yes": 1, "no": 0, "abstain": 0}), []
    )
    prior_score = rep.compute_vote_trust_score(
        _vote("m2", "a", "yes"),
        [_vote("m1", "a", "yes", outcome="approved", tally={"yes": 1, "no": 0, "abstain": 0})],
    )
    insufficient_score = rep.compute_vote_trust_score(_vote("m3", "newcomer", "yes"), [])
    coverage = rep.reputation_coverage([decided_score, prior_score, insufficient_score])
    assert coverage == 2 / 3


def test_reputation_coverage_of_empty_input_is_vacuously_full() -> None:
    assert rep.reputation_coverage([]) == 1.0


def test_reputation_coverage_meets_acceptance_bar_on_a_realistic_mix() -> None:
    """The acceptance criterion (VOYN-MIN-LINK-REPUTE): at least 90% of votes
    carry an explainable trust score. Model a realistic board: 10 decided
    motions with 3 votes each (fully explainable via outcome_alignment) and 2
    freshly-opened motions each carrying one brand-new voter's first-ever vote
    (insufficient_data) — a small, expected tail of not-yet-explainable votes
    that must still clear the 90% bar."""
    scores = []
    history_by_voter: dict[str, list[dict]] = {"chair": [], "security": [], "product": []}
    for i in range(10):
        tally = {"yes": 2, "no": 1, "abstain": 0}
        motion_votes = [
            _vote(f"decided-{i}", "chair", "yes", outcome="approved", tally=tally, vote_id=f"v-{i}-chair"),
            _vote(f"decided-{i}", "security", "yes", outcome="approved", tally=tally, vote_id=f"v-{i}-security"),
            _vote(f"decided-{i}", "product", "no", outcome="approved", tally=tally, vote_id=f"v-{i}-product"),
        ]
        for v in motion_votes:
            scores.append(rep.compute_vote_trust_score(v, history_by_voter[v["voter_id"]]))
            history_by_voter[v["voter_id"]].append(v)
    for i in range(2):
        newcomer_vote = _vote(f"open-{i}", f"newcomer-{i}", "yes", vote_id=f"v-open-{i}")
        scores.append(rep.compute_vote_trust_score(newcomer_vote, []))
    coverage = rep.reputation_coverage(scores)
    assert len(scores) == 32
    assert coverage >= 0.9
