"""Endpoint + service tests for the Wave-3 networking surface
(``command_center.api.networking_routes`` → ``networking.service`` →
``runtime.db.networking``).

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
    Event,
    NetworkingContactInvited,
    NetworkingFeedbackReceived,
    default_bus,
)
from command_center.networking.service import ROOT
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path
from command_center.tasks_repository import load_tasks


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    """Migrate the per-test sandbox db up front so tests that read the store
    directly see the v23 tables."""
    db.migrate(resolve_db_path(ROOT))


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


def _create_contact(client: TestClient, **overrides) -> dict:
    payload = {"display_name": "Ada", "handle": "@ada", "project_ref": "AICC"}
    payload.update(overrides)
    r = client.post("/api/v1/networking/contacts", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# --- contacts -------------------------------------------------------------


def test_create_and_get_contact(client) -> None:
    body = _create_contact(client)
    assert body["id"] and body["display_name"] == "Ada"
    got = client.get(f"/api/v1/networking/contacts/{body['id']}")
    assert got.status_code == 200 and got.json()["handle"] == "@ada"


def test_get_missing_contact_is_404(client) -> None:
    assert client.get("/api/v1/networking/contacts/nope").status_code == 404


def test_create_contact_sensitive_project_is_rejected(client) -> None:
    r = client.post(
        "/api/v1/networking/contacts",
        json={"display_name": "Secret", "project_ref": "BANK"},
    )
    assert r.status_code == 400


def test_sensitive_contact_is_redacted_from_reads(client) -> None:
    # Force a sensitive row directly to prove the read path drops it too.
    row = db.create_contact(
        resolve_db_path(ROOT), display_name="Secret", handle="@secret", project_ref="BANK"
    )
    visible = client.get("/api/v1/networking/contacts").json()["contacts"]
    assert all(c["handle"] != "@secret" for c in visible)
    assert client.get(f"/api/v1/networking/contacts/{row['id']}").status_code == 404


# --- messages -------------------------------------------------------------


def test_create_message_and_list(client) -> None:
    contact = _create_contact(client)
    r = client.post(
        "/api/v1/networking/messages",
        json={"contact_id": contact["id"], "body": "hello", "direction": "outbound"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["direction"] == "outbound"
    listing = client.get(
        "/api/v1/networking/messages", params={"contact_id": contact["id"]}
    ).json()
    assert listing["messages"][0]["body"] == "hello"


def test_message_against_missing_contact_is_404(client) -> None:
    r = client.post("/api/v1/networking/messages", json={"contact_id": "ghost", "body": "x"})
    assert r.status_code == 404


# --- feedback intake → actionable task (ACCEPTANCE) -----------------------


def test_feedback_yields_actionable_task(client, events) -> None:
    """Acceptance: submitting feedback yields an actionable task on the board —
    asserted by loading the task store and finding the returned id."""
    contact = _create_contact(client)
    r = client.post(
        "/api/v1/networking/feedback",
        json={
            "contact_id": contact["id"],
            "project_ref": "AICC",
            "title": "Add CSV export",
            "body": "customer asked for a CSV export button",
        },
    )
    assert r.status_code == 201, r.text
    result = r.json()
    task_id = result["task_id"]
    assert task_id
    assert result["message"]["kind"] == "feedback"

    # The task actually exists in the store (single-writer tasks_repository).
    tasks = load_tasks(ROOT)
    created = next((t for t in tasks if t["id"] == task_id), None)
    assert created is not None, "feedback must create an actionable task"
    assert created["project"] == "AICC"
    assert created["title"] == "Add CSV export"
    assert created["status"] == "Backlog"

    # The captured intake message is persisted and tied to the contact.
    msgs = client.get(
        "/api/v1/networking/messages", params={"contact_id": contact["id"]}
    ).json()["messages"]
    assert any(m["id"] == result["message"]["id"] and m["kind"] == "feedback" for m in msgs)

    # The feedback signal event fired (advisor-consumable seam).
    fired = [e for e in events if isinstance(e, NetworkingFeedbackReceived)]
    assert len(fired) == 1
    assert fired[0].task_id == task_id and fired[0].contact_id == contact["id"]


def test_feedback_sensitive_project_is_rejected(client) -> None:
    contact = _create_contact(client)
    r = client.post(
        "/api/v1/networking/feedback",
        json={"contact_id": contact["id"], "project_ref": "BANK", "title": "x"},
    )
    assert r.status_code == 400
    # No task leaked into the store.
    assert load_tasks(ROOT) == []


def test_feedback_unknown_project_is_422(client) -> None:
    contact = _create_contact(client)
    r = client.post(
        "/api/v1/networking/feedback",
        json={"contact_id": contact["id"], "project_ref": "NOPE", "title": "x"},
    )
    assert r.status_code == 422
    assert load_tasks(ROOT) == []


def test_feedback_missing_contact_is_404(client) -> None:
    r = client.post(
        "/api/v1/networking/feedback",
        json={"contact_id": "ghost", "project_ref": "AICC", "title": "x"},
    )
    assert r.status_code == 404


# --- Council invitations (seam) -------------------------------------------


def test_invite_creates_record_and_emits_seam_event(client, events) -> None:
    contact = _create_contact(client)
    r = client.post(
        "/api/v1/networking/invite",
        json={"contact_id": contact["id"], "note": "join the council"},
    )
    assert r.status_code == 201, r.text
    inv = r.json()
    assert inv["status"] == "pending"
    assert inv["council_ref"]  # the stable seam Council consumes
    assert inv["project_ref"] == "AICC"  # inherited from the contact

    listing = client.get("/api/v1/networking/invitations").json()["invitations"]
    assert any(i["id"] == inv["id"] for i in listing)

    fired = [e for e in events if isinstance(e, NetworkingContactInvited)]
    assert len(fired) == 1
    assert fired[0].invitation_id == inv["id"]
    assert fired[0].council_ref == inv["council_ref"]


def test_invite_missing_contact_is_404(client) -> None:
    r = client.post("/api/v1/networking/invite", json={"contact_id": "ghost"})
    assert r.status_code == 404


def test_invitations_redacted_from_reads(client) -> None:
    # A sensitive contact + invitation forced directly must not surface.
    row = db.create_contact(resolve_db_path(ROOT), display_name="Secret", project_ref="BANK")
    db.create_invitation(
        resolve_db_path(ROOT), contact_id=row["id"], council_ref="leak", project_ref="BANK"
    )
    visible = client.get("/api/v1/networking/invitations").json()["invitations"]
    assert all(i["council_ref"] != "leak" for i in visible)
