"""Endpoint tests for the arena API (``/api/v1/arena/duel``)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _variant(agent_id: str, **overrides) -> dict:
    defaults = dict(
        output="42",
        rationale="because",
        correct=True,
        quality=0.5,
        explainability=0.5,
        duration_seconds=10.0,
        cost_usd=0.01,
    )
    defaults.update(overrides)
    return {"agent_id": agent_id, **defaults}


def test_run_duel_ranks_and_declares_a_winner(client) -> None:
    r = client.post(
        "/api/v1/arena/duel",
        json={
            "case_id": "case-1",
            "case_prompt": "reverse a linked list",
            "variants": [
                _variant("best", quality=0.9, explainability=0.9, duration_seconds=1.0, cost_usd=0.001),
                _variant("middle"),
                _variant("worst", correct=False, quality=0.1, explainability=0.1),
            ],
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["case_id"] == "case-1"
    assert body["winner_agent_id"] == "best"
    assert [v["agent_id"] for v in body["ranking"]] == ["best", "middle", "worst"]
    assert [v["rank"] for v in body["ranking"]] == [1, 2, 3]


def test_run_duel_with_fewer_than_three_variants_is_a_client_error(client) -> None:
    r = client.post(
        "/api/v1/arena/duel",
        json={
            "case_id": "case-1",
            "case_prompt": "reverse a linked list",
            "variants": [_variant("a"), _variant("b")],
        },
    )
    assert r.status_code == 400
