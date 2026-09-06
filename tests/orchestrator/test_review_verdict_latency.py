from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from command_center.orchestrator import review_merge

T0 = datetime(2026, 1, 1, 0, 0, 0)


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


def test_single_chunk_cycle_latency_is_completion_minus_enqueue():
    rows = [("TASK", "review:TASK:1:" + "a" * 40 + ":v8:base:" + "b" * 40 + ":diff:" + "c" * 64,
              at(0), at(30))]
    assert review_merge.review_verdict_latencies(rows) == [30.0]


def test_multi_chunk_cycle_uses_earliest_start_and_latest_finish():
    base = "review:TASK:1:" + "a" * 40 + ":v8:base:" + "b" * 40 + ":diff:" + "c" * 64
    hash0, hash1 = "d" * 64, "e" * 64
    rows = [
        ("TASK", f"{base}:chunk:0000:{hash0}", at(0), at(10)),
        ("TASK", f"{base}:chunk:0001:{hash1}", at(5), at(40)),
    ]
    assert review_merge.review_verdict_latencies(rows) == [40.0]


def test_incomplete_cycle_is_excluded_not_estimated():
    base = "review:TASK:1:" + "a" * 40 + ":v8:base:" + "b" * 40 + ":diff:" + "c" * 64
    hash0, hash1 = "d" * 64, "e" * 64
    rows = [
        ("TASK", f"{base}:chunk:0000:{hash0}", at(0), at(10)),
        ("TASK", f"{base}:chunk:0001:{hash1}", at(5), None),  # still in flight
    ]
    assert review_merge.review_verdict_latencies(rows) == []


def test_retry_uses_the_chunks_earliest_completed_attempt():
    base = "review:TASK:1:" + "a" * 40 + ":v8:base:" + "b" * 40 + ":diff:" + "c" * 64
    rows = [
        ("TASK", base, at(0), at(10)),          # first attempt succeeds fast
        ("TASK", f"{base}:retry:1", at(20), at(90)),  # a later, unnecessary retry
    ]
    assert review_merge.review_verdict_latencies(rows) == [10.0]


def test_non_review_keys_are_ignored():
    rows = [("TASK", "merge:TASK:whatever", at(0), at(5))]
    assert review_merge.review_verdict_latencies(rows) == []


def test_two_independent_cycles_are_not_merged():
    base_a = "review:TASK_A:1:" + "a" * 40 + ":v8:base:" + "b" * 40 + ":diff:" + "c" * 64
    base_b = "review:TASK_B:2:" + "a" * 40 + ":v8:base:" + "b" * 40 + ":diff:" + "c" * 64
    rows = [
        ("TASK_A", base_a, at(0), at(10)),
        ("TASK_B", base_b, at(0), at(50)),
    ]
    assert sorted(review_merge.review_verdict_latencies(rows)) == [10.0, 50.0]


# -- percentile / median_and_p95 ----------------------------------------------


def test_percentile_matches_linear_interpolation_reference_points():
    values = [10.0, 20.0, 30.0, 40.0]
    assert review_merge.percentile(values, 0.0) == 10.0
    assert review_merge.percentile(values, 1.0) == 40.0
    assert review_merge.percentile(values, 0.5) == 25.0


def test_percentile_is_order_independent():
    ordered = [1.0, 2.0, 3.0, 4.0, 5.0]
    shuffled = [3.0, 1.0, 5.0, 2.0, 4.0]
    assert review_merge.percentile(ordered, 0.95) == review_merge.percentile(
        shuffled, 0.95
    )


def test_percentile_single_value():
    assert review_merge.percentile([42.0], 0.95) == 42.0


def test_percentile_rejects_empty_or_out_of_range():
    with pytest.raises(ValueError):
        review_merge.percentile([], 0.5)
    with pytest.raises(ValueError):
        review_merge.percentile([1.0], 1.5)


def test_median_and_p95_none_for_no_data():
    assert review_merge.median_and_p95([]) is None


def test_median_and_p95_reports_both():
    values = [float(n) for n in range(1, 101)]  # 1..100
    median, p95 = review_merge.median_and_p95(values)
    assert median == pytest.approx(50.5)
    assert p95 == pytest.approx(95.05)
