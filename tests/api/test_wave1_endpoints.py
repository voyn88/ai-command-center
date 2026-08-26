"""Endpoint tests for the Wave-1 write surface
(``command_center.api.wave1_routes`` → ``wave1_service`` → ``runtime.db.wave1``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox
and resets its contents between cases, so the runtime db and ``tasks.json`` the
service writes are throwaway. A fresh :class:`EventBus` is installed on the
process-wide bus per test so published events are observable and never leak.

Fixtures use only generic project codes (``AICC``, ``BANK``) and invented ids —
no real names or paths — keeping the public-repo privacy gate green.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api.app import create_app
from command_center.events import (
    DigestReady,
    Event,
    OwnerItemCreated,
    ProposalCreated,
    ProposalPromotedToTask,
    default_bus,
)
from command_center.tasks_repository import load_tasks
from command_center.runtime.db.wave1 import create_advisor_proposal, get_advisor_proposal
from command_center.runtime.db.core import resolve_db_path
from command_center.api.wave1_service import ROOT, _db_path


@pytest.fixture
def events() -> list[Event]:
    """Capture every event published on the default bus during a test."""
    captured: list[Event] = []
    bus = default_bus()
    bus.clear()
    unsubscribe = bus.subscribe(Event, captured.append)
    try:
        yield captured
    finally:
        unsubscribe()
        bus.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


# --- proposals: create / list / get ---------------------------------------


def _create_proposal(client: TestClient, **overrides) -> dict:
    payload = {
        "kind": "trend", "title": "Adopt X", "project_ref": "AICC",
        "body": "why", "expected_gain": "high", "effort": "low",
    }
    payload.update(overrides)
    r = client.post("/api/v1/proposals", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def test_create_proposal_persists_and_emits_event(client, events) -> None:
    body = _create_proposal(client)
    assert body["id"]
    assert body["status"] == "new"
    assert body["project_ref"] == "AICC"

    # Persisted through to the runtime db.
    stored = get_advisor_proposal(resolve_db_path(ROOT), body["id"])
    assert stored is not None and stored["title"] == "Adopt X"

    created = [e for e in events if isinstance(e, ProposalCreated)]
    assert len(created) == 1
    assert created[0].proposal_id == body["id"]
    assert created[0].project_ref == "AICC"


def test_list_proposals_paginates_and_filters(client) -> None:
    for i in range(3):
        _create_proposal(client, title=f"p{i}")
    _create_proposal(client, kind="ux", title="ux-one")

    all_body = client.get("/api/v1/proposals").json()
    assert all_body["limit"] == 100 and all_body["offset"] == 0
    assert len(all_body["proposals"]) == 4

    page = client.get("/api/v1/proposals", params={"limit": 2, "offset": 0}).json()
    assert len(page["proposals"]) == 2 and page["limit"] == 2


def test_get_proposal_detail_and_404(client) -> None:
    body = _create_proposal(client)
    got = client.get(f"/api/v1/proposals/{body['id']}")
    assert got.status_code == 200
    assert got.json()["id"] == body["id"]
    assert client.get("/api/v1/proposals/does-not-exist").status_code == 404


def test_create_proposal_rejects_bad_kind(client) -> None:
    r = client.post(
        "/api/v1/proposals",
        json={"kind": "not-a-kind", "title": "t", "project_ref": "AICC"},
    )
    assert r.status_code == 422


# --- proposals: privacy redaction -----------------------------------------


def _insert_legacy_sensitive_proposal(title: str = "secret leaky") -> dict:
    """Insert a BANK proposal straight through the repository, bypassing the
    write-time rejection — a stand-in for a row that predates the redaction
    fix. The read path must still drop it."""
    return create_advisor_proposal(
        _db_path(), kind="trend", title=title, project_ref="BANK", body="b"
    )


def test_sensitive_proposal_is_rejected_at_write(client) -> None:
    # The manual POST never persists a sensitive title (MED-1c): it is rejected,
    # so the title cannot leak through any later read.
    r = client.post(
        "/api/v1/proposals",
        json={"kind": "trend", "title": "secret leaky", "project_ref": "BANK"},
    )
    assert r.status_code == 400, r.text
    listed = client.get("/api/v1/proposals").json()["proposals"]
    assert all("secret" not in (p["title"] or "") for p in listed)


def test_sensitive_proposal_is_dropped_from_list_and_detail(client) -> None:
    visible = _create_proposal(client, project_ref="AICC", title="visible")
    secret = _insert_legacy_sensitive_proposal()

    listed = client.get("/api/v1/proposals").json()["proposals"]
    ids = {p["id"] for p in listed}
    assert visible["id"] in ids
    assert secret["id"] not in ids
    assert all(p["project_ref"] != "BANK" for p in listed)

    # Detail reads as not-found — the title never leaves the surface.
    assert client.get(f"/api/v1/proposals/{secret['id']}").status_code == 404


# --- proposals: promote → task --------------------------------------------


def test_promote_creates_task_and_emits_event(client, events) -> None:
    body = _create_proposal(client, title="Ship feature Y")
    r = client.post(f"/api/v1/proposals/{body['id']}/promote")
    assert r.status_code == 200, r.text
    result = r.json()
    assert result["proposal"]["status"] == "converted"
    task_id = result["task_id"]
    assert task_id

    # A real task landed on the board through the tasks repository.
    tasks = load_tasks(ROOT)
    created = next((t for t in tasks if t["id"] == task_id), None)
    assert created is not None
    assert created["project"] == "AICC"
    assert created["title"] == "Ship feature Y"

    promoted = [e for e in events if isinstance(e, ProposalPromotedToTask)]
    assert len(promoted) == 1
    assert promoted[0].task_id == task_id
    assert promoted[0].proposal_id == body["id"]


def test_double_promote_is_conflict_and_creates_one_task(client) -> None:
    body = _create_proposal(client)
    first = client.post(f"/api/v1/proposals/{body['id']}/promote")
    assert first.status_code == 200
    second = client.post(f"/api/v1/proposals/{body['id']}/promote")
    assert second.status_code == 409
    # Exactly one task was created for this proposal.
    tasks = load_tasks(ROOT)
    linked = [t for t in tasks if t.get("title") == "Adopt X"]
    assert len(linked) == 1


def test_promote_missing_proposal_is_404(client) -> None:
    assert client.post("/api/v1/proposals/nope/promote").status_code == 404


def test_promote_sensitive_proposal_is_404(client) -> None:
    # A legacy sensitive row (inserted below the write-guard) is still
    # unpromotable — it reads as not-found, so it can never become a task.
    secret = _insert_legacy_sensitive_proposal()
    assert client.post(f"/api/v1/proposals/{secret['id']}/promote").status_code == 404


# --- owner items ----------------------------------------------------------


def test_owner_item_create_list_and_event(client, events) -> None:
    r = client.post(
        "/api/v1/owner-items",
        json={"title": "Call bank", "detail": "d", "due": "2026-08-13"},
    )
    assert r.status_code == 201
    item = r.json()
    assert item["id"] and item["done"] is False

    listed = client.get("/api/v1/owner-items").json()
    assert {i["id"] for i in listed["items"]} == {item["id"]}

    got = client.get(f"/api/v1/owner-items/{item['id']}")
    assert got.status_code == 200 and got.json()["title"] == "Call bank"

    created = [e for e in events if isinstance(e, OwnerItemCreated)]
    assert len(created) == 1 and created[0].item_id == item["id"]


def test_owner_item_manual_sensitive_is_rejected(client) -> None:
    r = client.post(
        "/api/v1/owner-items",
        json={"title": "secret", "project_ref": "BANK"},
    )
    assert r.status_code == 400, r.text
    assert client.get("/api/v1/owner-items").json()["items"] == []


def test_owner_item_sensitive_is_dropped_from_list_and_detail(client) -> None:
    # A legacy owner item flagged to a sensitive project (inserted below the
    # write-guard) is dropped on list and reads as not-found on detail.
    from command_center.runtime.db.wave1 import create_owner_item

    visible = create_owner_item(_db_path(), title="visible", project_ref="AICC")
    secret = create_owner_item(_db_path(), title="secret", project_ref="BANK")

    listed = client.get("/api/v1/owner-items").json()["items"]
    ids = {i["id"] for i in listed}
    assert visible["id"] in ids and secret["id"] not in ids
    assert client.get(f"/api/v1/owner-items/{secret['id']}").status_code == 404


# --- digest ---------------------------------------------------------------


def test_digest_create_list_and_event(client, events) -> None:
    r = client.post(
        "/api/v1/digest",
        json={"title": "Weekly", "body": "b", "category": "ops", "refs": ["a", "b"]},
    )
    assert r.status_code == 201
    item = r.json()
    assert item["refs"] == ["a", "b"]

    listed = client.get("/api/v1/digest", params={"category": "ops"}).json()
    assert {i["id"] for i in listed["items"]} == {item["id"]}

    ready = [e for e in events if isinstance(e, DigestReady)]
    assert len(ready) == 1 and ready[0].digest_id == item["id"]


def test_digest_manual_sensitive_is_rejected(client) -> None:
    r = client.post(
        "/api/v1/digest",
        json={"title": "secret", "project_ref": "BANK"},
    )
    assert r.status_code == 400, r.text
    assert client.get("/api/v1/digest").json()["items"] == []


def test_digest_sensitive_is_dropped_from_list_and_detail(client) -> None:
    # A legacy digest row flagged to a sensitive project is dropped on list and
    # reads as not-found on detail — its title never leaves the surface.
    from command_center.runtime.db.wave1 import create_digest_item

    visible = create_digest_item(_db_path(), title="visible", project_ref="AICC")
    secret = create_digest_item(_db_path(), title="secret", project_ref="BANK")

    listed = client.get("/api/v1/digest").json()["items"]
    ids = {i["id"] for i in listed}
    assert visible["id"] in ids and secret["id"] not in ids
    assert client.get(f"/api/v1/digest/{secret['id']}").status_code == 404
