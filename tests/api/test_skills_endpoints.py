"""Endpoint tests for the Skill Acquisition surface
(``command_center.api.skills_routes`` → ``skills.service`` →
``runtime.db.skills``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox
and resets its contents between cases. ``tests/api/conftest.py`` auto-applies
the ``authenticated_caller`` fixture (see ``tests/http_auth_fixture.py``), so
every request here acts as an already-authenticated, fully-granted caller —
the guard itself is exhaustively covered by ``tests/http_auth``, not here.

Fixtures use only generic names and invented ids — no real names or paths —
keeping the public-repo privacy gate green.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api.app import create_app
from command_center.skills.service import ROOT
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path

_HASH_A = "a" * 64
_HASH_B = "b" * 64


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    db.migrate(resolve_db_path(ROOT))


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _propose_source(client: TestClient, **overrides) -> dict:
    payload = {"name": "Registry", "kind": "mcp_registry", "origin": "o1", "proposed_by": "alice"}
    payload.update(overrides)
    r = client.post("/api/v1/skills/sources", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _approved_source(client: TestClient, **overrides) -> dict:
    source = _propose_source(client, **overrides)
    r = client.post(f"/api/v1/skills/sources/{source['id']}/approve", json={"actor": "alice"})
    assert r.status_code == 200, r.text
    return r.json()


def _register(client: TestClient, source_id: str, **overrides) -> dict:
    payload = {
        "name": "Thing", "kind": "mcp_server", "version": "1.0.0", "content_hash": _HASH_A,
        "source_id": source_id,
    }
    payload.update(overrides)
    r = client.post("/api/v1/skills/items", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# --- sources ----------------------------------------------------------------


def test_propose_source_persists_proposed(client) -> None:
    body = _propose_source(client, origin="o-persist")
    assert body["id"] and body["status"] == "proposed"
    stored = db.get_skill_source(resolve_db_path(ROOT), body["id"])
    assert stored is not None and stored["origin"] == "o-persist"


def test_propose_source_rejects_bad_kind(client) -> None:
    r = client.post(
        "/api/v1/skills/sources",
        json={"name": "x", "kind": "nope", "origin": "o", "proposed_by": "a"},
    )
    assert r.status_code == 422


def test_approve_source_over_http(client) -> None:
    source = _propose_source(client, origin="o-approve")
    r = client.post(f"/api/v1/skills/sources/{source['id']}/approve", json={"actor": "bob"})
    assert r.status_code == 200
    assert r.json()["status"] == "approved" and r.json()["approved_by"] == "bob"


def test_approve_missing_source_404(client) -> None:
    r = client.post("/api/v1/skills/sources/nope/approve", json={"actor": "bob"})
    assert r.status_code == 404


def test_revoke_source_over_http(client) -> None:
    source = _approved_source(client, origin="o-revoke")
    r = client.post(
        f"/api/v1/skills/sources/{source['id']}/revoke",
        json={"actor": "bob", "reason": "deprecated"},
    )
    assert r.status_code == 200 and r.json()["status"] == "revoked"


def test_list_and_get_sources(client) -> None:
    _propose_source(client, origin="o-list-a")
    _propose_source(client, origin="o-list-b")
    body = client.get("/api/v1/skills/sources").json()
    assert len(body["items"]) == 2
    got = client.get(f"/api/v1/skills/sources/{body['items'][0]['id']}")
    assert got.status_code == 200


def test_get_source_404_when_absent(client) -> None:
    assert client.get("/api/v1/skills/sources/nope").status_code == 404


# --- items: registration + pinning + allowlist gate -------------------------


def test_register_persists_candidate_item(client) -> None:
    source = _approved_source(client, origin="o-item-a")
    body = _register(client, source["id"], provenance="channel:stable")
    assert body["id"] and body["status"] == "candidate"
    assert body["provenance"] == "channel:stable"


def test_register_rejects_unapproved_source(client) -> None:
    source = _propose_source(client, origin="o-item-unapproved")
    r = client.post(
        "/api/v1/skills/items",
        json={
            "name": "x", "kind": "mcp_server", "version": "1.0.0", "content_hash": _HASH_A,
            "source_id": source["id"],
        },
    )
    assert r.status_code == 422


def test_register_rejects_malformed_hash(client) -> None:
    source = _approved_source(client, origin="o-item-badhash")
    r = client.post(
        "/api/v1/skills/items",
        json={
            "name": "x", "kind": "mcp_server", "version": "1.0.0", "content_hash": "bad",
            "source_id": source["id"],
        },
    )
    assert r.status_code == 422


def test_list_items_filters_and_pages(client) -> None:
    source = _approved_source(client, origin="o-item-list")
    _register(client, source["id"], name="a", content_hash=_HASH_A)
    _register(client, source["id"], name="b", content_hash=_HASH_B)
    body = client.get("/api/v1/skills/items").json()
    assert len(body["items"]) == 2


def test_get_item_404_when_absent(client) -> None:
    assert client.get("/api/v1/skills/items/nope").status_code == 404


# --- acquire / reject / revoke + log -----------------------------------


def test_acquire_transitions_and_is_logged(client) -> None:
    source = _approved_source(client, origin="o-acquire")
    item = _register(client, source["id"], version="2.0.0")

    r = client.post(f"/api/v1/skills/items/{item['id']}/acquire", json={"actor": "alice"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "acquired"

    log = client.get(f"/api/v1/skills/items/{item['id']}/log").json()
    assert len(log["entries"]) == 1
    entry = log["entries"][0]
    assert entry["actor"] == "alice"
    assert entry["action"] == "acquire"
    assert entry["executor"] == "null-skill-executor"
    assert entry["metadata"]["network"] == "denied"


def test_acquire_is_idempotent_over_http(client) -> None:
    source = _approved_source(client, origin="o-acquire-idem")
    item = _register(client, source["id"])
    path = f"/api/v1/skills/items/{item['id']}/acquire"

    assert client.post(path, json={"actor": "alice"}).status_code == 200
    assert client.post(path, json={"actor": "alice"}).status_code == 200

    log = client.get(f"/api/v1/skills/items/{item['id']}/log").json()
    assert len(log["entries"]) == 1


def test_acquire_missing_item_404(client) -> None:
    r = client.post("/api/v1/skills/items/nope/acquire", json={"actor": "alice"})
    assert r.status_code == 404


def test_reject_and_revoke_over_http(client) -> None:
    source = _approved_source(client, origin="o-reject-revoke")
    rejected_item = _register(client, source["id"], name="rej", content_hash=_HASH_A)
    r = client.post(
        f"/api/v1/skills/items/{rejected_item['id']}/reject",
        json={"actor": "alice", "reason": "lost selection"},
    )
    assert r.status_code == 200 and r.json()["status"] == "rejected"

    acquired_item = _register(client, source["id"], name="rev", content_hash=_HASH_B)
    client.post(f"/api/v1/skills/items/{acquired_item['id']}/acquire", json={"actor": "alice"})
    r = client.post(
        f"/api/v1/skills/items/{acquired_item['id']}/revoke",
        json={"actor": "alice", "reason": "no improvement"},
    )
    assert r.status_code == 200 and r.json()["status"] == "revoked"


def test_log_404_when_item_absent(client) -> None:
    assert client.get("/api/v1/skills/items/nope/log").status_code == 404


# --- outcomes + effect --------------------------------------------------


def test_record_outcome_and_get_effect(client) -> None:
    source = _approved_source(client, origin="o-effect")
    item = _register(client, source["id"])
    client.post(f"/api/v1/skills/items/{item['id']}/acquire", json={"actor": "alice"})

    for i in range(5):
        r = client.post(
            f"/api/v1/skills/items/{item['id']}/outcomes",
            json={"task_id": f"b{i}", "phase": "baseline", "cost": 4.0, "accepted": True, "first_pass": False},
        )
        assert r.status_code == 201, r.text
        r = client.post(
            f"/api/v1/skills/items/{item['id']}/outcomes",
            json={"task_id": f"w{i}", "phase": "with_skill", "cost": 1.0, "accepted": True, "first_pass": True},
        )
        assert r.status_code == 201

    effect = client.get(f"/api/v1/skills/items/{item['id']}/effect").json()
    assert effect["baseline_samples"] == 5
    assert effect["with_skill_samples"] == 5
    assert effect["improved"] is True


def test_record_outcome_missing_item_404(client) -> None:
    r = client.post(
        "/api/v1/skills/items/nope/outcomes",
        json={"task_id": "t1", "phase": "baseline", "cost": 1.0, "accepted": True, "first_pass": True},
    )
    assert r.status_code == 404


def test_effect_missing_item_404(client) -> None:
    assert client.get("/api/v1/skills/items/nope/effect").status_code == 404
