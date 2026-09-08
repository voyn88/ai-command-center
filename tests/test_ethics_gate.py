from command_center.ethics_gate import (
    CONFLICT_OF_INTEREST,
    RETRAINING,
    TOXICITY,
    AgentDecision,
    PolicyReview,
    classify_risk,
    evaluate_ethics_gate,
)

PROPOSER = "agent-council-writer"
REVIEWER = "human-ethics-officer"


def _decision(**overrides) -> AgentDecision:
    fields = dict(
        id="decision-1",
        proposer_id=PROPOSER,
        action="publish_response",
        stakeholders=frozenset(),
        toxicity_score=None,
        text=None,
    )
    fields.update(overrides)
    return AgentDecision(**fields)


def test_a_decision_with_no_risk_signal_is_allowed_without_any_review():
    decision = _decision(toxicity_score=0.0)

    verdict = evaluate_ethics_gate(decision)

    assert verdict.allowed is True
    assert verdict.risk_categories == frozenset()
    assert verdict.reasons == ()


def test_classify_risk_reads_structural_facts_not_the_decisions_own_claim():
    """An agent cannot clear the gate by asserting it is low-risk: nothing
    resembling a `self_reported_safe` flag is consulted."""
    decision = _decision(action="retrain_model", toxicity_score=0.0)

    assert classify_risk(decision) == frozenset({RETRAINING})


def test_retraining_action_is_detected_case_and_format_insensitively():
    for action in ("retrain_model", "Retrain-Model", "RETRAIN MODEL"):
        decision = _decision(action=action, toxicity_score=0.0)
        assert classify_risk(decision) == frozenset({RETRAINING})


def test_a_non_retraining_action_carries_no_retraining_risk():
    decision = _decision(action="publish_response", toxicity_score=0.0)

    assert classify_risk(decision) == frozenset()


def test_conflict_of_interest_is_read_from_proposer_in_stakeholders():
    decision = _decision(
        stakeholders=frozenset({PROPOSER, "other-party"}), toxicity_score=0.0
    )

    assert classify_risk(decision) == frozenset({CONFLICT_OF_INTEREST})


def test_stakeholders_without_the_proposer_carry_no_conflict():
    decision = _decision(stakeholders=frozenset({"other-party"}), toxicity_score=0.0)

    assert classify_risk(decision) == frozenset()


def test_a_toxicity_score_at_or_above_threshold_is_flagged():
    at_threshold = _decision(toxicity_score=0.5)
    above_threshold = _decision(toxicity_score=0.9)

    assert classify_risk(at_threshold) == frozenset({TOXICITY})
    assert classify_risk(above_threshold) == frozenset({TOXICITY})


def test_a_toxicity_score_below_threshold_is_not_flagged():
    decision = _decision(toxicity_score=0.49)

    assert classify_risk(decision) == frozenset()


def test_content_nobody_scored_fails_closed_as_toxicity_risk():
    """A missing score is not read as a clean one — otherwise the check is
    defeated by simply never running the classifier."""
    decision = _decision(toxicity_score=None, text="some rationale text")

    assert classify_risk(decision) == frozenset({TOXICITY})


def test_no_content_and_no_score_carries_no_toxicity_risk():
    decision = _decision(toxicity_score=None, text=None)

    assert classify_risk(decision) == frozenset()


def test_blank_text_with_no_score_carries_no_toxicity_risk():
    decision = _decision(toxicity_score=None, text="   ")

    assert classify_risk(decision) == frozenset()


def test_multiple_categories_are_all_detected_together():
    decision = _decision(
        action="retrain_model",
        stakeholders=frozenset({PROPOSER}),
        toxicity_score=0.9,
    )

    assert classify_risk(decision) == frozenset(
        {RETRAINING, CONFLICT_OF_INTEREST, TOXICITY}
    )


# --------------------------------------------------------------------------
# evaluate_ethics_gate — the policy-gate itself
# --------------------------------------------------------------------------


def test_a_high_risk_decision_without_any_review_is_blocked():
    decision = _decision(action="retrain_model", toxicity_score=0.0)

    verdict = evaluate_ethics_gate(decision)

    assert verdict.allowed is False
    assert verdict.risk_categories == frozenset({RETRAINING})
    assert verdict.reasons == (f"policy_review_required:{RETRAINING}",)


def test_a_high_risk_decision_with_a_covering_independent_approved_review_passes():
    decision = _decision(action="retrain_model", toxicity_score=0.0)
    review = PolicyReview(
        decision_id=decision.id,
        reviewer_id=REVIEWER,
        covers=frozenset({RETRAINING}),
        approved=True,
    )

    verdict = evaluate_ethics_gate(decision, review)

    assert verdict.allowed is True
    assert verdict.reasons == ()


def test_a_review_bound_to_a_different_decision_is_refused():
    decision = _decision(action="retrain_model", toxicity_score=0.0)
    review = PolicyReview(
        decision_id="some-other-decision",
        reviewer_id=REVIEWER,
        covers=frozenset({RETRAINING}),
    )

    verdict = evaluate_ethics_gate(decision, review)

    assert verdict.allowed is False
    assert "policy_review_decision_mismatch" in verdict.reasons


def test_a_review_authored_by_the_decisions_own_proposer_is_not_independent():
    decision = _decision(action="retrain_model", toxicity_score=0.0)
    review = PolicyReview(
        decision_id=decision.id,
        reviewer_id=PROPOSER,
        covers=frozenset({RETRAINING}),
    )

    verdict = evaluate_ethics_gate(decision, review)

    assert verdict.allowed is False
    assert "policy_review_not_independent" in verdict.reasons


def test_an_unapproved_review_does_not_clear_the_gate():
    decision = _decision(action="retrain_model", toxicity_score=0.0)
    review = PolicyReview(
        decision_id=decision.id,
        reviewer_id=REVIEWER,
        covers=frozenset({RETRAINING}),
        approved=False,
    )

    verdict = evaluate_ethics_gate(decision, review)

    assert verdict.allowed is False
    assert "policy_review_rejected" in verdict.reasons


def test_a_review_covering_only_one_of_two_categories_is_partial():
    decision = _decision(
        action="retrain_model",
        stakeholders=frozenset({PROPOSER}),
        toxicity_score=0.0,
    )
    review = PolicyReview(
        decision_id=decision.id,
        reviewer_id=REVIEWER,
        covers=frozenset({RETRAINING}),
    )

    verdict = evaluate_ethics_gate(decision, review)

    assert verdict.allowed is False
    assert verdict.reasons == (
        f"policy_review_missing_coverage:{CONFLICT_OF_INTEREST}",
    )


def test_a_review_covering_every_detected_category_clears_a_multi_risk_decision():
    decision = _decision(
        action="retrain_model",
        stakeholders=frozenset({PROPOSER}),
        toxicity_score=0.9,
    )
    review = PolicyReview(
        decision_id=decision.id,
        reviewer_id=REVIEWER,
        covers=frozenset({RETRAINING, CONFLICT_OF_INTEREST, TOXICITY}),
    )

    verdict = evaluate_ethics_gate(decision, review)

    assert verdict.allowed is True
    assert verdict.risk_categories == frozenset(
        {RETRAINING, CONFLICT_OF_INTEREST, TOXICITY}
    )
    assert verdict.reasons == ()


def test_a_review_that_over_covers_unrelated_categories_still_passes():
    """Covering more than what was found is fine; only under-coverage blocks."""
    decision = _decision(action="retrain_model", toxicity_score=0.0)
    review = PolicyReview(
        decision_id=decision.id,
        reviewer_id=REVIEWER,
        covers=frozenset({RETRAINING, TOXICITY}),
    )

    verdict = evaluate_ethics_gate(decision, review)

    assert verdict.allowed is True
