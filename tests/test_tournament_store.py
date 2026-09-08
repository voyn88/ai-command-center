"""Unit tests for ``command_center.tournament_store``: publishing, reading
back and the idempotent-per-month rebuild.

Hermetic like ``tests/test_digest_service.py``: the loader seams
(``_load_tasks_by_id`` / ``_load_completed_runs``) are monkeypatched with
fixed fixtures, so no real task store or runtime db is touched. ``AICC_DATA_DIR``
(set in ``tests/conftest.py``) already sandboxes ``tournament_store.PROTOCOLS_FILE``.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from command_center import tournament_store


@pytest.fixture
def stub_sources(monkeypatch):
    tasks_by_id = {"t1": {"id": "t1", "category": "Dev"}}
    runs = [
        {"task_id": "t1", "agent": "claude", "state": "COMPLETED", "completed_at": "2026-08-10T09:00:00"},
        {"task_id": "t1", "agent": "claude", "state": "COMPLETED", "completed_at": "2026-08-11T09:00:00"},
    ]
    monkeypatch.setattr(tournament_store, "_load_tasks_by_id", lambda root: tasks_by_id)
    monkeypatch.setattr(tournament_store, "_load_completed_runs", lambda root: runs)
    return tasks_by_id, runs


def test_publish_month_persists_and_returns_the_protocol(stub_sources):
    record = tournament_store.publish_month(month="2026-08")

    assert record["month"] == "2026-08"
    assert record["categories"]["Dev"][0]["participant"] == "claude"
    assert record["categories"]["Dev"][0]["completed"] == 2
    assert tournament_store.get_protocol("2026-08") == record


def test_publish_month_is_idempotent_rebuild_not_accumulation(stub_sources, monkeypatch):
    tournament_store.publish_month(month="2026-08")

    tasks_by_id, _ = stub_sources
    new_runs = [
        {"task_id": "t1", "agent": "codex", "state": "COMPLETED", "completed_at": "2026-08-12T09:00:00"},
    ]
    monkeypatch.setattr(tournament_store, "_load_completed_runs", lambda root: new_runs)

    rebuilt = tournament_store.publish_month(month="2026-08")

    assert rebuilt["categories"]["Dev"] == [{"participant": "codex", "completed": 1, "rank": 1}]
    assert tournament_store.get_protocol("2026-08") == rebuilt
    assert len(tournament_store.list_protocols()) == 1


def test_get_protocol_none_for_unpublished_month():
    assert tournament_store.get_protocol("2099-01") is None


def test_ensure_current_month_published_publishes_once(stub_sources, monkeypatch):
    now = datetime(2026, 8, 15)
    calls = []
    original = tournament_store.publish_month

    def counting_publish(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(tournament_store, "publish_month", counting_publish)

    first = tournament_store.ensure_current_month_published(now=now)
    second = tournament_store.ensure_current_month_published(now=now)

    assert first == second
    assert len(calls) == 1


def test_list_protocols_orders_most_recent_month_first(stub_sources):
    tournament_store.publish_month(month="2026-06")
    tournament_store.publish_month(month="2026-08")
    tournament_store.publish_month(month="2026-07")

    months = [record["month"] for record in tournament_store.list_protocols()]
    assert months == ["2026-08", "2026-07", "2026-06"]
