"""Hermetic tests for the `/api/v1/dispatch/*` controller.

The router delegates to `command_center.dispatch.service` / `.repository`,
referenced as module globals in `command_center.dispatch.api`, so every test
monkeypatches those before issuing a request — no real board, runtime.db or
policy file is ever touched, and the controller is proven to be a thin
pass-through (no business logic of its own).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from fastapi.testclient import TestClient

import command_center.dispatch.api as apimod
from command_center import pipeline_settings, tasks_repository, task_pipeline
from command_center.dispatch import service as dispatch_service
from command_center.dispatch.models import (
    ASSIGNED,
    DispatchDecision,
    DispatchPlan,
    DispatchPolicy,
    ExecutorProfile,
)
from command_center.webapi.app import create_app

# `AICC_DATA_DIR` (session conftest) redirects all board storage, so the root
# handed to the service is an inert sentinel.
_ROOT = Path("/unused-AICC_DATA_DIR-overrides")


def _client() -> TestClient:
    return TestClient(create_app())


def _sample_plan() -> DispatchPlan:
    decision = DispatchDecision(
        task_id="t1", project="AICC", priority="High", reason=ASSIGNED,
        assigned_executor="ollama", estimated_cost_usd=0.0,
    )
    return DispatchPlan(
        decisions=(decision,), kill_switch_engaged=False,
        daily_spend_usd=0.0, max_daily_spend_usd=5.0, projected_spend_usd=0.0,
    )


def test_get_plan_returns_serialized_plan(monkeypatch):
    monkeypatch.setattr(apimod._service, "plan", lambda root: _sample_plan())

    body = _client().get("/api/v1/dispatch/plan").json()

    assert body["assignment_count"] == 1
    assert body["decisions"][0]["assigned_executor"] == "ollama"
    assert body["budget_remaining_usd"] == 5.0


def test_post_assign_forwards_confirmation(monkeypatch, authenticated_caller):
    captured: dict = {}

    def fake_assign(root, principal, *, confirmed):
        captured["confirmed"] = confirmed
        captured["principal"] = principal
        return {"applied": confirmed, "assigned_task_ids": ["t1"]}

    monkeypatch.setattr(apimod._service, "assign", fake_assign)

    r = _client().post("/api/v1/dispatch/assign", json={"confirmed": True})

    assert r.status_code == 200
    assert r.json()["applied"] is True
    # The recorded actor is the authenticated caller and nothing else: there is
    # no `actor` field left for a client to supply (VOYN-W0-AICC-AUTH-HTTP-01).
    assert captured == {"confirmed": True, "principal": authenticated_caller}


def test_post_assign_refuses_a_body_that_declares_an_actor(monkeypatch):
    """`extra="forbid"` — a forged actor is refused out loud, not ignored.

    Ignoring it would be safe but silent: a client that has been sending one
    keeps getting 200s and never learns the contract changed.
    """
    called: list = []
    monkeypatch.setattr(
        apimod._service, "assign", lambda *a, **k: called.append(k) or {}
    )

    r = _client().post(
        "/api/v1/dispatch/assign", json={"confirmed": True, "actor": "op"}
    )

    assert r.status_code == 422
    assert called == []


def test_post_assign_defaults_to_unconfirmed(monkeypatch):
    captured: dict = {}

    def fake_assign(root, principal, *, confirmed):
        captured["confirmed"] = confirmed
        return {"applied": False, "reason": "confirmation_required"}

    monkeypatch.setattr(apimod._service, "assign", fake_assign)

    r = _client().post("/api/v1/dispatch/assign", json={})

    assert r.status_code == 200
    assert captured["confirmed"] is False


def test_get_policy_returns_serialized_policy(monkeypatch):
    monkeypatch.setattr(
        apimod._policy_config,
        "load_policy",
        lambda root: DispatchPolicy(prefer_local=False, cost_matrix={"ollama": 0.0}),
    )

    body = _client().get("/api/v1/dispatch/policy").json()

    assert body["prefer_local"] is False
    assert body["cost_matrix"] == {"ollama": 0.0}


def test_put_policy_forwards_changes(monkeypatch, authenticated_caller):
    captured: dict = {}

    def fake_update(root, changes, *, principal):
        captured["changes"] = changes
        captured["principal"] = principal
        return DispatchPolicy(prefer_local=False)

    monkeypatch.setattr(apimod._policy_config, "update_policy", fake_update)

    r = _client().put(
        "/api/v1/dispatch/policy", json={"changes": {"prefer_local": False}}
    )

    assert r.status_code == 200
    assert r.json()["prefer_local"] is False
    assert captured["changes"] == {"prefer_local": False}
    assert captured["principal"] == authenticated_caller


def test_put_policy_refuses_a_body_that_declares_an_actor(monkeypatch):
    called: list = []
    monkeypatch.setattr(
        apimod._policy_config, "update_policy", lambda *a, **k: called.append(k)
    )

    r = _client().put(
        "/api/v1/dispatch/policy",
        json={"changes": {"prefer_local": False}, "actor": "editor"},
    )

    assert r.status_code == 422
    assert called == []


def test_put_policy_no_longer_accepts_a_bare_body(monkeypatch):
    """The old handler treated any unwrapped body as `changes`.

    That form was indistinguishable from a body carrying an unexpected
    top-level key — which is precisely what now has to be refused, so the
    convenience had to go with it.
    """
    called: list = []
    monkeypatch.setattr(
        apimod._policy_config, "update_policy", lambda *a, **k: called.append(k)
    )

    r = _client().put("/api/v1/dispatch/policy", json={"prefer_local": True})

    assert r.status_code == 422
    assert called == []


# --- redaction: a BANK task never surfaces in /dispatch/plan --------------


def test_get_plan_never_leaks_a_sensitive_project_task(monkeypatch):
    """End-to-end through the *real* service: a BANK task on the board must
    never appear (by id or by project) in the /dispatch/plan response, while a
    non-sensitive task in the same window is planned normally."""
    # Point the controller at the sandboxed board and drive the real service.
    monkeypatch.setattr(apimod, "_root", lambda: _ROOT)
    # Deterministic inputs so the pipeline runs without touching real CLIs/db.
    monkeypatch.setattr(
        dispatch_service,
        "collect_executor_pool",
        lambda policy: [
            ExecutorProfile(
                id="ollama", label="Ollama", kind="cli", is_local=True,
                available=True, cost_per_task_usd=0.0,
            )
        ],
    )
    monkeypatch.setattr(dispatch_service, "active_by_executor", lambda db_path: {})
    monkeypatch.setattr(task_pipeline, "daily_spend_usd", lambda *_a, **_k: 0.0)
    settings = pipeline_settings.load_settings(_ROOT)
    pipeline_settings.save_settings(_ROOT, dataclasses.replace(settings, enabled=True))

    bank = tasks_repository.create_task(
        _ROOT, project="BANK", title="wire reconciliation",
        task_type="implementation", status="Backlog",
    )
    ok = tasks_repository.create_task(
        _ROOT, project="AICC", title="ship dispatch",
        task_type="implementation", status="Backlog",
    )

    r = _client().get("/api/v1/dispatch/plan")
    assert r.status_code == 200, r.text
    body = r.json()
    task_ids = {d["task_id"] for d in body["decisions"]}
    projects = {d["project"] for d in body["decisions"]}

    assert ok["id"] in task_ids
    assert bank["id"] not in task_ids
    assert "BANK" not in projects
    # Belt and suspenders: the sensitive id/ref appears nowhere in the payload.
    assert bank["id"] not in r.text
    assert "BANK" not in r.text
