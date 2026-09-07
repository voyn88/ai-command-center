"""Endpoint tests for priority/wave reassignment (VOYN-W0-APP-CONTROL-S6d).

`BacklogStore` is faked — the SQL-level behaviour of `backlog_reassign` is
already proven in `tests/db/test_backlog_store.py` against a real
PostgreSQL. These tests cover the HTTP-layer glue: request-shape validation,
the optimistic-revision 409, and the 404/422/503 refusal paths.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import command_center.api.backlog_reassign_routes as reassign_routes
from command_center.api.app import create_app
from command_center.db.pool import PoolNotOpenError


class _FakeStore:
    def __init__(self, reassign_result=(True, "reassigned", 2)):
        self._reassign_result = reassign_result
        self.calls = []

    def reassign(self, task_id, wave, priority, expected_revision):
        self.calls.append((task_id, wave, priority, expected_revision))
        return self._reassign_result


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_reassign_moves_wave_and_priority(client, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(reassign_routes, "_write_store", lambda: store)
    resp = client.post(
        "/api/v1/backlog/tasks/VOYN-W0-APP-CONTROL-S6d/reassign",
        json={"wave": "1", "priority": "P2", "expected_revision": 1},
    )
    assert resp.status_code == 200
    assert resp.json() == {
        "task_id": "VOYN-W0-APP-CONTROL-S6d",
        "reason": "reassigned",
        "revision": 2,
    }
    assert store.calls == [("VOYN-W0-APP-CONTROL-S6d", "1", "P2", 1)]


def test_reassign_accepts_a_null_priority(client, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(reassign_routes, "_write_store", lambda: store)
    resp = client.post(
        "/api/v1/backlog/tasks/VOYN-W0-APP-CONTROL-S6d/reassign",
        json={"wave": "1", "priority": None, "expected_revision": 1},
    )
    assert resp.status_code == 200
    assert store.calls == [("VOYN-W0-APP-CONTROL-S6d", "1", None, 1)]


def test_reassign_requires_nonempty_wave(client, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(reassign_routes, "_write_store", lambda: store)
    resp = client.post(
        "/api/v1/backlog/tasks/VOYN-W0-APP-CONTROL-S6d/reassign",
        json={"wave": "  ", "priority": "P2", "expected_revision": 1},
    )
    assert resp.status_code == 422
    assert store.calls == []


def test_reassign_requires_priority_to_be_a_string_or_null(client, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(reassign_routes, "_write_store", lambda: store)
    resp = client.post(
        "/api/v1/backlog/tasks/VOYN-W0-APP-CONTROL-S6d/reassign",
        json={"wave": "1", "priority": 2, "expected_revision": 1},
    )
    assert resp.status_code == 422
    assert store.calls == []


def test_reassign_requires_an_integer_expected_revision(client, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(reassign_routes, "_write_store", lambda: store)
    resp = client.post(
        "/api/v1/backlog/tasks/VOYN-W0-APP-CONTROL-S6d/reassign",
        json={"wave": "1", "priority": "P2", "expected_revision": "1"},
    )
    assert resp.status_code == 422
    assert store.calls == []


def test_reassign_refuses_an_unknown_task(client, monkeypatch):
    store = _FakeStore(reassign_result=(False, "unknown_task", None))
    monkeypatch.setattr(reassign_routes, "_write_store", lambda: store)
    resp = client.post(
        "/api/v1/backlog/tasks/VOYN-W0-NO-SUCH-TASK/reassign",
        json={"wave": "1", "priority": "P2", "expected_revision": 1},
    )
    assert resp.status_code == 404


def test_reassign_reports_a_stale_revision_as_a_conflict(client, monkeypatch):
    store = _FakeStore(reassign_result=(False, "revision_conflict", 5))
    monkeypatch.setattr(reassign_routes, "_write_store", lambda: store)
    resp = client.post(
        "/api/v1/backlog/tasks/VOYN-W0-APP-CONTROL-S6d/reassign",
        json={"wave": "1", "priority": "P2", "expected_revision": 1},
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"reason": "revision_conflict", "revision": 5}


def test_reassign_surfaces_a_sql_level_refusal(client, monkeypatch):
    store = _FakeStore(reassign_result=(False, "constraint: bad wave", 1))
    monkeypatch.setattr(reassign_routes, "_write_store", lambda: store)
    resp = client.post(
        "/api/v1/backlog/tasks/VOYN-W0-APP-CONTROL-S6d/reassign",
        json={"wave": "not-a-wave", "priority": "P2", "expected_revision": 1},
    )
    assert resp.status_code == 422
    assert "constraint: bad wave" in resp.json()["detail"]


def test_reassign_503s_when_the_backlog_is_not_configured(client, monkeypatch):
    class _Unconfigured:
        def reassign(self, task_id, wave, priority, expected_revision):
            raise PoolNotOpenError("AICC_PG_HOST unset")

    monkeypatch.setattr(reassign_routes, "_write_store", lambda: _Unconfigured())
    resp = client.post(
        "/api/v1/backlog/tasks/VOYN-W0-APP-CONTROL-S6d/reassign",
        json={"wave": "1", "priority": "P2", "expected_revision": 1},
    )
    assert resp.status_code == 503
