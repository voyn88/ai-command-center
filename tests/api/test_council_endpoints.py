"""Endpoint + service tests for the Wave-3 Council surface
(``command_center.api.council_routes`` → ``council.service`` →
``runtime.db.council``).

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
from command_center.council import roles, service
from command_center.council.service import ROOT
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    db.migrate(resolve_db_path(ROOT))


@pytest.fixture(autouse=True)
def _restore_roster() -> None:
    """A test may swap the Board roster (the permission seam); restore the default
    afterward so cases stay independent."""
    original = service._roster
    yield
    service._roster = original


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _motion(client: TestClient, **overrides) -> dict:
    payload = {"title": "Adopt X", "proposed_by": "chair", "quorum": 1}
    payload.update(overrides)
    r = client.post("/api/v1/council/motions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _vote(client: TestClient, motion_id: str, voter_id: str, choice: str, **extra):
    body = {"voter_id": voter_id, "choice": choice}
    body.update(extra)
    return client.post(f"/api/v1/council/motions/{motion_id}/vote", json=body)


# --- motions: create / list / get -----------------------------------------


def test_create_motion_persists(client) -> None:
    body = _motion(client, quorum=2)
    assert body["id"] and body["status"] == "open" and body["quorum"] == 2
    stored = db.get_motion(resolve_db_path(ROOT), body["id"])
    assert stored is not None and stored["proposed_by"] == "chair"


def test_create_motion_rejects_bad_quorum(client) -> None:
    r = client.post(
        "/api/v1/council/motions",
        json={"title": "X", "proposed_by": "chair", "quorum": 0},
    )
    assert r.status_code == 422


def test_create_motion_rejects_sensitive_project(client) -> None:
    r = client.post(
        "/api/v1/council/motions",
        json={"title": "X", "proposed_by": "chair", "project_ref": "BANK"},
    )
    assert r.status_code == 400


def test_list_motions_filters_and_pages(client) -> None:
    _motion(client, title="a")
    _motion(client, title="b")
    body = client.get("/api/v1/council/motions").json()
    assert body["limit"] == 100 and len(body["motions"]) == 2
    open_only = client.get("/api/v1/council/motions", params={"status": "open"}).json()
    assert len(open_only["motions"]) == 2


def test_get_motion_detail_carries_votes_and_journal(client) -> None:
    m = _motion(client)
    _vote(client, m["id"], "chair", "yes")
    detail = client.get(f"/api/v1/council/motions/{m['id']}").json()
    assert detail["motion"]["id"] == m["id"]
    assert len(detail["votes"]) == 1 and detail["votes"][0]["role"] == "chair"
    assert [e["event_type"] for e in detail["journal"]] == ["motion_opened", "vote_cast"]


def test_get_missing_motion_404(client) -> None:
    assert client.get("/api/v1/council/motions/nope").status_code == 404


# --- voting: roles recorded, one vote per voter, permission ---------------


def test_vote_records_authoritative_role(client) -> None:
    m = _motion(client)
    r = _vote(client, m["id"], "security", "yes", rationale="safe")
    assert r.status_code == 201, r.text
    body = r.json()
    # role comes from the roster, not the client
    assert body["role"] == "security" and body["voter_kind"] == "ai"
    assert body["choice"] == "yes"


def test_double_vote_returns_409(client) -> None:
    m = _motion(client)
    assert _vote(client, m["id"], "chair", "yes").status_code == 201
    dup = _vote(client, m["id"], "chair", "no")
    assert dup.status_code == 409


def test_vote_on_missing_motion_404(client) -> None:
    assert _vote(client, "nope", "chair", "yes").status_code == 404


def test_non_member_forbidden_on_closed_board(client) -> None:
    service._roster = roles.CouncilRoster(
        members={"chair": roles.CouncilMember("chair", "chair")},
        open_membership=False,
    )
    m = _motion(client)
    assert _vote(client, m["id"], "chair", "yes").status_code == 201
    assert _vote(client, m["id"], "stranger", "yes").status_code == 403


def test_inactive_human_seat_forbidden(client) -> None:
    service._roster = roles.CouncilRoster(
        members={
            "chair": roles.CouncilMember("chair", "chair"),
            "guest": roles.CouncilMember("guest", "advisor", kind="human", can_vote=False),
        },
        open_membership=False,
    )
    m = _motion(client)
    assert _vote(client, m["id"], "guest", "yes").status_code == 403


# --- closing: quorum, tally, explainability, immutability -----------------


def test_close_unmet_quorum_returns_409(client) -> None:
    m = _motion(client, quorum=3)
    _vote(client, m["id"], "chair", "yes")
    r = client.post(f"/api/v1/council/motions/{m['id']}/close")
    assert r.status_code == 409


def test_close_tallies_records_roles_and_journal(client) -> None:
    m = _motion(client, quorum=3)
    _vote(client, m["id"], "chair", "yes")
    _vote(client, m["id"], "security", "yes")
    _vote(client, m["id"], "product", "no")
    r = client.post(f"/api/v1/council/motions/{m['id']}/close")
    assert r.status_code == 200, r.text
    record = r.json()
    dec = record["decision"]
    assert dec["outcome"] == "approved"
    assert dec["tally"] == {"yes": 2, "no": 1, "abstain": 0}
    # roles of every voter are carried (acceptance)
    assert {r["role"] for r in dec["roles"]} == {"chair", "security", "product"}
    # explainability: rationale explains the outcome + tally
    assert "approved" in dec["rationale"] and "quorum" in dec["rationale"].lower()
    # full journal carried
    assert "decision_recorded" in [e["event_type"] for e in record["journal"]]


def test_tie_defers(client) -> None:
    m = _motion(client, quorum=2)
    _vote(client, m["id"], "chair", "yes")
    _vote(client, m["id"], "security", "no")
    dec = client.post(f"/api/v1/council/motions/{m['id']}/close").json()["decision"]
    assert dec["outcome"] == "deferred"


def test_decision_is_immutable_no_reclose_or_revote(client) -> None:
    m = _motion(client, quorum=1)
    _vote(client, m["id"], "chair", "yes")
    assert client.post(f"/api/v1/council/motions/{m['id']}/close").status_code == 200
    # re-close is refused
    assert client.post(f"/api/v1/council/motions/{m['id']}/close").status_code == 409
    # a further vote is refused
    assert _vote(client, m["id"], "product", "yes").status_code == 409


# --- decisions surface ----------------------------------------------------


def test_list_decisions_carries_roles_and_journal(client) -> None:
    m = _motion(client, quorum=1)
    _vote(client, m["id"], "chair", "yes")
    client.post(f"/api/v1/council/motions/{m['id']}/close")
    body = client.get("/api/v1/council/decisions").json()
    assert len(body["decisions"]) == 1
    rec = body["decisions"][0]
    assert rec["decision"]["roles"] and rec["journal"]
    approved = client.get("/api/v1/council/decisions", params={"outcome": "approved"}).json()
    assert len(approved["decisions"]) == 1
    rejected = client.get("/api/v1/council/decisions", params={"outcome": "rejected"}).json()
    assert rejected["decisions"] == []
