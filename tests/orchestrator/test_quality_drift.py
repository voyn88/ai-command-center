"""Quality-drift detection and agent quarantine (VOYN-AGT-DRIFT): hermetic,
no database -- the monitor is pure in-memory bookkeeping over caller-supplied
samples."""

from __future__ import annotations

import pytest

from command_center.orchestrator.quality_drift import QualityDriftMonitor


def _monitor(**kwargs):
    kwargs.setdefault("window_size", 1)
    kwargs.setdefault("deviation_threshold", 0.10)
    kwargs.setdefault("consecutive_breaches_to_quarantine", 2)
    return QualityDriftMonitor(**kwargs)


def test_first_window_establishes_baseline_and_is_never_a_breach():
    mon = _monitor()
    result = mon.record("agent-a", 100.0)
    assert result.baseline == 100.0
    assert result.mean == 100.0
    assert result.deviation == 0.0
    assert not result.breached
    assert not mon.is_quarantined("agent-a")


def test_two_consecutive_breaches_quarantine_the_agent():
    mon = _monitor()
    mon.record("agent-a", 100.0)  # baseline
    first = mon.record("agent-a", 85.0)  # -15%, breach 1
    assert first.breached
    assert not mon.is_quarantined("agent-a")

    second = mon.record("agent-a", 84.0)  # -16%, breach 2
    assert second.breached
    assert mon.is_quarantined("agent-a")
    assert "2 consecutive windows" in mon.quarantine_reason("agent-a")


def test_a_single_breach_does_not_quarantine():
    mon = _monitor()
    mon.record("agent-a", 100.0)
    mon.record("agent-a", 85.0)  # breach 1
    assert not mon.is_quarantined("agent-a")


def test_a_good_window_resets_the_consecutive_streak():
    mon = _monitor()
    mon.record("agent-a", 100.0)
    mon.record("agent-a", 85.0)  # breach 1
    mon.record("agent-a", 100.0)  # recovers, streak resets
    mon.record("agent-a", 85.0)  # breach again, but streak restarted at 1
    assert not mon.is_quarantined("agent-a")


def test_deviation_boundary_is_exclusive():
    mon = _monitor()
    mon.record("agent-a", 100.0)  # baseline
    exactly_ten_pct = mon.record("agent-a", 90.0)
    assert not exactly_ten_pct.breached, "exactly 10% is not > 10%"

    just_over = mon.record("agent-a", 89.9)
    assert just_over.breached


def test_improving_quality_never_breaches_regardless_of_magnitude():
    mon = _monitor()
    mon.record("agent-a", 100.0)  # baseline
    huge_improvement = mon.record("agent-a", 1000.0)
    assert not huge_improvement.breached
    assert huge_improvement.deviation < 0


def test_release_lifts_quarantine_and_resets_the_baseline():
    mon = _monitor()
    mon.record("agent-a", 100.0)
    mon.record("agent-a", 85.0)
    mon.record("agent-a", 84.0)
    assert mon.is_quarantined("agent-a")

    mon.release("agent-a")
    assert not mon.is_quarantined("agent-a")
    assert mon.quarantine_reason("agent-a") is None

    # The next closed window re-establishes baseline rather than comparing
    # against the pre-quarantine one.
    fresh_baseline = mon.record("agent-a", 10.0)
    assert fresh_baseline.baseline == 10.0
    assert not fresh_baseline.breached


def test_quarantine_persists_through_recovered_quality_until_released():
    mon = _monitor()
    mon.record("agent-a", 100.0)
    mon.record("agent-a", 85.0)
    mon.record("agent-a", 84.0)
    assert mon.is_quarantined("agent-a")

    recovered = mon.record("agent-a", 100.0)
    assert not recovered.breached
    assert mon.is_quarantined("agent-a"), "quarantine is a freeze, not a cooldown"


def test_agents_are_tracked_independently():
    mon = _monitor()
    mon.record("agent-a", 100.0)
    mon.record("agent-a", 85.0)
    mon.record("agent-a", 84.0)
    assert mon.is_quarantined("agent-a")

    mon.record("agent-b", 100.0)
    mon.record("agent-b", 99.0)
    assert not mon.is_quarantined("agent-b")


def test_record_returns_none_until_the_window_fills():
    mon = _monitor(window_size=3)
    assert mon.record("agent-a", 100.0) is None
    assert mon.record("agent-a", 90.0) is None
    result = mon.record("agent-a", 80.0)
    assert result is not None
    assert result.mean == pytest.approx(90.0)


def test_unknown_agent_is_not_quarantined_and_has_no_reason_or_history():
    mon = _monitor()
    assert not mon.is_quarantined("ghost")
    assert mon.quarantine_reason("ghost") is None
    assert mon.history("ghost") == []
    mon.release("ghost")  # must not raise


def test_history_records_every_closed_window_in_order():
    mon = _monitor()
    mon.record("agent-a", 100.0)
    mon.record("agent-a", 85.0)
    mon.record("agent-a", 84.0)
    history = mon.history("agent-a")
    assert [h.window_index for h in history] == [1, 2, 3]
    assert [h.breached for h in history] == [False, True, True]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"window_size": 0},
        {"deviation_threshold": 0.0},
        {"deviation_threshold": 1.0},
        {"consecutive_breaches_to_quarantine": 0},
    ],
)
def test_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        QualityDriftMonitor(**kwargs)
