"""Endpoint + service tests for the Wave-2 conflicts surface
(``command_center.api.conflict_routes`` → ``conflicts.service`` →
``runtime.db.conflict``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox
and resets its contents between cases, so the runtime db the service writes is
throwaway.

Fixtures use only generic project codes (``AICC``, ``BANK``) and invented ids —
no real names or paths — keeping the public-repo privacy gate green.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api.app import create_app
from command_center.conflicts.service import ROOT
from command_center.runtime import db
from command_center.runtime.db.conflict import get_conflict
from command_center.runtime.db.core import resolve_db_path


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    """Migrate the per-test sandbox db up front so tests that read/write the
    store directly (bypassing the service's lazy migrate) see the tables."""
    db.migrate(resolve_db_path(ROOT))


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _create(client: TestClient, **overrides) -> dict:
    payload = {"kind": "merge", "source_ref": "pr:1", "severity": "sev2"}
    payload.update(overrides)
    r = client.post("/api/v1/conflicts", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# --- create / list / get --------------------------------------------------


def test_create_conflict_persists(client) -> None:
    body = _create(client)
    assert body["id"] and body["status"] == "open"
    assert body["kind"] == "merge"
    stored = get_conflict(resolve_db_path(ROOT), body["id"])
    assert stored is not None and stored["source_ref"] == "pr:1"


def test_create_conflict_rejects_bad_kind(client) -> None:
    r = client.post("/api/v1/conflicts", json={"kind": "nonsense"})
    assert r.status_code == 422


def test_list_conflicts_filters_and_pages(client) -> None:
    _create(client, kind="merge", owner="alice")
    _create(client, kind="perf", owner="bob")
    _create(client, kind="security", owner="alice")

    all_body = client.get("/api/v1/conflicts").json()
    assert all_body["limit"] == 100 and all_body["offset"] == 0
    assert len(all_body["conflicts"]) == 3

    by_kind = client.get("/api/v1/conflicts", params={"kind": "merge"}).json()
    assert len(by_kind["conflicts"]) == 1

    by_owner = client.get("/api/v1/conflicts", params={"owner": "alice"}).json()
    assert len(by_owner["conflicts"]) == 2


def test_get_missing_conflict_is_404(client) -> None:
    assert client.get("/api/v1/conflicts/nope").status_code == 404


# --- redaction ------------------------------------------------------------


def test_sensitive_conflict_write_is_rejected(client) -> None:
    r = client.post(
        "/api/v1/conflicts",
        json={"kind": "security", "source_ref": "secret", "project_ref": "BANK"},
    )
    assert r.status_code == 400


def test_sensitive_conflict_is_redacted_from_reads(client) -> None:
    # Force a sensitive row into the store directly (bypassing the write guard)
    # to prove the *read* path also drops it.
    row = db.create_conflict(
        resolve_db_path(ROOT), kind="security", source_ref="leak", project_ref="BANK"
    )
    visible = client.get("/api/v1/conflicts").json()["conflicts"]
    assert all(c["source_ref"] != "leak" for c in visible)
    assert client.get(f"/api/v1/conflicts/{row['id']}").status_code == 404


# --- workflow: assign / mitigate / resolve --------------------------------


def test_assign_sets_owner(client) -> None:
    body = _create(client)
    r = client.post(f"/api/v1/conflicts/{body['id']}/assign", json={"owner": "alice"})
    assert r.status_code == 200
    assert r.json()["owner"] == "alice"


def test_mitigation_advances_open_to_mitigating(client) -> None:
    body = _create(client)
    r = client.post(
        f"/api/v1/conflicts/{body['id']}/mitigation", json={"mitigation": "revert #1"}
    )
    assert r.status_code == 200
    updated = r.json()
    assert updated["mitigation"] == "revert #1"
    assert updated["status"] == "mitigating"


def test_resolve_requires_mitigation_and_owner(client) -> None:
    body = _create(client)
    cid = body["id"]

    # Neither owner nor mitigation yet -> 409.
    assert client.post(f"/api/v1/conflicts/{cid}/resolve").status_code == 409

    # Owner only -> still 409 (mitigation missing).
    client.post(f"/api/v1/conflicts/{cid}/assign", json={"owner": "alice"})
    assert client.post(f"/api/v1/conflicts/{cid}/resolve").status_code == 409

    # Add the mitigation -> resolve now succeeds.
    client.post(f"/api/v1/conflicts/{cid}/mitigation", json={"mitigation": "revert"})
    r = client.post(f"/api/v1/conflicts/{cid}/resolve")
    assert r.status_code == 200
    resolved = r.json()
    assert resolved["status"] == "resolved"
    assert resolved["resolved_at"] is not None


def test_resolve_with_mitigation_but_no_owner_is_blocked(client) -> None:
    body = _create(client)
    cid = body["id"]
    client.post(f"/api/v1/conflicts/{cid}/mitigation", json={"mitigation": "plan"})
    assert client.post(f"/api/v1/conflicts/{cid}/resolve").status_code == 409


def test_resolve_missing_conflict_is_404(client) -> None:
    assert client.post("/api/v1/conflicts/nope/resolve").status_code == 404


def test_full_lifecycle_open_mitigating_resolved(client) -> None:
    body = _create(client, kind="budget")
    cid = body["id"]
    client.post(f"/api/v1/conflicts/{cid}/assign", json={"owner": "cto"})
    mit = client.post(
        f"/api/v1/conflicts/{cid}/mitigation", json={"mitigation": "cap spend"}
    ).json()
    assert mit["status"] == "mitigating"
    resolved = client.post(f"/api/v1/conflicts/{cid}/resolve").json()
    assert resolved["status"] == "resolved"
    assert resolved["owner"] == "cto" and resolved["mitigation"] == "cap spend"
