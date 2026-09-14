"""Unit tests for `TailRiskScenario`: pricing, blocking, and fail-closed
(de)serialization. Pure, no database/filesystem/HTTP.
"""

from __future__ import annotations

from command_center.dispatch.models import (
    DEFAULT_TAIL_RISK_SCENARIOS,
    TailRiskScenario,
)


def _scenario(**overrides) -> TailRiskScenario:
    fields = dict(
        id="s1",
        label="Scenario One",
        business_path="AICC",
        probability=0.1,
        impact_usd=100.0,
        assumptions="test",
        limit_usd=5.0,
    )
    fields.update(overrides)
    return TailRiskScenario(**fields)


# --------------------------------------------------------------------------
# Pricing: expected_cost_usd = probability x impact_usd
# --------------------------------------------------------------------------


def test_expected_cost_is_probability_times_impact():
    scenario = _scenario(probability=0.2, impact_usd=1000.0, limit_usd=1000.0)
    assert scenario.expected_cost_usd == 200.0


def test_probability_above_one_is_clamped_for_pricing():
    scenario = _scenario(probability=5.0, impact_usd=100.0, limit_usd=1000.0)
    assert scenario.expected_cost_usd == 100.0


def test_negative_probability_and_impact_are_clamped_to_zero():
    scenario = _scenario(probability=-1.0, impact_usd=-100.0, limit_usd=1000.0)
    assert scenario.expected_cost_usd == 0.0


# --------------------------------------------------------------------------
# Blocking
# --------------------------------------------------------------------------


def test_exceeds_limit_when_expected_cost_is_over_the_limit():
    scenario = _scenario(probability=0.5, impact_usd=100.0, limit_usd=10.0)
    assert scenario.expected_cost_usd == 50.0
    assert scenario.exceeds_limit() is True


def test_does_not_exceed_limit_when_expected_cost_is_at_or_under_it():
    scenario = _scenario(probability=0.5, impact_usd=100.0, limit_usd=50.0)
    assert scenario.expected_cost_usd == 50.0
    assert scenario.exceeds_limit() is False


def test_zero_limit_means_unset_and_never_blocks():
    scenario = _scenario(probability=1.0, impact_usd=1_000_000.0, limit_usd=0.0)
    assert scenario.exceeds_limit() is False


# --------------------------------------------------------------------------
# (de)serialization — fail closed at the entry level
# --------------------------------------------------------------------------


def test_roundtrips_through_dict():
    scenario = _scenario()
    restored = TailRiskScenario.from_dict(scenario.id, scenario.as_dict())

    assert restored == scenario


def test_from_dict_drops_a_non_dict_entry():
    assert TailRiskScenario.from_dict("s1", "not-a-dict") is None
    assert TailRiskScenario.from_dict("s1", None) is None
    assert TailRiskScenario.from_dict("s1", 42) is None


def test_from_dict_clamps_garbage_numeric_fields_to_safe_defaults():
    scenario = TailRiskScenario.from_dict(
        "s1",
        {
            "business_path": "AICC",
            "probability": "high",
            "impact_usd": -5.0,
            "limit_usd": "unset",
        },
    )
    assert scenario is not None
    assert scenario.probability == 0.0
    assert scenario.impact_usd == 0.0
    assert scenario.limit_usd == 0.0
    assert scenario.expected_cost_usd == 0.0
    assert scenario.exceeds_limit() is False


def test_from_dict_defaults_missing_business_path_to_empty_and_never_matches():
    scenario = TailRiskScenario.from_dict("s1", {"probability": 1.0, "impact_usd": 1.0})
    assert scenario is not None
    assert scenario.business_path == ""


# --------------------------------------------------------------------------
# The default registry — the acceptance this module exists to satisfy
# --------------------------------------------------------------------------


def test_default_registry_has_exactly_five_scenarios():
    assert len(DEFAULT_TAIL_RISK_SCENARIOS) == 5


def test_default_registry_ids_are_unique():
    ids = [s.id for s in DEFAULT_TAIL_RISK_SCENARIOS]
    assert len(ids) == len(set(ids))


def test_every_default_scenario_documents_assumptions_and_a_real_limit():
    for scenario in DEFAULT_TAIL_RISK_SCENARIOS:
        assert scenario.business_path
        assert scenario.assumptions
        assert len(scenario.assumptions) > 20
        assert 0.0 < scenario.probability <= 1.0
        assert scenario.impact_usd > 0.0
        assert scenario.limit_usd > 0.0
