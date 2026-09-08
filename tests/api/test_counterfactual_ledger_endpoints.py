"""Endpoint + acceptance tests for the Counterfactual Ledger surface
(``command_center.api.counterfactual_ledger_routes`` →
``counterfactual_ledger.service`` → ``runtime.db.counterfactual_ledger``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox
and resets its contents between cases, so the runtime db the service writes is
throwaway. Auth: ``tests/api/conftest.py`` makes every request here act as an
authenticated, fully-granted caller (see ``tests/http_auth_fixture.py``).

The acceptance bar this file exists to prove (VOYN-MIN-COMP): a ``critical``
decision may only finalize once it carries at least 3 recorded alternatives.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api.app import create_app
from command_center.counterfactual_ledger.service import ROOT
from command_center.runtime import db
from command_center.runtime.db.core import resolve_db_path


@pytest.fixture(autouse=True)
def _migrated_db() -> None:
    db.migrate(resolve_db_path(ROOT))


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _decision(client: TestClient, **overrides) -> dict:
    payload = {"title": "Pick a database", "criticality": "normal"}
    payload.update(overrides)
    r = client.post("/api/v1/decisions", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def _alternative(client: TestClient, decision_id: str, option: str, **extra) -> dict:
    body = {"option": option, "rejection_reason": "too costly"}
    body.update(extra)
    r = client.post(f"/api/v1/decisions/{decision_id}/alternatives", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- decisions: create / list / get ----------------------------------------


def test_create_and_get_decision_round_trips() -> None:
    client = TestClient(create_app())
    created = _decision(client, title="Choose cloud provider", owner="alice")
    assert created["status"] == "draft"
    assert created["criticality"] == "normal"

    fetched = client.get(f"/api/v1/decisions/{created['id']}").json()
    assert fetched == created


def test_get_unknown_decision_is_404() -> None:
    client = TestClient(create_app())
    r = client.get("/api/v1/decisions/does-not-exist")
    assert r.status_code == 404


def test_create_decision_requires_a_title() -> None:
    client = TestClient(create_app())
    r = client.post("/api/v1/decisions", json={"title": "   "})
    assert r.status_code == 422


def test_create_decision_rejects_bad_criticality() -> None:
    client = TestClient(create_app())
    r = client.post(
        "/api/v1/decisions", json={"title": "X", "criticality": "urgent"}
    )
    assert r.status_code == 422


def test_list_decisions_filters_by_criticality_and_status() -> None:
    client = TestClient(create_app())
    normal = _decision(client, title="Normal one", criticality="normal")
    critical = _decision(client, title="Critical one", criticality="critical")

    only_critical = client.get(
        "/api/v1/decisions", params={"criticality": "critical"}
    ).json()
    ids = {d["id"] for d in only_critical["decisions"]}
    assert critical["id"] in ids
    assert normal["id"] not in ids

    only_draft = client.get("/api/v1/decisions", params={"status": "draft"}).json()
    draft_ids = {d["id"] for d in only_draft["decisions"]}
    assert {normal["id"], critical["id"]} <= draft_ids


# --- alternatives -----------------------------------------------------------


def test_add_and_list_alternatives() -> None:
    client = TestClient(create_app())
    decision = _decision(client)
    _alternative(client, decision["id"], "Postgres")
    _alternative(client, decision["id"], "MySQL")

    listed = client.get(f"/api/v1/decisions/{decision['id']}/alternatives").json()
    options = {a["option"] for a in listed["alternatives"]}
    assert options == {"Postgres", "MySQL"}


def test_add_alternative_to_unknown_decision_is_404() -> None:
    client = TestClient(create_app())
    r = client.post(
        "/api/v1/decisions/does-not-exist/alternatives",
        json={"option": "X"},
    )
    assert r.status_code == 404


def test_add_alternative_requires_a_non_empty_option() -> None:
    client = TestClient(create_app())
    decision = _decision(client)
    r = client.post(
        f"/api/v1/decisions/{decision['id']}/alternatives",
        json={"option": "  "},
    )
    assert r.status_code == 422


# --- workflow: finalize ------------------------------------------------------


def test_normal_decision_finalizes_with_zero_alternatives() -> None:
    client = TestClient(create_app())
    decision = _decision(client, criticality="normal")

    r = client.post(
        f"/api/v1/decisions/{decision['id']}/finalize",
        json={"chosen_option": "Postgres", "rationale": "best fit"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "finalized"
    assert body["chosen_option"] == "Postgres"
    assert body["rationale"] == "best fit"
    assert body["decided_at"] is not None


def test_critical_decision_refuses_to_finalize_below_three_alternatives() -> None:
    client = TestClient(create_app())
    decision = _decision(client, criticality="critical")

    # Zero alternatives: refused.
    r = client.post(
        f"/api/v1/decisions/{decision['id']}/finalize",
        json={"chosen_option": "Postgres"},
    )
    assert r.status_code == 409

    # One and two alternatives: still refused.
    _alternative(client, decision["id"], "MySQL")
    r = client.post(
        f"/api/v1/decisions/{decision['id']}/finalize",
        json={"chosen_option": "Postgres"},
    )
    assert r.status_code == 409

    _alternative(client, decision["id"], "SQLite")
    r = client.post(
        f"/api/v1/decisions/{decision['id']}/finalize",
        json={"chosen_option": "Postgres"},
    )
    assert r.status_code == 409

    # Still finalized as draft — refusing the finalize must not have written
    # anything.
    fetched = client.get(f"/api/v1/decisions/{decision['id']}").json()
    assert fetched["status"] == "draft"


def test_critical_decision_finalizes_at_exactly_three_alternatives() -> None:
    """The acceptance bar: minimum 3 saved alternatives for 1 critical decision."""
    client = TestClient(create_app())
    decision = _decision(client, criticality="critical")

    _alternative(client, decision["id"], "MySQL", rejection_reason="weaker JSON support")
    _alternative(client, decision["id"], "SQLite", rejection_reason="no concurrent writers")
    _alternative(client, decision["id"], "MongoDB", rejection_reason="no strong schema")

    listed = client.get(f"/api/v1/decisions/{decision['id']}/alternatives").json()
    assert len(listed["alternatives"]) == 3

    r = client.post(
        f"/api/v1/decisions/{decision['id']}/finalize",
        json={"chosen_option": "Postgres", "rationale": "strong ACID + JSON support"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "finalized"
    assert body["chosen_option"] == "Postgres"


def test_finalize_unknown_decision_is_404() -> None:
    client = TestClient(create_app())
    r = client.post(
        "/api/v1/decisions/does-not-exist/finalize",
        json={"chosen_option": "X"},
    )
    assert r.status_code == 404


def test_finalize_requires_a_non_empty_chosen_option() -> None:
    client = TestClient(create_app())
    decision = _decision(client, criticality="normal")
    r = client.post(
        f"/api/v1/decisions/{decision['id']}/finalize",
        json={"chosen_option": "   "},
    )
    assert r.status_code == 422


def test_finalized_decision_refuses_new_alternatives() -> None:
    client = TestClient(create_app())
    decision = _decision(client, criticality="normal")
    client.post(
        f"/api/v1/decisions/{decision['id']}/finalize",
        json={"chosen_option": "Postgres"},
    )
    r = client.post(
        f"/api/v1/decisions/{decision['id']}/alternatives",
        json={"option": "MySQL"},
    )
    assert r.status_code == 409


def test_finalized_decision_cannot_finalize_again() -> None:
    client = TestClient(create_app())
    decision = _decision(client, criticality="normal")
    client.post(
        f"/api/v1/decisions/{decision['id']}/finalize",
        json={"chosen_option": "Postgres"},
    )
    r = client.post(
        f"/api/v1/decisions/{decision['id']}/finalize",
        json={"chosen_option": "MySQL"},
    )
    assert r.status_code == 409


# --- redaction: sensitive project_ref ----------------------------------------


def test_sensitive_project_ref_is_rejected_on_create() -> None:
    client = TestClient(create_app())
    r = client.post(
        "/api/v1/decisions",
        json={"title": "Rotate credentials", "project_ref": "BANK"},
    )
    assert r.status_code == 400
    listed = client.get("/api/v1/decisions").json()
    assert all(d["title"] != "Rotate credentials" for d in listed["decisions"])
