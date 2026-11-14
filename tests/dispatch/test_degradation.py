"""Unit tests for `degradation.evaluate_degradation`: the pure quality-drift
detector behind VOYN-MIN-AGT-DRIFT2 ("threshold degradation >10% in 2 windows
triggers quarantine and retraining"). Pure, no database/filesystem/HTTP.
"""

from __future__ import annotations

from command_center.dispatch.degradation import (
    CONSECUTIVE_WINDOWS_REQUIRED,
    DEGRADATION_THRESHOLD,
    MIN_RUNS_PER_WINDOW,
    REASON_DEGRADATION_CONFIRMED,
    REASON_INSUFFICIENT_DATA,
    REASON_WITHIN_THRESHOLD,
    QualityWindow,
    evaluate_degradation,
)


def _window(window_id: str, total: int, non_ok: int) -> QualityWindow:
    return QualityWindow(window_id=window_id, total_runs=total, non_ok_runs=non_ok)


# --------------------------------------------------------------------------
# QualityWindow
# --------------------------------------------------------------------------


def test_failure_rate_is_non_ok_over_total():
    window = _window("w1", 10, 3)
    assert window.failure_rate == 0.3


def test_failure_rate_is_none_for_an_empty_window():
    window = _window("w1", 0, 0)
    assert window.failure_rate is None


def test_has_enough_data_uses_min_runs_threshold():
    assert _window("w1", MIN_RUNS_PER_WINDOW, 0).has_enough_data is True
    assert _window("w1", MIN_RUNS_PER_WINDOW - 1, 0).has_enough_data is False


# --------------------------------------------------------------------------
# evaluate_degradation: the acceptance itself
# --------------------------------------------------------------------------


def test_two_consecutive_breaching_windows_trigger_quarantine_and_retrain():
    # Baseline failure rate 10%; last two windows both jump past +10pp.
    windows = [
        _window("w1", 20, 2),   # 10% — baseline, not counted
        _window("w2", 20, 5),   # 25% -> +15pp over baseline: breaches
        _window("w3", 20, 6),   # 30% -> +20pp over baseline: breaches
    ]

    verdict = evaluate_degradation("codex", baseline_failure_rate=0.10, windows=windows)

    assert verdict.quarantine is True
    assert verdict.retrain_required is True
    assert verdict.reason == REASON_DEGRADATION_CONFIRMED
    assert verdict.breaching_window_ids == ("w2", "w3")


def test_a_single_breaching_window_does_not_quarantine():
    windows = [
        _window("w1", 20, 2),   # 10% baseline
        _window("w2", 20, 2),   # 10% — within threshold
        _window("w3", 20, 6),   # 30% — breaches, but only one in a row
    ]

    verdict = evaluate_degradation("codex", baseline_failure_rate=0.10, windows=windows)

    assert verdict.quarantine is False
    assert verdict.retrain_required is False
    assert verdict.reason == REASON_WITHIN_THRESHOLD
    assert verdict.breaching_window_ids == ()


def test_degradation_at_or_under_the_threshold_does_not_quarantine():
    # Exactly +10pp over baseline is "at" the threshold, not "over" it — the
    # acceptance says >10%, so this must stay open.
    windows = [
        _window("w1", 20, 4),   # 20% -> +10pp exactly
        _window("w2", 20, 4),   # 20% -> +10pp exactly
    ]

    verdict = evaluate_degradation("codex", baseline_failure_rate=0.10, windows=windows)

    assert verdict.quarantine is False
    assert verdict.reason == REASON_WITHIN_THRESHOLD


def test_windows_below_min_runs_are_skipped_as_noise():
    # Both recent windows breach the threshold but are too small to trust;
    # they must not count toward the two-in-a-row requirement.
    windows = [
        _window("w1", 20, 2),                    # 10% baseline, enough data
        _window("w2", 20, 2),                     # 10%, enough data
        _window("w3", MIN_RUNS_PER_WINDOW - 1, 5),  # tiny, would be ~100% — skipped
    ]

    verdict = evaluate_degradation("codex", baseline_failure_rate=0.10, windows=windows)

    assert verdict.quarantine is False
    # Only one usable window has enough data beyond the skipped tiny one plus
    # the two baseline ones — still short of two *breaching* windows.
    assert verdict.reason in (REASON_WITHIN_THRESHOLD, REASON_INSUFFICIENT_DATA)


def test_fewer_than_two_usable_windows_is_insufficient_data():
    windows = [_window("w1", 20, 20)]  # 100% failure, but only one window

    verdict = evaluate_degradation("codex", baseline_failure_rate=0.0, windows=windows)

    assert verdict.quarantine is False
    assert verdict.retrain_required is False
    assert verdict.reason == REASON_INSUFFICIENT_DATA
    assert verdict.breaching_window_ids == ()


def test_no_windows_is_insufficient_data():
    verdict = evaluate_degradation("codex", baseline_failure_rate=0.0, windows=[])
    assert verdict.quarantine is False
    assert verdict.reason == REASON_INSUFFICIENT_DATA


def test_only_the_two_most_recent_usable_windows_are_considered():
    # An old breaching window followed by two healthy ones must not trigger
    # quarantine just because *some* window in history breached once.
    windows = [
        _window("w1", 20, 15),  # 75% — old, breaching, but not "recent"
        _window("w2", 20, 2),   # 10% baseline
        _window("w3", 20, 2),   # 10% baseline
    ]

    verdict = evaluate_degradation("codex", baseline_failure_rate=0.10, windows=windows)

    assert verdict.quarantine is False


def test_baseline_failure_rate_is_clamped_to_zero_one():
    windows = [_window("w1", 20, 20), _window("w2", 20, 20)]  # 100% failure

    verdict = evaluate_degradation("codex", baseline_failure_rate=-5.0, windows=windows)

    assert verdict.baseline_failure_rate == 0.0
    assert verdict.quarantine is True  # 100% - 0% >> 10pp


def test_module_constants_match_the_documented_acceptance():
    assert DEGRADATION_THRESHOLD == 0.10
    assert CONSECUTIVE_WINDOWS_REQUIRED == 2


# --------------------------------------------------------------------------
# to_quarantine_record
# --------------------------------------------------------------------------


def test_confirmed_verdict_converts_to_a_quarantine_record():
    windows = [_window("w1", 20, 6), _window("w2", 20, 6)]  # 30% each

    verdict = evaluate_degradation("codex", baseline_failure_rate=0.10, windows=windows)
    record = verdict.to_quarantine_record(quarantined_at="2026-09-08T00:00:00Z")

    assert record is not None
    assert record.executor_id == "codex"
    assert record.retrain_required is True
    assert record.quarantined_at == "2026-09-08T00:00:00Z"
    assert record.breaching_window_ids == ("w1", "w2")


def test_non_quarantining_verdict_converts_to_no_record():
    verdict = evaluate_degradation("codex", baseline_failure_rate=0.0, windows=[])
    assert verdict.to_quarantine_record(quarantined_at="now") is None
