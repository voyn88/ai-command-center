"""The benchmark ledger gating aider+Ollama promotion (AICC Fleet decision
2026-09-03): the promotion bar, and the durability of `demote` against the
very next ordinary benchmark sample."""

from __future__ import annotations

import pytest

from command_center.orchestrator import local_model_gates


def test_an_unbenchmarked_class_is_not_promoted():
    record = local_model_gates.current_record("bounded_implementation")
    assert record.promoted is False
    assert record.sample_count == 0
    assert record.pass_count == 0
    assert local_model_gates.is_promoted("bounded_implementation") is False


def test_meets_promotion_bar_requires_both_sample_floor_and_pass_rate():
    below_floor = local_model_gates.GateRecord(
        task_class="x", sample_count=19, pass_count=19
    )
    assert local_model_gates.meets_promotion_bar(below_floor) is False

    at_floor_low_rate = local_model_gates.GateRecord(
        task_class="x", sample_count=20, pass_count=17
    )  # 0.85 < 0.9
    assert local_model_gates.meets_promotion_bar(at_floor_low_rate) is False

    at_floor_clears_rate = local_model_gates.GateRecord(
        task_class="x", sample_count=20, pass_count=18
    )  # 0.9 exactly
    assert local_model_gates.meets_promotion_bar(at_floor_clears_rate) is True


def test_record_benchmark_run_accrues_cumulative_counts():
    local_model_gates.record_benchmark_run("cls", True)
    updated = local_model_gates.record_benchmark_run("cls", False)
    assert updated.sample_count == 2
    assert updated.pass_count == 1
    assert updated.promoted is False


def test_record_benchmark_run_auto_promotes_the_moment_the_bar_clears():
    for _ in range(19):
        local_model_gates.record_benchmark_run("cls", True)
    almost = local_model_gates.current_record("cls")
    assert almost.promoted is False

    updated = local_model_gates.record_benchmark_run("cls", True)
    assert updated.sample_count == 20
    assert updated.promoted is True
    assert updated.reason == "auto_promoted"
    assert local_model_gates.is_promoted("cls") is True


def test_demote_requires_a_reason():
    with pytest.raises(ValueError):
        local_model_gates.demote("cls", reason="")


def test_demote_clears_promotion_and_is_visible_immediately():
    for _ in range(20):
        local_model_gates.record_benchmark_run("cls", True)
    assert local_model_gates.is_promoted("cls") is True

    demoted = local_model_gates.demote(
        "cls", reason="incident_2026_09_07", notes="bad patch merged"
    )
    assert demoted.promoted is False
    assert demoted.reason == "incident_2026_09_07"
    assert local_model_gates.is_promoted("cls") is False


def test_demote_survives_the_very_next_ordinary_benchmark_sample():
    """The regression this test pins: an earlier revision of `demote` left
    `sample_count`/`pass_count` untouched, so after a class had already
    cleared the 20-sample/90% bar once, ONE more accrued sample (pass or
    fail) almost never moves a ~20-sample cumulative average below 0.9 --
    meaning the very next ordinary `record_benchmark_run` call after an
    operator's demotion would silently re-promote the class and overwrite
    the operator's incident `reason`/`notes` with an auto-promotion message.
    `demote` must reset the counts so re-promotion needs fresh samples.
    """
    for _ in range(20):
        local_model_gates.record_benchmark_run("cls", True)
    assert local_model_gates.is_promoted("cls") is True

    local_model_gates.demote("cls", reason="incident", notes="do not trust this yet")

    # One ordinary benchmark sample, exactly what a scheduled benchmark run
    # would append next -- this must NOT flip promoted back to True.
    after_one_sample = local_model_gates.record_benchmark_run("cls", True)
    assert after_one_sample.promoted is False
    assert local_model_gates.is_promoted("cls") is False

    # The demotion's own record must not have been overwritten by the
    # auto-promotion path either -- read it back before the new sample was
    # appended, at the point `demote` returned.
    record_immediately_after_demote = local_model_gates.demote(
        "cls2", reason="incident", notes="keep for the audit trail"
    )
    assert record_immediately_after_demote.reason == "incident"
    assert record_immediately_after_demote.notes == "keep for the audit trail"

    # Re-promotion after a demotion requires PROMOTION_SAMPLE_FLOOR genuinely
    # fresh samples, not one more added to the old (now-reset) total. One
    # fresh sample was already recorded above (`after_one_sample`); this
    # loop adds the rest, stopping one short of the floor so the final,
    # separately-asserted call is the one that actually clears it.
    for _ in range(local_model_gates.PROMOTION_SAMPLE_FLOOR - 2):
        still_not_promoted = local_model_gates.record_benchmark_run("cls", True)
        assert still_not_promoted.promoted is False
    finally_repromoted = local_model_gates.record_benchmark_run("cls", True)
    assert finally_repromoted.sample_count == local_model_gates.PROMOTION_SAMPLE_FLOOR
    assert finally_repromoted.promoted is True


def test_ledger_is_isolated_per_task_class():
    local_model_gates.record_benchmark_run("class_a", True)
    local_model_gates.demote("class_b", reason="unrelated_incident")
    assert local_model_gates.current_record("class_a").sample_count == 1
    assert local_model_gates.current_record("class_b").sample_count == 0
    assert local_model_gates.is_promoted("class_a") is False
    assert local_model_gates.is_promoted("class_b") is False
