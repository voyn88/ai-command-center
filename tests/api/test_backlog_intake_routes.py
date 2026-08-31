"""Endpoint tests for chat-text backlog intake (VOYN-W0-APP-CONTROL-S6a).

`BacklogStore` and the model call are both faked — the SQL-level behaviour of
`backlog_upsert_task` is already proven in `tests/db/test_backlog_store.py`
against a real PostgreSQL, and the grammar itself is proven hermetically in
`tests/db/test_backlog_intake.py`. These tests cover the HTTP-layer glue:
draft/confirm shape, the never-overwrite-an-existing-task guard, and the
422/503 refusal paths.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import command_center.api.backlog_intake_routes as intake_routes
from command_center.api.app import create_app
from command_center.db.pool import PoolNotOpenError

_VALID_LINE = (
    "- **VOYN-W0-APP-CONTROL-S9** | Wave 0 | UNTRIAGED | P1 | "
    "`voice-chat-intake` | Let the owner file a task by typing a sentence."
)


class _FakeStore:
    def __init__(self, existing=None, upsert_result=(True, "inserted", True)):
        self._existing = existing or {}
        self._upsert_result = upsert_result
        self.upserted = []

    def get_task(self, task_id):
        return self._existing.get(task_id)

    def upsert_task(self, task):
        self.upserted.append(task)
        return self._upsert_result


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


# -- draft --------------------------------------------------------------------


def test_draft_returns_the_parsed_task_for_a_well_formed_model_reply(client, monkeypatch):
    monkeypatch.setattr(intake_routes, "_call_model", lambda prompt: _VALID_LINE)
    resp = client.post("/api/v1/backlog/intake/draft", json={"text": "file a task"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["task"]["task_id"] == "VOYN-W0-APP-CONTROL-S9"
    assert body["task"]["status"] == "UNTRIAGED"
    assert body["line"] == _VALID_LINE


def test_draft_surfaces_a_refusal_instead_of_guessing(client, monkeypatch):
    monkeypatch.setattr(intake_routes, "_call_model", lambda prompt: "not a task line at all")
    resp = client.post("/api/v1/backlog/intake/draft", json={"text": "file a task"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["reason"]
    assert "raw_output" in body


def test_draft_requires_nonempty_text(client, monkeypatch):
    monkeypatch.setattr(intake_routes, "_call_model", lambda prompt: _VALID_LINE)
    resp = client.post("/api/v1/backlog/intake/draft", json={"text": "   "})
    assert resp.status_code == 422


def test_draft_propagates_a_failed_model_call(client, monkeypatch):
    def _fail(prompt):
        raise HTTPException(status_code=502, detail="model timed out")

    monkeypatch.setattr(intake_routes, "_call_model", _fail)
    resp = client.post("/api/v1/backlog/intake/draft", json={"text": "file a task"})
    assert resp.status_code == 502


# -- confirm --------------------------------------------------------------------


def test_confirm_inserts_a_new_task(client, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(intake_routes, "_write_store", lambda: store)
    resp = client.post("/api/v1/backlog/intake/confirm", json={"line": _VALID_LINE})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"task_id": "VOYN-W0-APP-CONTROL-S9", "reason": "inserted", "changed": True}
    assert store.upserted[0].task_id == "VOYN-W0-APP-CONTROL-S9"
    assert store.upserted[0].status == "UNTRIAGED"


def test_confirm_refuses_a_line_the_grammar_rejects(client, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(intake_routes, "_write_store", lambda: store)
    resp = client.post(
        "/api/v1/backlog/intake/confirm", json={"line": "not a task line at all"}
    )
    assert resp.status_code == 422
    assert store.upserted == []


def test_confirm_never_overwrites_an_existing_task(client, monkeypatch):
    store = _FakeStore(existing={"VOYN-W0-APP-CONTROL-S9": {"task_id": "VOYN-W0-APP-CONTROL-S9"}})
    monkeypatch.setattr(intake_routes, "_write_store", lambda: store)
    resp = client.post("/api/v1/backlog/intake/confirm", json={"line": _VALID_LINE})
    assert resp.status_code == 409
    assert store.upserted == [], "an existing task must never be silently rewritten"


def test_confirm_surfaces_a_sql_level_refusal(client, monkeypatch):
    store = _FakeStore(upsert_result=(False, "constraint: bad wave", False))
    monkeypatch.setattr(intake_routes, "_write_store", lambda: store)
    resp = client.post("/api/v1/backlog/intake/confirm", json={"line": _VALID_LINE})
    assert resp.status_code == 422
    assert "constraint: bad wave" in resp.json()["detail"]


def test_confirm_503s_when_the_backlog_is_not_configured(client, monkeypatch):
    class _Unconfigured:
        def get_task(self, task_id):
            raise PoolNotOpenError("AICC_PG_HOST unset")

    monkeypatch.setattr(intake_routes, "_write_store", lambda: _Unconfigured())
    resp = client.post("/api/v1/backlog/intake/confirm", json={"line": _VALID_LINE})
    assert resp.status_code == 503


def test_confirm_requires_nonempty_line(client, monkeypatch):
    store = _FakeStore()
    monkeypatch.setattr(intake_routes, "_write_store", lambda: store)
    resp = client.post("/api/v1/backlog/intake/confirm", json={"line": ""})
    assert resp.status_code == 422
    assert store.upserted == []
