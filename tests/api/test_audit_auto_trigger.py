"""Unit tests for `command_center.api.audit_service.is_due` — the pure
due-ness decision behind the auto-trigger seam (`auto_trigger`,
`POST /audit/auto-trigger`). Endpoint-level behaviour (skips, events,
persistence) is covered in `tests/api/test_audit_endpoints.py`; this file
isolates the timestamp arithmetic so every edge case is fast and hermetic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from command_center.api import audit_service


def test_never_run_before_is_due() -> None:
    assert audit_service.is_due(None, now=datetime.now(UTC), min_interval_seconds=900)


def test_empty_string_last_run_is_due() -> None:
    assert audit_service.is_due("", now=datetime.now(UTC), min_interval_seconds=900)


def test_unparsable_timestamp_fails_open_to_due() -> None:
    assert audit_service.is_due(
        "not-a-timestamp", now=datetime.now(UTC), min_interval_seconds=900
    )


def test_recent_run_is_not_due() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    last_run_at = (now - timedelta(seconds=60)).isoformat()
    assert not audit_service.is_due(last_run_at, now=now, min_interval_seconds=900)


def test_run_exactly_at_interval_boundary_is_due() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    last_run_at = (now - timedelta(seconds=900)).isoformat()
    assert audit_service.is_due(last_run_at, now=now, min_interval_seconds=900)


def test_stale_run_is_due() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    last_run_at = (now - timedelta(hours=1)).isoformat()
    assert audit_service.is_due(last_run_at, now=now, min_interval_seconds=900)


def test_naive_timestamp_is_treated_as_utc() -> None:
    # `created_at` rows are stamped by `models.iso_now` without a timezone
    # offset; `is_due` must not raise comparing a naive stamp against an
    # aware `now` (or vice versa).
    now = datetime(2026, 1, 1, tzinfo=UTC)
    naive_last_run = (now - timedelta(seconds=30)).replace(tzinfo=None).isoformat()
    assert not audit_service.is_due(naive_last_run, now=now, min_interval_seconds=900)

    naive_now = now.replace(tzinfo=None)
    aware_last_run = (now - timedelta(seconds=30)).isoformat()
    assert not audit_service.is_due(
        aware_last_run, now=naive_now, min_interval_seconds=900
    )


def test_zero_interval_is_always_due() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    last_run_at = now.isoformat()
    assert audit_service.is_due(last_run_at, now=now, min_interval_seconds=0)


# --- interval resolution -----------------------------------------------------


def test_interval_override_wins_over_environment(monkeypatch) -> None:
    monkeypatch.setenv("AICC_AUDIT_AUTO_TRIGGER_INTERVAL_SECONDS", "60")
    assert audit_service._auto_trigger_interval_seconds(120) == 120


def test_interval_falls_back_to_environment(monkeypatch) -> None:
    monkeypatch.setenv("AICC_AUDIT_AUTO_TRIGGER_INTERVAL_SECONDS", "60")
    assert audit_service._auto_trigger_interval_seconds(None) == 60


def test_interval_falls_back_to_default_on_malformed_environment(monkeypatch) -> None:
    monkeypatch.setenv("AICC_AUDIT_AUTO_TRIGGER_INTERVAL_SECONDS", "not-a-number")
    assert (
        audit_service._auto_trigger_interval_seconds(None)
        == audit_service.DEFAULT_AUTO_TRIGGER_INTERVAL_SECONDS
    )


def test_interval_default_without_environment(monkeypatch) -> None:
    monkeypatch.delenv("AICC_AUDIT_AUTO_TRIGGER_INTERVAL_SECONDS", raising=False)
    assert (
        audit_service._auto_trigger_interval_seconds(None)
        == audit_service.DEFAULT_AUTO_TRIGGER_INTERVAL_SECONDS
    )
