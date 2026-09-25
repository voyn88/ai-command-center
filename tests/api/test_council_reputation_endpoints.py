"""Endpoint tests for the reputation surface (VOYN-MIN-LINK-REPUTE):
``GET /council/votes/{id}/trust-score``, ``GET /council/reputation[/{voter_id}]``
and ``GET /council/reputation/coverage``.

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox
(see ``tests/api/test_council_endpoints.py`` for the shared fixture rationale).

Fixtures use only generic project codes (``AICC``, ``BANK``) and invented ids —
no real names or paths — keeping the public-repo privacy gate green.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api.app import create_app
from command_center.council.service import ROOT
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    db.migrate(resolve_db_path(ROOT))


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _motion(client: TestClient, **overrides) -> dict:
    payload = {"title": "Adopt X", "proposed_by": "chair", "quorum": 1}
    payload.update(overrides)
    r = client.post("/api/v1/council/motions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _vote(client: TestClient, motion_id: str, voter_id: str, choice: str, **extra) -> dict:
    body = {"voter_id": voter_id, "choice": choice}
    body.update(extra)
    r = client.post(f"/api/v1/council/motions/{motion_id}/vote", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _close(client: TestClient, motion_id: str) -> dict:
    r = client.post(f"/api/v1/council/motions/{motion_id}/close")
    assert r.status_code == 200, r.text
    return r.json()


# --- per-vote trust score ---------------------------------------------------


def test_trust_score_on_decided_motion_is_outcome_alignment(client) -> None:
    m = _motion(client, quorum=1)
    vote = _vote(client, m["id"], "chair", "yes")
    _close(client, m["id"])
    body = client.get(f"/api/v1/council/votes/{vote['id']}/trust-score").json()
    assert body["basis"] == "outcome_alignment"
    assert body["score"] == 100.0
    assert body["voter_id"] == "chair" and body["motion_id"] == m["id"]
    assert "approved" in body["explanation"]


def test_trust_score_on_open_motion_falls_back_to_voter_prior(client) -> None:
    decided = _motion(client, quorum=1)
    _vote(client, decided["id"], "chair", "yes")
    _close(client, decided["id"])

    open_motion = _motion(client, quorum=1, title="still open")
    open_vote = _vote(client, open_motion["id"], "chair", "yes")
    body = client.get(f"/api/v1/council/votes/{open_vote['id']}/trust-score").json()
    assert body["basis"] == "voter_prior"
    assert body["votes_considered"] == 1


def test_trust_score_on_open_motion_with_no_history_is_insufficient_data(client) -> None:
    m = _motion(client, quorum=1)
    vote = _vote(client, m["id"], "newcomer", "yes")
    body = client.get(f"/api/v1/council/votes/{vote['id']}/trust-score").json()
    assert body["basis"] == "insufficient_data"
    assert body["score"] is None


def test_trust_score_missing_vote_404(client) -> None:
    assert client.get("/api/v1/council/votes/nope/trust-score").status_code == 404


# --- voter reputation --------------------------------------------------------


def test_voter_reputation_aggregates_history(client) -> None:
    m1 = _motion(client, quorum=1)
    _vote(client, m1["id"], "chair", "yes")
    _close(client, m1["id"])
    m2 = _motion(client, quorum=1, title="second")
    _vote(client, m2["id"], "chair", "yes")
    _close(client, m2["id"])

    body = client.get("/api/v1/council/reputation/chair").json()
    assert body["basis"] == "history"
    assert body["votes_considered"] == 2
    assert body["score"] == 100.0
    assert body["alignment_rate"] == 1.0


def test_voter_reputation_missing_voter_404(client) -> None:
    assert client.get("/api/v1/council/reputation/nope").status_code == 404


def test_list_voter_reputations_pages_all_voters(client) -> None:
    m = _motion(client, quorum=2)
    _vote(client, m["id"], "chair", "yes")
    _vote(client, m["id"], "security", "no")
    _close(client, m["id"])
    body = client.get("/api/v1/council/reputation").json()
    voters = {r["voter_id"] for r in body["reputations"]}
    assert voters == {"chair", "security"}
    assert body["limit"] == 100 and body["offset"] == 0


# --- coverage acceptance metric ----------------------------------------------


def test_reputation_coverage_reports_totals(client) -> None:
    m = _motion(client, quorum=1)
    vote = _vote(client, m["id"], "chair", "yes")
    _close(client, m["id"])
    body = client.get("/api/v1/council/reputation/coverage").json()
    assert body["total_votes"] == 1
    assert body["explainable_votes"] == 1
    assert body["coverage"] == 1.0


def test_reputation_coverage_meets_acceptance_bar(client) -> None:
    """VOYN-MIN-LINK-REPUTE acceptance: at least 90% of votes carry an
    explainable trust score. Ten decided 3-vote motions (fully explainable)
    plus two brand-new voters' first votes on still-open motions (the only
    insufficient-data case) comfortably clears the bar."""
    for i in range(10):
        m = _motion(client, quorum=3, title=f"decided-{i}")
        _vote(client, m["id"], "chair", "yes")
        _vote(client, m["id"], "security", "yes")
        _vote(client, m["id"], "product", "no")
        _close(client, m["id"])
    for i in range(2):
        m = _motion(client, quorum=1, title=f"open-{i}")
        _vote(client, m["id"], f"newcomer-{i}", "yes")

    body = client.get("/api/v1/council/reputation/coverage").json()
    assert body["total_votes"] == 32
    assert body["coverage"] >= 0.9


# --- redaction: a sensitive motion's votes never surface ---------------------


def test_sensitive_motion_votes_excluded_from_reputation(client) -> None:
    # Sensitive motions can't be created through the API (rejected outright);
    # exercise the redaction path the way the intake seam would populate it —
    # directly through the repository, as the sibling motion-redaction test does.
    path = resolve_db_path(ROOT)
    sensitive = db.create_motion(path, title="secret", proposed_by="chair", project_ref="BANK")
    db.cast_vote(path, motion_id=sensitive["id"], voter_id="ghost", role="chair", choice="yes")

    assert client.get("/api/v1/council/reputation/ghost").status_code == 404
    body = client.get("/api/v1/council/reputation").json()
    assert "ghost" not in {r["voter_id"] for r in body["reputations"]}
    coverage = client.get("/api/v1/council/reputation/coverage").json()
    assert coverage["total_votes"] == 0
