"""Tests for `command_center.agent_policy` — the UI-configurable metric
weight / fallback / SLA policy engine (VOYN-MIN-AGT-TUNING).

The acceptance criterion is "1 new policy is deployed without a code
deploy": these tests verify a freshly `create_policy`d row is picked up by
`resolve_effective_policy` / `effective_weights` / `sla_seconds_for` /
`apply_agent_weights` on the very next read — no process restart, no
scheduler.py change required.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from command_center import agent_policy
from command_center.agent_policy import AgentPolicyError, InvalidPolicy, PolicyNotFound
from command_center.runtime import scheduler


@pytest.fixture()
def p_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "agent_policies.db"
    agent_policy.init_db(db_path)
    return db_path


def _registry() -> scheduler.AgentRegistry:
    return scheduler.AgentRegistry(
        [
            scheduler.AgentSpec("claude_code", "claude_code", frozenset({scheduler.CAP_ANY}), 2, weight=2),
            scheduler.AgentSpec("codex", "codex", frozenset({scheduler.CAP_ANY}), 2, weight=2),
            scheduler.AgentSpec("ollama", "ollama", frozenset({scheduler.CAP_ANY}), 2, weight=2),
        ]
    )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def test_create_policy_minimal_sla_only(p_db: Path):
    p = agent_policy.create_policy(p_db, name="SLA critical", priority="Critical", sla_seconds=3600)
    assert p["enabled"] is True
    assert p["sla_seconds"] == 3600
    assert p["task_type"] == agent_policy.ANY


def test_create_policy_requires_name(p_db: Path):
    with pytest.raises(InvalidPolicy):
        agent_policy.create_policy(p_db, name="  ", sla_seconds=60)


def test_create_policy_requires_at_least_one_knob(p_db: Path):
    with pytest.raises(InvalidPolicy):
        agent_policy.create_policy(p_db, name="Empty policy")


def test_create_policy_rejects_unknown_priority(p_db: Path):
    with pytest.raises(InvalidPolicy):
        agent_policy.create_policy(p_db, name="Bad prio", priority="Urgent", sla_seconds=60)


def test_create_policy_rejects_duplicate_fallback_agents(p_db: Path):
    with pytest.raises(InvalidPolicy):
        agent_policy.create_policy(
            p_db, name="Dup fallback", fallback_agents=["codex", "codex"]
        )


def test_create_policy_rejects_non_int_weight(p_db: Path):
    with pytest.raises(InvalidPolicy):
        agent_policy.create_policy(p_db, name="Bad weight", agent_weights={"codex": "high"})


def test_list_policies_ordered_by_most_recently_updated(p_db: Path):
    first = agent_policy.create_policy(p_db, name="First", sla_seconds=60)
    second = agent_policy.create_policy(p_db, name="Second", sla_seconds=120)
    listed = agent_policy.list_policies(p_db)
    assert [p["id"] for p in listed] == [second["id"], first["id"]]


def test_toggle_policy(p_db: Path):
    p = agent_policy.create_policy(p_db, name="Toggle me", sla_seconds=60)
    disabled = agent_policy.toggle_policy(p_db, p["id"], enabled=False)
    assert disabled["enabled"] is False
    assert agent_policy.list_policies(p_db, enabled_only=True) == []


def test_toggle_unknown_policy_raises(p_db: Path):
    with pytest.raises(PolicyNotFound):
        agent_policy.toggle_policy(p_db, "does-not-exist", enabled=True)


def test_delete_policy(p_db: Path):
    p = agent_policy.create_policy(p_db, name="Delete me", sla_seconds=60)
    agent_policy.delete_policy(p_db, p["id"])
    assert agent_policy.list_policies(p_db) == []


def test_delete_unknown_policy_raises(p_db: Path):
    with pytest.raises(PolicyNotFound):
        agent_policy.delete_policy(p_db, "does-not-exist")


# ---------------------------------------------------------------------------
# Matching / specificity
# ---------------------------------------------------------------------------


def test_resolve_effective_policy_none_when_no_match(p_db: Path):
    assert agent_policy.resolve_effective_policy(p_db, task_type="implementation", priority="Low") is None


def test_resolve_effective_policy_wildcard_matches_anything(p_db: Path):
    agent_policy.create_policy(p_db, name="Global SLA", sla_seconds=999)
    p = agent_policy.resolve_effective_policy(p_db, task_type="implementation", priority="Critical")
    assert p is not None
    assert p["name"] == "Global SLA"


def test_more_specific_policy_wins_over_wildcard(p_db: Path):
    agent_policy.create_policy(p_db, name="Global", sla_seconds=999)
    specific = agent_policy.create_policy(
        p_db, name="Critical implementation", task_type="implementation", priority="Critical", sla_seconds=60
    )
    p = agent_policy.resolve_effective_policy(p_db, task_type="implementation", priority="Critical")
    assert p["id"] == specific["id"]
    # A non-matching (task_type, priority) combo still falls back to the
    # wildcard policy rather than resolving nothing.
    p2 = agent_policy.resolve_effective_policy(p_db, task_type="verification_review", priority="Low")
    assert p2["name"] == "Global"


def test_disabled_policy_is_never_matched(p_db: Path):
    p = agent_policy.create_policy(p_db, name="Disabled", sla_seconds=60)
    agent_policy.toggle_policy(p_db, p["id"], enabled=False)
    assert agent_policy.resolve_effective_policy(p_db) is None


# ---------------------------------------------------------------------------
# SLA application
# ---------------------------------------------------------------------------


def test_sla_seconds_for_matching_policy(p_db: Path):
    agent_policy.create_policy(p_db, name="Critical SLA", priority="Critical", sla_seconds=1800)
    assert agent_policy.sla_seconds_for(p_db, priority="Critical") == 1800
    assert agent_policy.sla_seconds_for(p_db, priority="Low") is None


# ---------------------------------------------------------------------------
# Weight / fallback application onto the (unmodified) scheduler registry
# ---------------------------------------------------------------------------


def test_fallback_agents_become_descending_weights(p_db: Path):
    agent_policy.create_policy(
        p_db, name="Codex-first fallback", fallback_agents=["codex", "claude_code", "ollama"]
    )
    weights = agent_policy.effective_weights(p_db)
    assert weights == {"codex": 3, "claude_code": 2, "ollama": 1}


def test_explicit_agent_weights_override_fallback_positions(p_db: Path):
    agent_policy.create_policy(
        p_db,
        name="Override",
        fallback_agents=["codex", "claude_code"],
        agent_weights={"claude_code": 100},
    )
    weights = agent_policy.effective_weights(p_db)
    assert weights == {"codex": 2, "claude_code": 100}


def test_apply_agent_weights_reorders_registry(p_db: Path):
    registry = _registry()
    # Before tuning: all weight 2, tie-break is agent_id ascending.
    assert [a.agent_id for a in registry.all()] == ["claude_code", "codex", "ollama"]

    agent_policy.create_policy(p_db, name="Prefer codex", agent_weights={"codex": 50})
    weights = agent_policy.effective_weights(p_db)
    tuned = agent_policy.apply_agent_weights(registry, weights)

    assert [a.agent_id for a in tuned.all()] == ["codex", "claude_code", "ollama"]
    # The untouched agents keep their original weight/capabilities/etc.
    untouched = tuned.get("claude_code")
    assert untouched.weight == 2
    assert untouched.max_concurrency == 2


def test_apply_agent_weights_is_pure_and_does_not_mutate_input(p_db: Path):
    registry = _registry()
    agent_policy.apply_agent_weights(registry, {"ollama": 99})
    # Original registry is unaffected by tuning.
    assert registry.get("ollama").weight == 2


def test_tuned_registry_end_to_end_new_policy_takes_effect_without_redeploy(p_db: Path):
    """The acceptance test: a brand-new policy, created purely as data,
    changes the *next* scheduling decision with zero code involvement."""
    registry = _registry()
    baseline = agent_policy.tuned_registry(registry, p_db)
    assert [a.agent_id for a in baseline.all()] == ["claude_code", "codex", "ollama"]

    # Operator adds one new policy through the UI/API — no deploy.
    agent_policy.create_policy(
        p_db, name="Escalate ollama", fallback_agents=["ollama", "codex", "claude_code"]
    )

    tuned = agent_policy.tuned_registry(registry, p_db)
    assert [a.agent_id for a in tuned.all()] == ["ollama", "codex", "claude_code"]
