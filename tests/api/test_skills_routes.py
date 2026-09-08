"""Endpoint tests for the skill-acquisition surface
(``command_center.api.skills_routes`` -> ``skills.service`` ->
``runtime.db.skills``).

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
from command_center.runtime import db
from command_center.skills.service import ROOT

_HASH_A = "sha256:" + "a" * 64
_HASH_B = "sha256:" + "b" * 64


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    from command_center.runtime.db.core import resolve_db_path

    db.migrate(resolve_db_path(ROOT))


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _propose_source(client: TestClient, **overrides) -> dict:
    payload = {"kind": "mcp_registry", "origin": "https://example.test/registry"}
    payload.update(overrides)
    r = client.post("/api/v1/skills/sources", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _approved_source(client: TestClient, **overrides) -> dict:
    source = _propose_source(client, **overrides)
    r = client.post(
        f"/api/v1/skills/sources/{source['id']}/approve",
        json={"expected_version": 0, "actor": "alice"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def _register(client: TestClient, source_id: str, **overrides) -> dict:
    payload = {
        "source_id": source_id,
        "name": "fetcher",
        "kind": "mcp_server",
        "content_hash": _HASH_A,
    }
    payload.update(overrides)
    r = client.post("/api/v1/skills/items", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# --- sources: propose / approve / revoke / get / list -----------------------


def test_propose_source_is_always_proposed(client) -> None:
    body = _propose_source(client)
    assert body["id"] and body["status"] == "proposed"
    assert body["kind"] == "mcp_registry"


def test_propose_source_rejects_bad_kind(client) -> None:
    r = client.post(
        "/api/v1/skills/sources", json={"kind": "nope", "origin": "o-1"}
    )
    assert r.status_code == 422


def test_propose_source_rejects_duplicate_origin(client) -> None:
    _propose_source(client, origin="dup")
    r = client.post(
        "/api/v1/skills/sources", json={"kind": "mcp_registry", "origin": "dup"}
    )
    assert r.status_code == 422


def test_get_source_404_when_absent(client) -> None:
    assert client.get("/api/v1/skills/sources/nope").status_code == 404


def test_approve_source_is_the_human_gate(client) -> None:
    source = _propose_source(client)
    approved = _approved_source_from(client, source)
    assert approved["status"] == "approved"


def _approved_source_from(client: TestClient, source: dict) -> dict:
    r = client.post(
        f"/api/v1/skills/sources/{source['id']}/approve",
        json={"expected_version": source["lock_version"], "actor": "alice"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_approve_source_missing_404(client) -> None:
    r = client.post(
        "/api/v1/skills/sources/nope/approve",
        json={"expected_version": 0, "actor": "alice"},
    )
    assert r.status_code == 404


def test_approve_source_version_conflict_409(client) -> None:
    source = _propose_source(client)
    r = client.post(
        f"/api/v1/skills/sources/{source['id']}/approve",
        json={"expected_version": 99, "actor": "alice"},
    )
    assert r.status_code == 409


def test_approve_already_revoked_source_is_422_not_500(client) -> None:
    """A disallowed transition (``revoked`` is terminal) must surface as a
    422, not fall through the routes' ``except`` clauses and become an
    unhandled 500 — the same class of failure gets the same status on every
    state-transition endpoint on this surface."""
    source = _approved_source(client)
    r = client.post(
        f"/api/v1/skills/sources/{source['id']}/revoke",
        json={"expected_version": source["lock_version"], "actor": "alice"},
    )
    assert r.status_code == 200, r.text
    revoked = r.json()

    r = client.post(
        f"/api/v1/skills/sources/{source['id']}/approve",
        json={"expected_version": revoked["lock_version"], "actor": "alice"},
    )
    assert r.status_code == 422, r.text


def test_revoke_source_missing_404(client) -> None:
    r = client.post(
        "/api/v1/skills/sources/nope/revoke",
        json={"expected_version": 0, "actor": "alice"},
    )
    assert r.status_code == 404


def test_approve_source_requires_actor(client) -> None:
    source = _propose_source(client)
    r = client.post(
        f"/api/v1/skills/sources/{source['id']}/approve",
        json={"expected_version": 0, "actor": ""},
    )
    assert r.status_code == 422


def test_list_sources_filters_and_pages(client) -> None:
    _propose_source(client, kind="mcp_registry", origin="a")
    _propose_source(client, kind="repo_doc", origin="b")
    _approved_source(client, origin="c")

    all_body = client.get("/api/v1/skills/sources").json()
    assert all_body["limit"] == 100 and all_body["offset"] == 0
    assert len(all_body["sources"]) == 3

    by_kind = client.get("/api/v1/skills/sources", params={"kind": "repo_doc"}).json()
    assert len(by_kind["sources"]) == 1 and by_kind["sources"][0]["kind"] == "repo_doc"

    by_status = client.get("/api/v1/skills/sources", params={"status": "approved"}).json()
    assert len(by_status["sources"]) == 1 and by_status["sources"][0]["status"] == "approved"

    page = client.get("/api/v1/skills/sources", params={"limit": 2}).json()
    assert len(page["sources"]) == 2 and page["limit"] == 2

    page2 = client.get("/api/v1/skills/sources", params={"limit": 2, "offset": 2}).json()
    assert len(page2["sources"]) == 1


def test_list_sources_rejects_unknown_filter_value(client) -> None:
    """``kind``/``status`` are typed to the closed enum, so a typo'd filter is
    a 422 from validation, never a silently empty page."""
    r = client.get("/api/v1/skills/sources", params={"kind": "not-a-real-kind"})
    assert r.status_code == 422


# --- items: register / acquire / reject / revoke / get / list --------------


def test_register_gates_on_unapproved_source(client) -> None:
    source = _propose_source(client)  # not approved
    r = client.post(
        "/api/v1/skills/items",
        json={
            "source_id": source["id"], "name": "x", "kind": "mcp_server",
            "content_hash": _HASH_A,
        },
    )
    assert r.status_code == 422


def test_register_missing_source_404(client) -> None:
    r = client.post(
        "/api/v1/skills/items",
        json={
            "source_id": "nope", "name": "x", "kind": "mcp_server",
            "content_hash": _HASH_A,
        },
    )
    assert r.status_code == 404


def test_register_rejects_malformed_request(client) -> None:
    source = _approved_source(client)
    r = client.post(
        "/api/v1/skills/items",
        json={"source_id": source["id"], "name": "x", "kind": "not-a-kind", "content_hash": _HASH_A},
    )
    assert r.status_code == 422


def test_get_item_404_when_absent(client) -> None:
    assert client.get("/api/v1/skills/items/nope").status_code == 404


def test_list_items_filters_by_every_field_and_pages(client) -> None:
    """Registers items across two sources/kinds/task-classes and exercises
    every filter the endpoint accepts, plus paging — not just the unfiltered
    count."""
    source_a = _approved_source(client, origin="src-a")
    source_b = _approved_source(client, origin="src-b")
    item_1 = _register(
        client, source_a["id"], name="a", kind="mcp_server",
        content_hash=_HASH_A, task_class="code_review",
    )
    item_2 = _register(
        client, source_a["id"], name="b", kind="cli_tool",
        content_hash=_HASH_B, task_class="code_review",
    )
    _register(
        client, source_b["id"], name="c", kind="mcp_server",
        content_hash="sha256:" + "c" * 64, task_class="triage",
    )

    all_body = client.get("/api/v1/skills/items").json()
    assert len(all_body["items"]) == 3

    by_source = client.get(
        "/api/v1/skills/items", params={"source_id": source_a["id"]}
    ).json()
    assert {i["id"] for i in by_source["items"]} == {item_1["id"], item_2["id"]}

    by_kind = client.get("/api/v1/skills/items", params={"kind": "cli_tool"}).json()
    assert len(by_kind["items"]) == 1 and by_kind["items"][0]["id"] == item_2["id"]

    by_task_class = client.get(
        "/api/v1/skills/items", params={"task_class": "triage"}
    ).json()
    assert len(by_task_class["items"]) == 1

    by_status = client.get(
        "/api/v1/skills/items", params={"status": "candidate"}
    ).json()
    assert len(by_status["items"]) == 3
    by_missing_status = client.get(
        "/api/v1/skills/items", params={"status": "acquired"}
    ).json()
    assert len(by_missing_status["items"]) == 0

    page = client.get("/api/v1/skills/items", params={"limit": 2}).json()
    assert len(page["items"]) == 2 and page["limit"] == 2
    page2 = client.get("/api/v1/skills/items", params={"limit": 2, "offset": 2}).json()
    assert len(page2["items"]) == 1
    assert {i["id"] for i in page["items"]} != {i["id"] for i in page2["items"]}


def test_list_items_rejects_unknown_filter_value(client) -> None:
    r = client.get("/api/v1/skills/items", params={"kind": "not-a-real-kind"})
    assert r.status_code == 422


def test_acquire_transitions_and_is_logged(client) -> None:
    source = _approved_source(client)
    item = _register(client, source["id"])

    r = client.post(
        f"/api/v1/skills/items/{item['id']}/acquire",
        json={"expected_version": 0, "actor": "alice"},
    )
    assert r.status_code == 200, r.text
    acquired = r.json()
    assert acquired["status"] == "acquired"

    log = client.get(f"/api/v1/skills/items/{item['id']}/log").json()
    actions = [entry["action"] for entry in log["entries"]]
    assert actions == ["registered", "acquiring", "acquired"]
    assert log["entries"][-1]["metadata"]["network"] == "denied"
    assert log["entries"][-1]["metadata"]["executor"] == "null-skill-executor"


def test_acquire_missing_item_404(client) -> None:
    r = client.post(
        "/api/v1/skills/items/nope/acquire",
        json={"expected_version": 0, "actor": "alice"},
    )
    assert r.status_code == 404


def test_acquire_version_conflict_409(client) -> None:
    source = _approved_source(client)
    item = _register(client, source["id"])
    r = client.post(
        f"/api/v1/skills/items/{item['id']}/acquire",
        json={"expected_version": 99, "actor": "alice"},
    )
    assert r.status_code == 409


def test_acquire_already_acquired_item_is_422_not_500(client) -> None:
    source = _approved_source(client)
    item = _register(client, source["id"])
    r = client.post(
        f"/api/v1/skills/items/{item['id']}/acquire",
        json={"expected_version": 0, "actor": "alice"},
    )
    acquired = r.json()

    r = client.post(
        f"/api/v1/skills/items/{item['id']}/acquire",
        json={"expected_version": acquired["lock_version"], "actor": "alice"},
    )
    assert r.status_code == 422, r.text


def test_acquire_is_idempotent_over_http_for_the_same_claim(client) -> None:
    """Retrying the exact same claim (the version the caller last saw) never
    materialises the skill twice — the second call loses the compare-and-set
    before the executor would run again."""
    source = _approved_source(client)
    item = _register(client, source["id"])
    path = f"/api/v1/skills/items/{item['id']}/acquire"
    body = {"expected_version": 0, "actor": "alice"}

    r1 = client.post(path, json=body)
    assert r1.status_code == 200
    r2 = client.post(path, json=body)
    assert r2.status_code == 409

    log = client.get(f"/api/v1/skills/items/{item['id']}/log").json()
    assert len(log["entries"]) == 3  # registered, acquiring, acquired — no duplicate


def test_reject_item(client) -> None:
    source = _approved_source(client)
    item = _register(client, source["id"])
    r = client.post(
        f"/api/v1/skills/items/{item['id']}/reject",
        json={"expected_version": 0, "actor": "alice", "detail": "not useful"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"


def test_reject_missing_item_404(client) -> None:
    r = client.post(
        "/api/v1/skills/items/nope/reject",
        json={"expected_version": 0, "actor": "alice"},
    )
    assert r.status_code == 404


def test_reject_already_rejected_item_is_422_not_500(client) -> None:
    source = _approved_source(client)
    item = _register(client, source["id"])
    r = client.post(
        f"/api/v1/skills/items/{item['id']}/reject",
        json={"expected_version": 0, "actor": "alice"},
    )
    rejected = r.json()

    r = client.post(
        f"/api/v1/skills/items/{item['id']}/reject",
        json={"expected_version": rejected["lock_version"], "actor": "alice"},
    )
    assert r.status_code == 422, r.text


def test_revoke_acquired_item(client) -> None:
    source = _approved_source(client)
    item = _register(client, source["id"])
    client.post(
        f"/api/v1/skills/items/{item['id']}/acquire",
        json={"expected_version": 0, "actor": "alice"},
    )
    r = client.post(
        f"/api/v1/skills/items/{item['id']}/revoke",
        json={"expected_version": 2, "actor": "alice", "detail": "bad effect"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked"


def test_revoke_missing_item_404(client) -> None:
    r = client.post(
        "/api/v1/skills/items/nope/revoke",
        json={"expected_version": 0, "actor": "alice"},
    )
    assert r.status_code == 404


def test_revoke_candidate_item_is_422_not_500(client) -> None:
    """``candidate -> revoked`` is not an allowed edge (only ``acquired ->
    revoked`` is) — must be a 422, not an unhandled 500."""
    source = _approved_source(client)
    item = _register(client, source["id"])
    r = client.post(
        f"/api/v1/skills/items/{item['id']}/revoke",
        json={"expected_version": 0, "actor": "alice"},
    )
    assert r.status_code == 422, r.text


def test_log_404_when_item_absent(client) -> None:
    assert client.get("/api/v1/skills/items/nope/log").status_code == 404


# --- outcomes + effect -------------------------------------------------------


def test_record_and_list_outcomes(client) -> None:
    source = _approved_source(client)
    item = _register(client, source["id"])
    r = client.post(
        f"/api/v1/skills/items/{item['id']}/outcomes",
        json={"task_id": "t-1", "used": True, "cost_usd": 1.5, "accepted": True},
    )
    assert r.status_code == 201, r.text
    assert r.json()["task_id"] == "t-1"

    outcomes = client.get(f"/api/v1/skills/items/{item['id']}/outcomes").json()
    assert len(outcomes["outcomes"]) == 1


def test_record_outcome_missing_item_404(client) -> None:
    r = client.post(
        "/api/v1/skills/items/nope/outcomes",
        json={"task_id": "t-1", "used": True, "cost_usd": 1.0, "accepted": True},
    )
    assert r.status_code == 404


def test_record_outcome_rejects_empty_task_id(client) -> None:
    source = _approved_source(client)
    item = _register(client, source["id"])
    r = client.post(
        f"/api/v1/skills/items/{item['id']}/outcomes",
        json={"task_id": "  ", "used": True, "cost_usd": 1.0, "accepted": True},
    )
    assert r.status_code == 422


def test_list_outcomes_missing_item_404(client) -> None:
    assert client.get("/api/v1/skills/items/nope/outcomes").status_code == 404


def test_get_effect_missing_item_404(client) -> None:
    assert client.get("/api/v1/skills/items/nope/effect").status_code == 404


def test_get_effect_none_when_no_outcomes(client) -> None:
    source = _approved_source(client)
    item = _register(client, source["id"])
    body = client.get(f"/api/v1/skills/items/{item['id']}/effect").json()
    assert body["improved"] is None
    assert body["baseline"]["count"] == 0


def test_get_effect_true_only_when_better_on_both_metrics(client) -> None:
    source = _approved_source(client)
    item = _register(client, source["id"])

    def _outcome(**kw):
        r = client.post(f"/api/v1/skills/items/{item['id']}/outcomes", json=kw)
        assert r.status_code == 201, r.text

    _outcome(task_id="b-1", used=False, cost_usd=10.0, accepted=True)
    _outcome(task_id="b-2", used=False, cost_usd=10.0, accepted=True)
    # with-skill: cheaper but *same* first-pass rate — a wash on one axis, not
    # a proven improvement.
    _outcome(task_id="w-1", used=True, cost_usd=5.0, accepted=True)
    _outcome(task_id="w-2", used=True, cost_usd=5.0, accepted=True)

    body = client.get(f"/api/v1/skills/items/{item['id']}/effect").json()
    assert body["improved"] is False
