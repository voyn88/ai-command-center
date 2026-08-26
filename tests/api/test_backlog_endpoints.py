"""Endpoint tests for the Postgres-backed backlog surface
(``command_center.api.backlog_routes`` -> ``backlog_service`` ->
``command_center.db.backlog_store.BacklogStore``).

The SQL-level behaviour (status machine, evidence gate, cascade exhaustion)
is already proven against a real PostgreSQL in ``tests/db/*`` — these tests
cover the HTTP-layer glue only (request/response shape, 404, 503-when-
unconfigured), so ``BacklogStore`` is faked rather than requiring a live
database, matching the existing endpoint tests in this package
(``test_audit_endpoints.py`` fakes the check registry for the same reason).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api import backlog_service
from command_center.api.app import create_app
from command_center.db.pool import PoolNotOpenError


class _FakeStore:
    def __init__(self, tasks=None, events=None, evidence=None):
        self._tasks = tasks or []
        self._events = events or []
        self._evidence = evidence or []

    def counts_by_status(self):
        counts: dict[str, int] = {}
        for t in self._tasks:
            counts[t["status"]] = counts.get(t["status"], 0) + 1
        return counts

    def list_tasks(self, *, status=None, limit=100, offset=0):
        rows = [t for t in self._tasks if status is None or t["status"] == status]
        return rows[offset : offset + limit], len(rows)

    def get_task(self, task_id):
        for t in self._tasks:
            if t["task_id"] == task_id:
                return t
        return None

    def list_events(self, task_id, *, limit=200):
        return self._events

    def list_evidence(self, task_id):
        return self._evidence


def _task(task_id="VOYN-W0-X", status="OPEN"):
    return {
        "task_id": task_id,
        "wave": "0",
        "priority": "P1",
        "status": status,
        "kind": "task",
        "title": "a finding",
        "repo": "ai-command-center",
        "revision": 1,
    }


@pytest.fixture
def client():
    return TestClient(create_app())


def test_status_counts_zero_fills_the_full_vocabulary(client, monkeypatch):
    monkeypatch.setattr(
        backlog_service,
        "BacklogStore",
        lambda *a, **k: _FakeStore(tasks=[_task(status="OPEN"), _task("VOYN-W0-Y", "DONE")]),
    )
    resp = client.get("/api/v1/backlog/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["counts"]["OPEN"] == 1
    assert body["counts"]["DONE"] == 1
    assert body["counts"]["DEFER_TO_USER"] == 0  # zero-filled, not absent
    assert body["total"] == 2


def test_tasks_filters_by_status(client, monkeypatch):
    monkeypatch.setattr(
        backlog_service,
        "BacklogStore",
        lambda *a, **k: _FakeStore(
            tasks=[_task("VOYN-A", "OPEN"), _task("VOYN-B", "DEFER_TO_USER")]
        ),
    )
    resp = client.get("/api/v1/backlog/tasks", params={"status": "DEFER_TO_USER"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert [t["task_id"] for t in body["tasks"]] == ["VOYN-B"]


def test_tasks_limit_is_bounded_server_side(client, monkeypatch):
    monkeypatch.setattr(
        backlog_service,
        "BacklogStore",
        lambda *a, **k: _FakeStore(tasks=[_task(f"VOYN-{i}") for i in range(3)]),
    )
    resp = client.get("/api/v1/backlog/tasks", params={"limit": 999999})
    assert resp.status_code == 422  # FastAPI's own Query(le=MAX_LIMIT) rejects it


def test_task_detail_includes_events_and_evidence(client, monkeypatch):
    monkeypatch.setattr(
        backlog_service,
        "BacklogStore",
        lambda *a, **k: _FakeStore(
            tasks=[_task("VOYN-W0-X")],
            events=[
                {
                    "event": "transition",
                    "outcome": "granted",
                    "reason": None,
                    "actor": "aicc_admin",
                    "detail": {"to": "OPEN"},
                    "created_at": "2026-08-20T09:00:00+00:00",
                }
            ],
            evidence=[
                {
                    "kind": "pr",
                    "value": "https://github.com/o/r/pull/1",
                    "recorded_at": "2026-08-20T09:05:00+00:00",
                }
            ],
        ),
    )
    resp = client.get("/api/v1/backlog/tasks/VOYN-W0-X")
    assert resp.status_code == 200
    body = resp.json()
    assert body["task"]["task_id"] == "VOYN-W0-X"
    assert len(body["events"]) == 1
    assert body["evidence"][0]["kind"] == "pr"


def test_unknown_task_is_404(client, monkeypatch):
    monkeypatch.setattr(backlog_service, "BacklogStore", lambda *a, **k: _FakeStore())
    resp = client.get("/api/v1/backlog/tasks/VOYN-NOPE")
    assert resp.status_code == 404


def test_unconfigured_backlog_is_503_not_a_silent_empty_page(client, monkeypatch):
    """A desktop shell pointed at a server with no AICC_PG_* must see a clear
    503, not an empty-but-200 dashboard that reads as "backlog is empty"."""

    def _raise(*a, **k):
        raise PoolNotOpenError("PostgreSQL pool is not open.")

    monkeypatch.setattr(backlog_service, "BacklogStore", _raise)
    resp = client.get("/api/v1/backlog/status")
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"]
