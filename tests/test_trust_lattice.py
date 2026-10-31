"""Unit tests for :mod:`command_center.trust_lattice`."""

from __future__ import annotations

from command_center.trust_lattice import (
    COLD_START_PRIOR,
    RunOutcome,
    TrustLattice,
)


def _run(agent="claude", project="AICC", domain="backend", risk_level="low", success=True):
    return RunOutcome(
        agent=agent, project=project, domain=domain, risk_level=risk_level, success=success
    )


def test_cold_start_has_no_evidence_and_neutral_score() -> None:
    result = TrustLattice([]).score("claude", "AICC", "backend", "low")
    assert result.score == COLD_START_PRIOR
    assert all(f.sample_size == 0 for f in result.factors)
    assert all(f.empirical_rate is None for f in result.factors)


def test_every_score_carries_a_full_causal_chain() -> None:
    history = [_run(success=True) for _ in range(3)]
    result = TrustLattice(history).score("claude", "AICC", "backend", "low")
    levels = [f.level for f in result.factors]
    assert levels == [
        "cold_start",
        "agent",
        "agent+risk_level",
        "agent+domain+risk_level",
        "agent+project+domain+risk_level",
    ]
    # Each factor documents the blend step: prior going in, score coming out.
    for factor in result.factors:
        assert 0.0 <= factor.score_after <= 1.0
        assert 0.0 <= factor.confidence <= 1.0


def test_strong_track_record_raises_score_above_cold_start() -> None:
    history = [_run(success=True) for _ in range(20)]
    result = TrustLattice(history).score("claude", "AICC", "backend", "low")
    assert result.score > COLD_START_PRIOR
    assert result.score > 0.8


def test_poor_track_record_lowers_score_below_cold_start() -> None:
    history = [_run(success=False) for _ in range(20)]
    result = TrustLattice(history).score("claude", "AICC", "backend", "low")
    assert result.score < COLD_START_PRIOR
    assert result.score < 0.2


def test_scores_are_isolated_by_project_domain_and_risk_level() -> None:
    history = [
        *[_run(project="AICC", domain="backend", risk_level="high", success=False) for _ in range(10)],
        *[_run(project="OTHER", domain="frontend", risk_level="low", success=True) for _ in range(10)],
    ]
    lattice = TrustLattice(history)
    risky = lattice.score("claude", "AICC", "backend", "high")
    safe = lattice.score("claude", "OTHER", "frontend", "low")
    assert risky.score < safe.score
    exact = next(f for f in risky.factors if f.level == "agent+project+domain+risk_level")
    assert exact.sample_size == 10
    assert exact.empirical_rate == 0.0


def test_unfamiliar_context_falls_back_to_agent_baseline_not_zero() -> None:
    # Agent has a strong global record but has never touched this exact
    # project/domain/risk_level combination.
    history = [_run(project="AICC", domain="backend", risk_level="low", success=True) for _ in range(20)]
    lattice = TrustLattice(history)
    unfamiliar = lattice.score("claude", "NEW_PROJECT", "ml", "critical")
    exact = next(f for f in unfamiliar.factors if f.level == "agent+project+domain+risk_level")
    assert exact.sample_size == 0
    assert exact.empirical_rate is None
    # Falls back to the agent-global level's blended prior rather than 0.5.
    assert unfamiliar.score > COLD_START_PRIOR


def test_agents_with_different_histories_are_not_conflated() -> None:
    history = [
        *[_run(agent="claude", success=True) for _ in range(20)],
        *[_run(agent="codex", success=False) for _ in range(20)],
    ]
    lattice = TrustLattice(history)
    claude_score = lattice.score("claude", "AICC", "backend", "low")
    codex_score = lattice.score("codex", "AICC", "backend", "low")
    assert claude_score.score > codex_score.score


def test_more_samples_yield_more_confidence_at_matching_level() -> None:
    lattice_small = TrustLattice([_run(success=True) for _ in range(1)])
    lattice_large = TrustLattice([_run(success=True) for _ in range(50)])
    small = lattice_small.score("claude", "AICC", "backend", "low")
    large = lattice_large.score("claude", "AICC", "backend", "low")
    small_exact = next(f for f in small.factors if f.level == "agent+project+domain+risk_level")
    large_exact = next(f for f in large.factors if f.level == "agent+project+domain+risk_level")
    assert large_exact.confidence > small_exact.confidence
    # More confident good evidence pushes the score closer to 1.0.
    assert large.score > small.score


def test_invalid_smoothing_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        TrustLattice([], smoothing=0)


def test_with_history_preserves_tuning() -> None:
    lattice = TrustLattice([], smoothing=10.0, cold_start_prior=0.3)
    updated = lattice.with_history([_run(success=True) for _ in range(10)])
    result = updated.score("claude", "AICC", "backend", "low")
    cold_start_factor = result.factors[0]
    assert cold_start_factor.score_after == 0.3
