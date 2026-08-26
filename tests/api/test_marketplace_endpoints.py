"""Endpoint tests for the Wave-3 Marketplace surface
(``command_center.api.marketplace_routes`` → ``marketplace.service`` →
``runtime.db.marketplace``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox
and resets its contents between cases, so the runtime db the service writes is
throwaway.

Fixtures use only generic names and invented ids — no real names or paths —
keeping the public-repo privacy gate green.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api.app import create_app
from command_center.marketplace.service import ROOT
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    db.migrate(resolve_db_path(ROOT))


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _register(client: TestClient, **overrides) -> dict:
    payload = {"name": "Thing", "kind": "module", "version": "1.0.0"}
    payload.update(overrides)
    r = client.post("/api/v1/marketplace/items", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# --- register / list / get ------------------------------------------------


def test_register_persists_listed_item(client) -> None:
    body = _register(client, provenance="channel:stable")
    assert body["id"] and body["status"] == "listed"
    assert body["kind"] == "module"
    stored = db.get_market_item(resolve_db_path(ROOT), body["id"])
    assert stored is not None and stored["provenance"] == "channel:stable"


def test_register_rejects_bad_kind(client) -> None:
    # pydantic Literal rejects an unknown kind at the request boundary (422).
    r = client.post("/api/v1/marketplace/items", json={"name": "x", "kind": "nope"})
    assert r.status_code == 422


def test_list_filters_and_pages(client) -> None:
    _register(client, name="a", kind="module")
    _register(client, name="b", kind="plugin")
    _register(client, name="c", kind="domain_pack")

    all_body = client.get("/api/v1/marketplace/items").json()
    assert all_body["limit"] == 100 and all_body["offset"] == 0
    assert len(all_body["items"]) == 3

    by_kind = client.get(
        "/api/v1/marketplace/items", params={"kind": "plugin"}
    ).json()
    assert len(by_kind["items"]) == 1 and by_kind["items"][0]["kind"] == "plugin"

    page = client.get("/api/v1/marketplace/items", params={"limit": 2}).json()
    assert len(page["items"]) == 2


def test_get_item_404_when_absent(client) -> None:
    assert client.get("/api/v1/marketplace/items/nope").status_code == 404


# --- install path + log (acceptance) --------------------------------------


def test_install_transitions_and_is_logged(client) -> None:
    item = _register(client, version="2.0.0", provenance="url:https://ex.test/z")

    r = client.post(
        f"/api/v1/marketplace/items/{item['id']}/install", json={"actor": "alice"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "installed"

    log = client.get(f"/api/v1/marketplace/items/{item['id']}/log").json()
    assert len(log["entries"]) == 1
    entry = log["entries"][0]
    assert entry["actor"] == "alice"
    assert entry["version"] == "2.0.0"
    assert entry["provenance"] == "url:https://ex.test/z"
    assert entry["installer"] == "null-installer"
    assert entry["installed_at"]


def test_install_is_idempotent_over_http(client) -> None:
    item = _register(client)
    path = f"/api/v1/marketplace/items/{item['id']}/install"

    assert client.post(path, json={"actor": "alice"}).status_code == 200
    assert client.post(path, json={"actor": "alice"}).status_code == 200

    log = client.get(f"/api/v1/marketplace/items/{item['id']}/log").json()
    assert len(log["entries"]) == 1  # no duplicate trail line


def test_install_missing_item_404(client) -> None:
    r = client.post(
        "/api/v1/marketplace/items/nope/install", json={"actor": "alice"}
    )
    assert r.status_code == 404


def test_install_requires_actor(client) -> None:
    item = _register(client)
    r = client.post(f"/api/v1/marketplace/items/{item['id']}/install", json={})
    assert r.status_code == 422  # actor is required by the request contract


def test_log_404_when_item_absent(client) -> None:
    assert client.get("/api/v1/marketplace/items/nope/log").status_code == 404
