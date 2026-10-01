"""Endpoint + service tests for the VOYN-MIN-WOW-1 proof-package surface
(``command_center.api.proof_package_routes`` → ``proof_package_service`` →
``runtime.db.proof_package``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox
and resets its contents between cases, so the runtime db the service writes is
throwaway.

Fixtures use only generic project codes (``AICC``, ``BANK``) — no real names —
keeping the public-repo privacy gate green.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api.app import create_app
from command_center.api.proof_package_service import ROOT
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    db.migrate(resolve_db_path(ROOT))


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_proof_package_assembles_from_council_and_proposal_data(client: TestClient) -> None:
    m = client.post(
        "/api/v1/council/motions",
        json={"title": "Adopt X", "proposed_by": "chair", "quorum": 1, "project_ref": "AICC"},
    ).json()
    client.post(f"/api/v1/council/motions/{m['id']}/vote", json={"voter_id": "chair", "choice": "yes"})
    close = client.post(
        f"/api/v1/council/motions/{m['id']}/close",
        json={"impact": {"amount_usd": 2500}},
    )
    assert close.status_code == 200, close.text

    r = client.get("/api/v1/proof-package/AICC")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"] == "AICC"
    assert any(e["ref_id"] == m["id"] for e in body["digital_memory"])
    assert len(body["decision_pnl"]) == 1
    assert body["decision_pnl"][0]["impact"] == {"amount_usd": 2500}
    assert isinstance(body["integrity_hash"], str) and len(body["integrity_hash"]) == 64


def test_proof_package_empty_project_is_still_200(client: TestClient) -> None:
    r = client.get("/api/v1/proof-package/NOBODY-HOME")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["digital_memory"] == []
    assert body["audit_vault"] == []


def test_proof_package_rejects_sensitive_project(client: TestClient) -> None:
    r = client.get("/api/v1/proof-package/BANK")
    assert r.status_code == 400
