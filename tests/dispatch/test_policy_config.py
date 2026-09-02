"""Tests for `DispatchPolicy` (de)serialization and its persistence layer.

`AICC_DATA_DIR` is redirected to a temp dir by the session conftest, so
`policy_config.*` writes never touch the developer's real `data/`.
"""

from __future__ import annotations

from pathlib import Path

from command_center.dispatch import policy_config
from command_center.http_auth.identity import Principal
from command_center.dispatch.models import (
    DEFAULT_PRIORITY_WEIGHTS,
    DEFAULT_TIER_BUDGET_MULTIPLIER,
    DEFAULT_TIER_PRIORITY_BONUS,
    DEFAULT_TIER_THRESHOLDS,
    AgentLimit,
    DispatchPolicy,
)

ROOT = Path("/unused-because-AICC_DATA_DIR-overrides")


def test_policy_roundtrips_through_dict():
    policy = DispatchPolicy(
        prefer_local=False,
        cost_matrix={"claude_code": 0.3, "ollama": 0.0},
        default_cost_usd=2.0,
        per_agent_limits={"codex": AgentLimit(max_concurrent=2, max_spend_usd=5.0)},
        per_project_limits={"AICC": 10.0},
        priority_weights={"Critical": 99},
        local_executor_ids=frozenset({"ollama"}),
    )
    restored = DispatchPolicy.from_dict(policy.as_dict())

    assert restored.prefer_local is False
    assert restored.cost_matrix == {"claude_code": 0.3, "ollama": 0.0}
    assert restored.default_cost_usd == 2.0
    assert restored.per_agent_limits["codex"].max_concurrent == 2
    assert restored.per_agent_limits["codex"].max_spend_usd == 5.0
    assert restored.per_project_limits == {"AICC": 10.0}
    assert restored.priority_weights == {"Critical": 99}
    assert restored.is_local("ollama")


def test_from_dict_is_fail_closed_on_garbage():
    for garbage in (None, [], "nope", 42, {"cost_matrix": "not-a-dict"}):
        policy = DispatchPolicy.from_dict(garbage)
        # Safe defaults: prefer local, standard priority weights, no exotic limits.
        assert policy.prefer_local is True
        assert policy.priority_weights == DEFAULT_PRIORITY_WEIGHTS
        assert policy.per_agent_limits == {}
        # And the leadership-metrics defaults (VOYN-AGT-REWARD): no scores on
        # file, standard tier ladder, no experimental zones carved out.
        assert policy.leaderboard == {}
        assert policy.tier_thresholds == DEFAULT_TIER_THRESHOLDS
        assert policy.tier_priority_bonus == DEFAULT_TIER_PRIORITY_BONUS
        assert policy.tier_budget_multiplier == DEFAULT_TIER_BUDGET_MULTIPLIER
        assert policy.experimental_executor_ids == frozenset()
        assert policy.experimental_min_tier == "trusted"


def test_leaderboard_roundtrips_through_dict():
    policy = DispatchPolicy(
        leaderboard={"codex": 92.5, "claude_code": 40.0},
        tier_thresholds={"elite": 90.0, "trusted": 50.0, "standard": 0.0},
        tier_priority_bonus={"elite": 5, "trusted": 1, "standard": 0},
        tier_budget_multiplier={"elite": 3.0, "trusted": 1.2, "standard": 1.0},
        experimental_executor_ids=frozenset({"codex"}),
        experimental_min_tier="elite",
    )
    restored = DispatchPolicy.from_dict(policy.as_dict())

    assert restored.leaderboard == {"codex": 92.5, "claude_code": 40.0}
    assert restored.tier_thresholds == {
        "elite": 90.0,
        "trusted": 50.0,
        "standard": 0.0,
    }
    assert restored.tier_priority_bonus == {"elite": 5, "trusted": 1, "standard": 0}
    assert restored.tier_budget_multiplier == {
        "elite": 3.0,
        "trusted": 1.2,
        "standard": 1.0,
    }
    assert restored.experimental_executor_ids == frozenset({"codex"})
    assert restored.experimental_min_tier == "elite"
    assert restored.tier_for("codex") == "elite"
    assert restored.meets_experimental_bar("codex") is True
    assert restored.meets_experimental_bar("claude_code") is False


def test_leaderboard_scores_are_clamped_to_0_100():
    policy = DispatchPolicy.from_dict(
        {"leaderboard": {"over": 500, "under": -20, "bad": "not-a-number"}}
    )
    assert policy.leaderboard == {"over": 100.0, "under": 0.0}


def test_unrecognized_experimental_min_tier_fails_closed_to_the_strictest_bar():
    policy = DispatchPolicy(experimental_min_tier="not-a-real-tier")
    # No agent can clear a bar that fails closed to the strictest tier.
    assert policy.meets_experimental_bar("anyone") is False


def test_from_dict_drops_non_numeric_costs():
    policy = DispatchPolicy.from_dict(
        {"cost_matrix": {"claude_code": "free", "ollama": 0.0, "bad": True}}
    )
    assert policy.cost_matrix == {"ollama": 0.0}


def test_negative_costs_are_clamped_to_zero_via_cost_for():
    policy = DispatchPolicy(cost_matrix={"x": -5.0})
    assert policy.cost_for("x") == 0.0


def test_default_cost_used_for_unpriced_executor():
    policy = DispatchPolicy(cost_matrix={}, default_cost_usd=1.5)
    assert policy.cost_for("anything") == 1.5


def test_save_then_load_roundtrips_on_disk():
    policy = DispatchPolicy(
        prefer_local=False,
        cost_matrix={"ollama": 0.0},
        per_project_limits={"AICC": 7.0},
    )
    policy_config.save_policy(ROOT, policy, actor="tester")

    loaded = policy_config.load_policy(ROOT)
    assert loaded.prefer_local is False
    assert loaded.cost_matrix == {"ollama": 0.0}
    assert loaded.per_project_limits == {"AICC": 7.0}
    assert loaded.updated_by == "tester"
    assert loaded.updated_at is not None


def test_load_returns_defaults_when_nothing_saved():
    loaded = policy_config.load_policy(ROOT)
    assert loaded.prefer_local is True
    assert loaded.cost_matrix == {}


def test_update_policy_overlays_only_named_fields():
    policy_config.save_policy(
        ROOT,
        DispatchPolicy(prefer_local=True, cost_matrix={"ollama": 0.0}),
        actor="init",
    )

    updated = policy_config.update_policy(
        ROOT,
        {"prefer_local": False},
        principal=Principal(principal_id="editor", tenant_id="tenant-1"),
    )

    # The changed field took, the untouched cost matrix survived.
    assert updated.prefer_local is False
    assert updated.cost_matrix == {"ollama": 0.0}
    assert updated.updated_by == "editor"

    # And it is persisted, not just returned.
    assert policy_config.load_policy(ROOT).prefer_local is False
