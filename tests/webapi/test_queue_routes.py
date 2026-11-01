"""Hermetic tests for `/api/v1/queue/*` on the accepted auth boundary.

Auth model (post-#316 rework of #325): the POST rides the http_auth routing
table + deny-by-default grants — its unauthenticated-401 and
credential-without-grant-403 proofs live with the rest of the mutating
surface in `tests/http_auth/test_routing_coverage.py`, which walks the live
app (EXPECTED_MUTATING_ROUTES now counts it). This package's autouse
`authenticated_caller` fixture (see conftest) makes every request here an
authenticated, fully granted principal, so these tests are about queue
semantics — plus the one wiring fact routing coverage cannot see: the two
GET reads authenticate through `routing.authenticate` even though reads are
outside the mutating table (the recorded AUTH-HTTP-02 asymmetry).

The stores are module globals in `command_center.webapi.queue_routes`,
monkeypatched per test — no PostgreSQL. The Python-to-SQL seam is proven by
`tests/db/test_work_queue_read.py` against a real server.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import command_center.webapi.queue_routes as qmod
from command_center.http_auth import routing
from command_center.webapi.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


class FakeReadStore:
    def __init__(self, items=None, item=None):
        self.items = items or []
        self.item = item
        self.calls = []

    def list_items(self, *, queue=None, state=None, limit=100):
        self.calls.append(("list", queue, state, limit))
        return self.items

    def get_item(self, work_item_id):
        self.calls.append(("get", work_item_id))
        return self.item


class FakeWriteStore:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, queue, *, idempotency_key, payload, **kwargs):
        self.enqueued.append((queue, idempotency_key, payload, kwargs))
        return "wki_new"


# -- read authentication wiring (what routing coverage cannot see) ------------


@pytest.mark.parametrize("path", ["/api/v1/queue/items", "/api/v1/queue/items/wki_1"])
def test_a_read_refused_by_the_platform_is_a_401_here(client, monkeypatch, path):
    """The GETs must consult `routing.authenticate` — reads are outside the
    mutating table, so their gate is this dependency and nothing else."""

    def refuse(request):
        raise HTTPException(status_code=401, detail="unauthenticated")

    monkeypatch.setattr(routing, "authenticate", refuse)
    monkeypatch.setattr(qmod, "_read_store", lambda: FakeReadStore())
    assert client.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/api/v1/queue/items", "/api/v1/queue/items/wki_1"])
def test_every_read_authenticates_exactly_once(client, monkeypatch, path):
    seen = []
    monkeypatch.setattr(routing, "authenticate", lambda request: seen.append(path))
    monkeypatch.setattr(
        qmod, "_read_store", lambda: FakeReadStore(item={"work_item_id": "wki_1"})
    )
    client.get(path)
    assert len(seen) == 1


# -- the queue reads ----------------------------------------------------------


def test_list_items_passes_filters_through(client, monkeypatch):
    store = FakeReadStore(items=[{"work_item_id": "wki_1", "state": "ready"}])
    monkeypatch.setattr(qmod, "_read_store", lambda: store)
    response = client.get("/api/v1/queue/items?state=ready&queue=execution&limit=7")
    assert response.status_code == 200
    assert response.json() == {"items": [{"work_item_id": "wki_1", "state": "ready"}]}
    assert store.calls == [("list", "execution", "ready", 7)]


def test_item_detail_and_404(client, monkeypatch):
    detail = {
        "work_item_id": "wki_1",
        "state": "succeeded",
        "attempts": [],
        "result": {"ok": 1},
    }
    monkeypatch.setattr(qmod, "_read_store", lambda: FakeReadStore(item=detail))
    assert client.get("/api/v1/queue/items/wki_1").json() == detail

    monkeypatch.setattr(qmod, "_read_store", lambda: FakeReadStore(item=None))
    assert client.get("/api/v1/queue/items/wki_nope").status_code == 404


# -- the audit enqueue (S4 start) ---------------------------------------------

_AUDIT_BODY = {
    "project_id": "aicc",
    "repository_path": "/repos/aicc",
    "prompt": "Проверь качество и риски проекта",
}


def test_audit_enqueues_a_read_only_trusted_agent_run(client, monkeypatch):
    store = FakeWriteStore()
    monkeypatch.setattr(qmod, "_write_store", lambda: store)
    response = client.post("/api/v1/queue/audit", json=_AUDIT_BODY)
    assert response.status_code == 200
    assert response.json()["work_item_id"] == "wki_new"

    (queue, _key, payload, _kwargs) = store.enqueued[0]
    assert queue == "execution"
    # The safety pins: review profile (read-only sandbox by the runner's own
    # classification) and trusted provenance from the authorized principal.
    assert payload["kind"] == "agent_run" and payload["v"] == 1
    assert payload["task_type"] == "review"
    assert payload["untrusted"] is False
    assert payload["project_id"] == "aicc"


def test_audit_idempotency_key_is_deterministic(client, monkeypatch):
    """The same audit request twice must produce the same key — that is what
    lets queue_enqueue's (queue, key) upsert absorb a double submit."""
    store = FakeWriteStore()
    monkeypatch.setattr(qmod, "_write_store", lambda: store)
    first = client.post("/api/v1/queue/audit", json=_AUDIT_BODY).json()
    second = client.post("/api/v1/queue/audit", json=_AUDIT_BODY).json()
    assert first["idempotency_key"] == second["idempotency_key"]
    assert store.enqueued[0][1] == store.enqueued[1][1]

    explicit = dict(_AUDIT_BODY, idempotency_key="my-key")
    third = client.post("/api/v1/queue/audit", json=explicit).json()
    assert third["idempotency_key"] == "my-key"


def test_audit_refuses_incomplete_requests(client, monkeypatch):
    store = FakeWriteStore()
    monkeypatch.setattr(qmod, "_write_store", lambda: store)
    response = client.post("/api/v1/queue/audit", json={"project_id": "aicc"})
    assert response.status_code == 422
    assert store.enqueued == [], "nothing may reach the queue on a refusal"


def test_audit_resolves_repository_path_from_project_config_when_omitted(
    client, monkeypatch
):
    """The one-button UI trigger only knows a project_id — the server must
    resolve the same path the worker will later confirm the run against
    (`agent_runner.validate_repository`), not force the caller to know it."""
    store = FakeWriteStore()
    monkeypatch.setattr(qmod, "_write_store", lambda: store)
    monkeypatch.setattr(
        qmod.project_config,
        "get_project_config",
        lambda project_id: {"repository_path": "/configured/aicc"},
    )
    body = {"project_id": "aicc", "prompt": "Проверь качество и риски проекта"}
    response = client.post("/api/v1/queue/audit", json=body)
    assert response.status_code == 200

    (_queue, _key, payload, _kwargs) = store.enqueued[0]
    assert payload["repository_path"] == "/configured/aicc"


def test_audit_refuses_when_repository_path_not_configured(client, monkeypatch):
    store = FakeWriteStore()
    monkeypatch.setattr(qmod, "_write_store", lambda: store)
    monkeypatch.setattr(
        qmod.project_config,
        "get_project_config",
        lambda project_id: {"repository_path": None},
    )
    body = {"project_id": "aicc", "prompt": "Проверь качество и риски проекта"}
    response = client.post("/api/v1/queue/audit", json=body)
    assert response.status_code == 422
    assert store.enqueued == [], "nothing may reach the queue on a refusal"


def test_audit_explicit_repository_path_still_honored(client, monkeypatch):
    """An explicit repository_path in the body must win over project config —
    existing callers (and the previous request shape) stay unaffected."""
    store = FakeWriteStore()
    monkeypatch.setattr(qmod, "_write_store", lambda: store)
    monkeypatch.setattr(
        qmod.project_config,
        "get_project_config",
        lambda project_id: {"repository_path": "/configured/aicc"},
    )
    response = client.post("/api/v1/queue/audit", json=_AUDIT_BODY)
    assert response.status_code == 200

    (_queue, _key, payload, _kwargs) = store.enqueued[0]
    assert payload["repository_path"] == _AUDIT_BODY["repository_path"]
