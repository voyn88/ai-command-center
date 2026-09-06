"""The aider+Ollama promotion ledger (VOYN-W0-AICC-AIDER-OLLAMA-EXECUTOR):
fail-closed by construction until a task class accumulates enough passing
benchmark evidence.

`AICC_DATA_DIR` is redirected to a temp dir by the session conftest (reset
between tests), so `root` below is unused for its own sake -- same pattern as
`tests/dispatch/test_policy_config.py`.
"""

from __future__ import annotations

from pathlib import Path

from command_center.orchestrator import local_model_gates

ROOT = Path("/unused-because-AICC_DATA_DIR-overrides")


def test_unbenchmarked_class_is_not_promoted():
    assert local_model_gates.is_promoted("bounded_implementation", ROOT) is False


def test_unknown_task_class_is_not_promoted():
    local_model_gates.record_benchmark_run(ROOT, "bounded_implementation", passed=True)
    assert local_model_gates.is_promoted("some_other_class", ROOT) is False


def test_promotion_requires_both_sample_size_and_pass_rate():
    # High pass rate, too few samples: still not promoted.
    for _ in range(local_model_gates.MIN_BENCHMARK_SAMPLES - 1):
        local_model_gates.record_benchmark_run(ROOT, "bounded_implementation", passed=True)
    assert local_model_gates.is_promoted("bounded_implementation", ROOT) is False

    # One more passing sample clears both bars.
    record = local_model_gates.record_benchmark_run(
        ROOT, "bounded_implementation", passed=True
    )
    assert record.promoted is True
    assert record.sample_count == local_model_gates.MIN_BENCHMARK_SAMPLES
    assert local_model_gates.is_promoted("bounded_implementation", ROOT) is True


def test_promotion_is_withheld_below_the_pass_rate_bar():
    samples = local_model_gates.MIN_BENCHMARK_SAMPLES
    failures = int(samples * (1 - local_model_gates.MIN_PASS_RATE)) + 1
    for index in range(samples):
        local_model_gates.record_benchmark_run(
            ROOT, "bounded_implementation", passed=index >= failures
        )
    assert local_model_gates.is_promoted("bounded_implementation", ROOT) is False


def test_promotion_is_a_ratchet_a_single_later_failure_does_not_revoke_it():
    for _ in range(local_model_gates.MIN_BENCHMARK_SAMPLES):
        local_model_gates.record_benchmark_run(ROOT, "bounded_implementation", passed=True)
    assert local_model_gates.is_promoted("bounded_implementation", ROOT) is True

    record = local_model_gates.record_benchmark_run(
        ROOT, "bounded_implementation", passed=False
    )
    assert record.promoted is True
    assert local_model_gates.is_promoted("bounded_implementation", ROOT) is True


def test_demote_is_an_explicit_operator_action():
    for _ in range(local_model_gates.MIN_BENCHMARK_SAMPLES):
        local_model_gates.record_benchmark_run(ROOT, "bounded_implementation", passed=True)
    assert local_model_gates.is_promoted("bounded_implementation", ROOT) is True

    record = local_model_gates.demote(
        ROOT, "bounded_implementation", reason="incident 2026-09-06", actor="owner"
    )
    assert record.promoted is False
    assert record.notes == "incident 2026-09-06"
    assert record.updated_by == "owner"
    assert local_model_gates.is_promoted("bounded_implementation", ROOT) is False
    # The accrued sample history survives a demotion -- it is not amnesia,
    # just a revoked verdict, so a later re-promotion is not starting over.
    assert record.sample_count == local_model_gates.MIN_BENCHMARK_SAMPLES


def test_record_benchmark_run_accumulates_across_calls():
    local_model_gates.record_benchmark_run(ROOT, "bounded_implementation", passed=True)
    record = local_model_gates.record_benchmark_run(
        ROOT, "bounded_implementation", passed=False
    )
    assert record.sample_count == 2
    assert record.pass_count == 1
    assert record.pass_rate == 0.5
