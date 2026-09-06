"""Cursor pagination and the read-only collection routes."""

from __future__ import annotations

import json

from .conftest import auth_headers, fresh_sample


def test_tasks_cursor_pagination(client, device_token):
    first = client.get("/v1/tasks?limit=1", headers=auth_headers(device_token))
    assert first.status_code == 200
    body = first.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["id"] == "VOYN-EXAMPLE-001"
    cursor = body["page"]["nextCursor"]
    assert cursor

    second = client.get(
        f"/v1/tasks?limit=1&cursor={cursor}", headers=auth_headers(device_token)
    )
    body2 = second.json()
    assert body2["items"][0]["id"] == "VOYN-EXAMPLE-002"
    assert body2["page"]["nextCursor"] is None


def test_task_detail_found(client, device_token):
    response = client.get(
        "/v1/tasks/VOYN-EXAMPLE-002", headers=auth_headers(device_token)
    )
    assert response.status_code == 200
    assert response.json()["task"]["blocker"] == "Waiting for owner decision"


def test_malformed_cursor_is_422(client, device_token):
    response = client.get(
        "/v1/tasks?cursor=%3Cgarbage%3E", headers=auth_headers(device_token)
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_failed"


def test_foreign_collection_cursor_is_422(client, device_token):
    first = client.get("/v1/tasks?limit=1", headers=auth_headers(device_token))
    tasks_cursor = first.json()["page"]["nextCursor"]
    response = client.get(
        f"/v1/dialogs?cursor={tasks_cursor}", headers=auth_headers(device_token)
    )
    assert response.status_code == 422


def test_events_cursor_survives_same_revision(client, device_token):
    first = client.get("/v1/events?limit=1", headers=auth_headers(device_token))
    cursor = first.json()["page"]["nextCursor"]
    second = client.get(
        f"/v1/events?after_cursor={cursor}", headers=auth_headers(device_token)
    )
    assert second.status_code == 200
    assert second.json()["items"][0]["id"] == "evt-002"


def test_events_cursor_conflicts_after_revision_change(
    client, device_token, projection_path
):
    first = client.get("/v1/events?limit=1", headers=auth_headers(device_token))
    cursor = first.json()["page"]["nextCursor"]
    data = fresh_sample(projection_path)
    data["revision"] = "r-000099"
    projection_path.write_text(json.dumps(data), encoding="utf-8")
    response = client.get(
        f"/v1/events?after_cursor={cursor}", headers=auth_headers(device_token)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "resync_required"


def test_decisions_route(client, device_token):
    response = client.get("/v1/decisions", headers=auth_headers(device_token))
    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["status"] == "accepted"
    assert items[0]["decidedAt"] == "2026-08-23T05:20:00Z"


def test_dialogs_route_is_summary_only(client, device_token):
    response = client.get("/v1/dialogs", headers=auth_headers(device_token))
    item = response.json()["items"][0]
    assert set(item) == {
        "id",
        "title",
        "state",
        "lastActivityAt",
        "messageCount",
        "lastSummary",
    }
