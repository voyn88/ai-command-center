"""GET /v1/snapshot: 200 contract, ETag/304, freshness states, client compat."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .conftest import auth_headers, fresh_sample

# The exact key set the shipped Swift SnapshotDecoder requires (default-key
# JSONDecoder over AICCNativeCore.Snapshot).  Additive keys are allowed;
# missing or renamed keys break every installed client.
SWIFT_SNAPSHOT_KEYS = {
    "schemaVersion",
    "revision",
    "generatedAt",
    "freshness",
    "tasks",
    "lanes",
    "events",
}
SWIFT_EVIDENCE_KEYS = {
    "headSHA",
    "pullRequest",
    "ci",
    "acceptance",
    "mergedSHA",
    "deployedSHA",
}

# The client-side prohibited scan (SnapshotDecoder.decode) — if any of these
# substrings appear in a body, the client rejects the whole snapshot.
CLIENT_PROHIBITED = [
    "authorization",
    "bearer ",
    "password",
    "ssh-rsa",
    "postgres://",
    "private_key",
    "prompt",
]


def test_snapshot_200_matches_schema_1_0(client, device_token):
    response = client.get("/v1/snapshot", headers=auth_headers(device_token))
    assert response.status_code == 200
    body = response.json()
    assert body["schemaVersion"] == "1.0"
    assert body["revision"] == "r-000042"
    assert body["freshness"] == "fresh"
    assert SWIFT_SNAPSHOT_KEYS <= set(body)
    assert body["tasks"][0]["id"] == "VOYN-EXAMPLE-001"
    assert set(body["tasks"][0]["evidence"]) == SWIFT_EVIDENCE_KEYS
    assert body["tasks"][0]["evidence"]["ci"] == "verified"  # 'passed' normalized
    assert body["tasks"][1]["blocker"] == "Waiting for owner decision"
    assert body["lanes"][0]["heartbeatAgeSeconds"] == 12
    assert body["events"][0]["occurredAt"].endswith("Z")
    # Additive calm-overview data for projects and connection state.
    assert body["projects"][0]["name"] == "AI Command Center"
    assert body["connection"]["state"] == "fresh"
    assert isinstance(body["connection"]["projectionAgeSeconds"], int)


def test_snapshot_body_passes_client_prohibited_scan(client, device_token):
    response = client.get("/v1/snapshot", headers=auth_headers(device_token))
    lowered = response.text.lower()
    for needle in CLIENT_PROHIBITED:
        assert needle not in lowered, needle


def test_snapshot_304_on_matching_revision(client, device_token):
    first = client.get("/v1/snapshot", headers=auth_headers(device_token))
    etag = first.headers["ETag"]
    assert etag == '"r-000042"'
    again = client.get(
        "/v1/snapshot",
        headers={**auth_headers(device_token), "If-None-Match": etag},
    )
    assert again.status_code == 304
    assert again.headers["ETag"] == etag
    # The client sends the bare revision (no quotes) — must also match.
    bare = client.get(
        "/v1/snapshot",
        headers={**auth_headers(device_token), "If-None-Match": "r-000042"},
    )
    assert bare.status_code == 304


def test_snapshot_no_304_when_revision_changed(
    client, device_token, projection_path: Path
):
    data = fresh_sample(projection_path)
    data["revision"] = "r-000043"
    projection_path.write_text(json.dumps(data), encoding="utf-8")
    response = client.get(
        "/v1/snapshot",
        headers={**auth_headers(device_token), "If-None-Match": '"r-000042"'},
    )
    assert response.status_code == 200
    assert response.json()["revision"] == "r-000043"


def _age_projection(projection_path: Path, seconds: int) -> None:
    data = json.loads(projection_path.read_text(encoding="utf-8"))
    stamp = datetime.now(UTC) - timedelta(seconds=seconds)
    data["generated_at"] = stamp.isoformat().replace("+00:00", "Z")
    projection_path.write_text(json.dumps(data), encoding="utf-8")


def test_snapshot_stale_and_offline_freshness(
    client, device_token, projection_path: Path
):
    _age_projection(projection_path, 300)
    stale = client.get("/v1/snapshot", headers=auth_headers(device_token))
    assert stale.json()["freshness"] == "stale"
    _age_projection(projection_path, 100_000)
    offline = client.get("/v1/snapshot", headers=auth_headers(device_token))
    assert offline.json()["freshness"] == "offline"


def test_snapshot_no_304_when_not_fresh(client, device_token, projection_path: Path):
    """A decayed freshness must reach the client even at the same revision."""
    _age_projection(projection_path, 300)
    response = client.get(
        "/v1/snapshot",
        headers={**auth_headers(device_token), "If-None-Match": '"r-000042"'},
    )
    assert response.status_code == 200
    assert response.json()["freshness"] == "stale"


def test_snapshot_degraded_when_producer_declares_it(
    client, device_token, projection_path: Path
):
    data = fresh_sample(projection_path)
    data["degraded"] = True
    projection_path.write_text(json.dumps(data), encoding="utf-8")
    response = client.get("/v1/snapshot", headers=auth_headers(device_token))
    assert response.json()["freshness"] == "degraded"


def test_snapshot_offline_when_projection_missing(
    client, device_token, projection_path: Path
):
    projection_path.unlink()
    response = client.get("/v1/snapshot", headers=auth_headers(device_token))
    assert response.status_code == 200
    body = response.json()
    assert body["freshness"] == "offline"
    assert body["tasks"] == [] and body["events"] == []
    assert body["schemaVersion"] == "1.0"


def test_snapshot_offline_when_projection_corrupt(
    client, device_token, projection_path: Path
):
    projection_path.write_text("{not json", encoding="utf-8")
    response = client.get("/v1/snapshot", headers=auth_headers(device_token))
    assert response.status_code == 200
    assert response.json()["freshness"] == "offline"
