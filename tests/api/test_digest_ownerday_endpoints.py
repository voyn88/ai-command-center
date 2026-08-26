"""Endpoint tests for the Wave-1 Дайджест build + «Мой день» complete surface
(``command_center.api.wave1_routes`` → ``wave1_service`` → digest engine → repo).

Hermetic: ``tests/conftest.py`` sandboxes ``AICC_DATA_DIR``; the digest sources
are stubbed so build output is deterministic, and a fresh :class:`EventBus` is
installed per test so ``DigestReady`` is observable and never leaks.

Fixtures use only invented ids and the generic project code ``AICC``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api.app import create_app
from command_center.digest import service as digest_service
from command_center.events import DigestReady, Event, default_bus


@pytest.fixture
def stub_sources(monkeypatch) -> None:
    src = digest_service.sources
    monkeypatch.setattr(src, "overnight_runs", lambda **_: [
        {"ref": "run:r1", "title": "impl", "detail": "COMPLETED", "project": "AICC", "ts": "x"},
    ])
    monkeypatch.setattr(src, "recent_commits", lambda **_: [])
    monkeypatch.setattr(src, "open_proposals", lambda **_: [
        {"ref": "proposal:p1", "title": "Adopt X", "detail": "trend", "project": "AICC", "ts": "x"},
    ])
    monkeypatch.setattr(src, "attention_items", lambda **_: [])
    monkeypatch.setattr(src, "agent_status", lambda **_: {
        "running": 0, "queued": 0, "attention": 0, "total": 0, "available": True})


@pytest.fixture
def events() -> list[Event]:
    captured: list[Event] = []
    bus = default_bus()
    bus.clear()
    off = bus.subscribe(Event, captured.append)
    try:
        yield captured
    finally:
        off()
        bus.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_build_returns_ordered_actionable_items_and_emits(client, stub_sources, events) -> None:
    r = client.post("/api/v1/digest/build")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["category"] for i in items] == ["overnight", "advisor", "status"]
    assert all(i["refs"] for i in items)  # actionable

    ready = [e for e in events if isinstance(e, DigestReady)]
    assert len(ready) == len(items)


def test_today_reads_back_the_built_digest(client, stub_sources) -> None:
    built = client.post("/api/v1/digest/build").json()["items"]
    today = client.get("/api/v1/digest/today").json()["items"]
    assert [i["id"] for i in today] == [i["id"] for i in built]


def test_today_is_not_shadowed_by_item_id_route(client, stub_sources) -> None:
    # "/digest/today" must resolve to the list endpoint, not "/digest/{item_id}".
    r = client.get("/api/v1/digest/today")
    assert r.status_code == 200
    assert "items" in r.json()


def test_rebuild_is_idempotent_over_the_api(client, stub_sources) -> None:
    first = client.post("/api/v1/digest/build").json()["items"]
    client.post("/api/v1/digest/build")
    today = client.get("/api/v1/digest/today").json()["items"]
    assert len(today) == len(first)
    assert [i["title"] for i in today] == [i["title"] for i in first]


def test_owner_item_complete_flow(client) -> None:
    created = client.post("/api/v1/owner-items", json={"title": "Do X"})
    assert created.status_code == 201
    item_id = created.json()["id"]
    assert created.json()["done"] is False

    done = client.post(f"/api/v1/owner-items/{item_id}/complete")
    assert done.status_code == 200 and done.json()["done"] is True

    # Idempotent: completing again still returns the done item.
    again = client.post(f"/api/v1/owner-items/{item_id}/complete")
    assert again.status_code == 200 and again.json()["done"] is True


def test_complete_missing_owner_item_is_404(client) -> None:
    assert client.post("/api/v1/owner-items/nope/complete").status_code == 404
