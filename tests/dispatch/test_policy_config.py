"""Tests for `DispatchPolicy` (de)serialization and its persistence layer.

`AICC_DATA_DIR` is redirected to a temp dir by the session conftest, so
`policy_config.*` writes never touch the developer's real `data/`.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from command_center.dispatch import policy_config
from command_center.http_auth.identity import Principal
from command_center.dispatch.models import (
    DEFAULT_PRIORITY_WEIGHTS,
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


def test_from_dict_drops_non_numeric_costs():
    policy = DispatchPolicy.from_dict(
        {"cost_matrix": {"claude_code": "free", "ollama": 0.0, "bad": True}}
    )
    assert policy.cost_matrix == {"ollama": 0.0}


def test_a_negative_cost_is_unusable_rather_than_clamped_to_free():
    """`cost_for` used to clamp a negative price to `0.0`, and that clamp was
    a fail-open: `0.0` does not mean "cheap", it means **free**, and a free
    executor satisfies the daily, per-agent and per-project ceilings at once
    no matter what it really costs. A price that is not usable money now
    resolves to NaN, which the engine refuses (`DEFER_DAILY_BUDGET`)."""
    policy = DispatchPolicy(cost_matrix={"x": -5.0})
    assert math.isnan(policy.cost_for("x"))


def test_a_non_finite_cost_never_reads_as_a_free_executor():
    """The measured shape of the defect, at the config layer. `max(0.0, nan)`
    is `0.0`, so a NaN price was laundered into a free executor before the
    engine's own finite check could ever see it — the corruption arrived
    already disguised as a valid, unbeatable price."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        policy = DispatchPolicy.from_dict({"cost_matrix": {"x": bad}})
        assert math.isnan(policy.cost_for("x")), bad

    # Control: an executor that is genuinely free is still free, so the guard
    # above cannot be passing by rejecting every zero.
    assert DispatchPolicy.from_dict({"cost_matrix": {"x": 0.0}}).cost_for("x") == 0.0


def test_an_unusable_ceiling_is_kept_unusable_rather_than_read_as_unset():
    """Ceilings widen through the *same* value a price widens through — `0.0`
    — but from the other side: for a limit it means "unset", i.e. no limit.
    So a corrupt ceiling must not decay into one either."""
    policy = DispatchPolicy.from_dict(
        {
            "per_project_limits": {"AICC": float("inf")},
            "per_agent_limits": {"codex": {"max_spend_usd": float("nan")}},
        }
    )
    assert math.isnan(policy.per_project_limits["AICC"])
    assert math.isnan(policy.per_agent_limits["codex"].max_spend_usd)


def test_an_unusable_concurrency_limit_tightens_rather_than_disappears():
    """`max_concurrent` is an `int`, so there is no NaN to carry — and `0`,
    the neutral fallback, means "no limit". An unusable value therefore falls
    back to the tightest *enforceable* limit instead. It must also not raise:
    `int(float("nan"))` is a `ValueError` and `int(float("inf"))` an
    `OverflowError`, either of which would take down every reader of the
    policy file, including `GET /api/v1/dispatch/policy`."""
    for bad in (float("nan"), float("inf"), -3, "two"):
        limit = AgentLimit.from_dict({"max_concurrent": bad})
        assert limit.max_concurrent == 1, bad

    # An *absent* key still means unset, which is what keeps a policy that
    # only configures a spend cap from acquiring a concurrency cap it never
    # asked for.
    assert AgentLimit.from_dict({"max_spend_usd": 5.0}).max_concurrent == 0


def test_an_unusable_amount_survives_the_json_round_trip_as_a_refusal():
    """The refusal has to outlive persistence. `json.dump` writes a bare `NaN`
    token that no RFC 8259 parser accepts, so `as_dict` degrades it to `null`
    — and `from_dict` has to read that `null` back as *unusable*, not as
    absent. Otherwise editing any unrelated policy field through
    `update_policy` (which rewrites the whole document) would silently restore
    the corrupt executor to the default price and clear the corrupt ceiling.
    """
    policy = DispatchPolicy.from_dict(
        {
            "cost_matrix": {"x": float("nan")},
            "per_project_limits": {"AICC": float("nan")},
        }
    )
    # `json.dumps` does not object to a NaN — it emits the bare token — so
    # the check has to be on the encoded text, not on an exception.
    encoded = json.dumps(policy.as_dict())
    assert "NaN" not in encoded

    restored = DispatchPolicy.from_dict(json.loads(encoded))
    assert math.isnan(restored.cost_for("x"))
    assert math.isnan(restored.per_project_limits["AICC"])


def test_a_corrupt_priority_weight_does_not_break_the_whole_policy():
    """A weight is an ordering hint, not a guardrail, so an unusable one is
    dropped — but it must be dropped rather than raise out of a `from_dict`
    documented as total."""
    policy = DispatchPolicy.from_dict(
        {"priority_weights": {"Critical": float("nan"), "High": 30}}
    )
    assert policy.priority_weights == {"High": 30}
    assert policy.priority_weight("Critical") == 0


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
