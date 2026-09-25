"""Unit tests for the metric-manipulation detector and its penalty coefficients.

Pure-function tests: every case builds a `RunSignals` or a unified-run-shaped
dict by hand — no db, no filesystem — mirroring how `tests/test_report_parser.py`
exercises `report_parser` in isolation.
"""

from __future__ import annotations

import pytest

from command_center import models
from command_center.audit.gaming_score import GamingDetector, RunSignals, signals_from_run


def _signals(**overrides) -> RunSignals:
    base = dict(
        duration_seconds=600.0,
        files_touched=0,
        verdict=models.VERDICT_APPROVED_FOR_COMMIT,
        verdict_contradictory=False,
        confidence="high",
        validation_present=True,
    )
    base.update(overrides)
    return RunSignals(**base)


# --- signals_from_run -------------------------------------------------------


def test_signals_from_run_sums_all_three_file_buckets() -> None:
    run = {
        "duration_seconds": 42.0,
        "parsed": {
            "files_modified": ["a.py", "b.py"],
            "files_created": ["c.py"],
            "files_deleted": [],
            "verdict": models.VERDICT_READY_FOR_COMMIT,
            "verdict_contradictory": False,
            "confidence": "medium",
            "validation_result": "ran pytest, all green",
        },
    }
    signals = signals_from_run(run)
    assert signals.files_touched == 3
    assert signals.duration_seconds == 42.0
    assert signals.verdict == models.VERDICT_READY_FOR_COMMIT
    assert signals.confidence == "medium"
    assert signals.validation_present is True


def test_signals_from_run_tolerates_missing_parsed() -> None:
    signals = signals_from_run({"duration_seconds": None})
    assert signals.files_touched == 0
    assert signals.verdict is None
    assert signals.confidence == "none"
    assert signals.validation_present is False


def test_signals_from_run_blank_validation_text_is_not_present() -> None:
    run = {"parsed": {"validation_result": "   "}}
    assert signals_from_run(run).validation_present is False


# --- GamingDetector.detect ---------------------------------------------------


def test_clean_run_scores_zero_risk() -> None:
    detector = GamingDetector()
    score = detector.detect(_signals())
    assert score.risk == 0.0
    assert score.reasons() == []


def test_fast_dirty_approval_scores_high_risk() -> None:
    detector = GamingDetector()
    # 30 files touched in 30 seconds -> 60 files/minute, far past the default
    # 12 files/minute ceiling; approved with no validation and no confidence.
    score = detector.detect(
        _signals(
            duration_seconds=30.0,
            files_touched=30,
            confidence="none",
            validation_present=False,
        )
    )
    assert score.velocity == 1.0
    assert score.unvalidated_approval == 1.0
    assert score.hollow_approval == 1.0
    assert score.contradiction == 0.0
    # velocity(0.35) + unvalidated_approval(0.30) + hollow_approval(0.20), no contradiction.
    assert score.risk == pytest.approx(0.85)
    reasons = score.reasons()
    assert len(reasons) == 3


def test_unknown_duration_does_not_trigger_velocity() -> None:
    detector = GamingDetector()
    score = detector.detect(_signals(duration_seconds=None, files_touched=100))
    assert score.velocity == 0.0


def test_zero_files_touched_does_not_trigger_velocity() -> None:
    detector = GamingDetector()
    score = detector.detect(_signals(duration_seconds=1.0, files_touched=0))
    assert score.velocity == 0.0


def test_non_approval_verdict_never_flags_validation_or_confidence() -> None:
    detector = GamingDetector()
    score = detector.detect(
        _signals(
            verdict=models.VERDICT_FAILED,
            confidence="none",
            validation_present=False,
        )
    )
    assert score.unvalidated_approval == 0.0
    assert score.hollow_approval == 0.0


def test_medium_confidence_approval_is_half_hollow() -> None:
    detector = GamingDetector()
    score = detector.detect(_signals(confidence="medium"))
    assert score.hollow_approval == 0.5


def test_contradictory_verdict_flags_even_without_approval() -> None:
    detector = GamingDetector()
    score = detector.detect(
        _signals(verdict=None, verdict_contradictory=True, validation_present=True, confidence="high")
    )
    assert score.contradiction == 1.0
    assert score.risk > 0.0
    assert "contradictory" in score.reasons()[0]


# --- weight/threshold validation & normalization -----------------------------


def test_weights_normalize_regardless_of_raw_scale() -> None:
    small = GamingDetector(
        velocity_weight=0.1, unvalidated_approval_weight=0.1, hollow_approval_weight=0.1, contradiction_weight=0.1
    )
    large = GamingDetector(
        velocity_weight=10, unvalidated_approval_weight=10, hollow_approval_weight=10, contradiction_weight=10
    )
    signals = _signals(verdict_contradictory=True)
    assert small.detect(signals).risk == pytest.approx(large.detect(signals).risk)


def test_zero_sum_weights_raise() -> None:
    with pytest.raises(ValueError):
        GamingDetector(
            velocity_weight=0, unvalidated_approval_weight=0, hollow_approval_weight=0, contradiction_weight=0
        )


def test_non_positive_velocity_ceiling_raises() -> None:
    with pytest.raises(ValueError):
        GamingDetector(velocity_ceiling_files_per_minute=0)


# --- penalize ----------------------------------------------------------------


def test_penalize_leaves_clean_run_value_untouched() -> None:
    detector = GamingDetector(penalty_coefficient=0.75)
    score = detector.detect(_signals())
    assert detector.penalize(10.0, score) == 10.0


def test_penalize_discounts_scale_with_risk() -> None:
    detector = GamingDetector(penalty_coefficient=0.75)
    score = detector.detect(
        _signals(duration_seconds=1.0, files_touched=1000, confidence="none", validation_present=False)
    )
    assert score.risk == pytest.approx(0.85)
    # value * (1 - penalty_coefficient * risk) = 10 * (1 - 0.75 * 0.85)
    assert detector.penalize(10.0, score) == pytest.approx(3.625)


def test_penalty_coefficient_is_clamped() -> None:
    over = GamingDetector(penalty_coefficient=5.0)
    under = GamingDetector(penalty_coefficient=-5.0)
    assert over.penalty_coefficient == 1.0
    assert under.penalty_coefficient == 0.0
