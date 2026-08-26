"""Endpoint tests for the Wave-3 model-registry surface
(``command_center.api.model_registry_routes`` → ``model_registry_service`` →
``runtime.db.model_registry``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox
and resets its contents between cases, so the runtime db the service writes is
throwaway. A fresh :class:`EventBus` is installed per test so published events
are observable and never leak. The downloader is the network-free stub — no test
touches the network.

Fixtures use only generic names and invented ids — no real names or paths —
keeping the public-repo privacy gate green.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api import model_registry_service as service
from command_center.api.app import create_app
from command_center.events import (
    Event,
    ModelAssigned,
    ModelRegistered,
    ModelStatusChanged,
    default_bus,
)
from command_center.models_registry import StubDownloader
from command_center.models_registry.policy import SensitiveModelRoutingError


@pytest.fixture(autouse=True)
def stub_downloader(monkeypatch) -> None:
    """Deterministic, network-free downloader for every test on this surface."""
    monkeypatch.setattr(service, "_downloader", StubDownloader(steps=4))


@pytest.fixture
def events():
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


def _register(client, **kw) -> dict:
    body = {"name": "M", "kind": "external"}
    body.update(kw)
    r = client.post("/api/v1/models", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --- register / list / get ------------------------------------------------


def test_register_persists_and_emits_event(client, events) -> None:
    m = _register(client, name="GPT-ish", kind="external", provider="acme")
    assert m["id"] and m["kind"] == "external" and m["status"] == "available"
    assert any(isinstance(e, ModelRegistered) for e in events)


def test_list_filters_by_kind_and_status(client) -> None:
    _register(client, kind="external")
    _register(client, name="Loc", kind="local")
    all_body = client.get("/api/v1/models").json()
    assert all_body["limit"] == 100 and len(all_body["models"]) == 2
    local = client.get("/api/v1/models", params={"kind": "local"}).json()
    assert len(local["models"]) == 1 and local["models"][0]["kind"] == "local"


def test_get_unknown_is_404(client) -> None:
    assert client.get("/api/v1/models/nope").status_code == 404


# --- download lifecycle ---------------------------------------------------


def test_download_local_runs_lifecycle_to_installed(client, events) -> None:
    m = _register(client, name="Loc", kind="local", provenance="src://loc")
    r = client.post(f"/api/v1/models/{m['id']}/download", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"]["status"] == "installed"
    assert body["model"]["download_progress"] == 100
    assert body["progress"] == [25, 50, 75, 100]
    assert any(
        isinstance(e, ModelStatusChanged) and e.status == "installed" for e in events
    )
    # the governance log captured the whole transfer, not just endpoints
    hist = client.get(f"/api/v1/models/{m['id']}/history").json()
    actions = [e["action"] for e in hist["events"]]
    assert actions.count("download-progress") == 4
    assert actions[0] == "register" and actions[1] == "download-request"
    assert actions[-1] == "status-change"


def test_download_external_is_409(client) -> None:
    m = _register(client, kind="external")
    r = client.post(f"/api/v1/models/{m['id']}/download", json={})
    assert r.status_code == 409


def test_download_failure_marks_error(client, monkeypatch) -> None:
    monkeypatch.setattr(service, "_downloader", StubDownloader(steps=4, fail_at=50))
    m = _register(client, name="Loc", kind="local")
    body = client.post(f"/api/v1/models/{m['id']}/download", json={}).json()
    assert body["model"]["status"] == "error"
    assert body["progress"] == [25]  # got one tick before the failure


def test_download_unknown_is_404(client) -> None:
    assert client.post("/api/v1/models/nope/download", json={}).status_code == 404


# --- assignment + sensitive guard -----------------------------------------


def test_assign_records_governance_and_emits_event(client, events) -> None:
    m = _register(client, name="Loc", kind="local")
    r = client.post(
        f"/api/v1/models/{m['id']}/assign",
        json={"target_ref": "task:T1", "sensitive": False},
    )
    assert r.status_code == 200, r.text
    assert r.json()["target_ref"] == "task:T1"
    assert any(isinstance(e, ModelAssigned) for e in events)
    hist = client.get(f"/api/v1/models/{m['id']}/history").json()
    assign = [e for e in hist["events"] if e["action"] == "assign"]
    assert len(assign) == 1 and assign[0]["target_ref"] == "task:T1"


def test_sensitive_context_rejects_external_model(client) -> None:
    m = _register(client, name="Ext", kind="external")
    r = client.post(
        f"/api/v1/models/{m['id']}/assign",
        json={"target_ref": "task:S", "sensitive": True},
    )
    assert r.status_code == 400
    # the rejected assignment left NO trace in the governance log
    hist = client.get(f"/api/v1/models/{m['id']}/history").json()
    assert [e["action"] for e in hist["events"]] == ["register"]


def test_sensitive_context_allows_local_model(client) -> None:
    m = _register(client, name="Loc", kind="local")
    r = client.post(
        f"/api/v1/models/{m['id']}/assign",
        json={"target_ref": "task:S", "sensitive": True},
    )
    assert r.status_code == 200


def test_assign_unknown_is_404(client) -> None:
    r = client.post(
        "/api/v1/models/nope/assign", json={"target_ref": "task:X"}
    )
    assert r.status_code == 404


# --- history / provenance traceability ------------------------------------


def test_history_is_full_ordered_and_carries_provenance(client) -> None:
    m = _register(client, name="Loc", kind="local", provenance="src://loc")
    client.post(f"/api/v1/models/{m['id']}/download", json={"provenance": "src://loc"})
    client.post(
        f"/api/v1/models/{m['id']}/assign",
        json={"target_ref": "task:T1", "provenance": "auto-select"},
    )
    hist = client.get(f"/api/v1/models/{m['id']}/history").json()
    assert hist["model_id"] == m["id"]
    seqs = [e["seq"] for e in hist["events"]]
    assert seqs == sorted(seqs) and seqs[0] == 1  # gap-free ascending order
    dl_request = next(e for e in hist["events"] if e["action"] == "download-request")
    assert dl_request["provenance"] == "src://loc"
    assign = next(e for e in hist["events"] if e["action"] == "assign")
    assert assign["provenance"] == "auto-select"


def test_history_unknown_is_404(client) -> None:
    assert client.get("/api/v1/models/nope/history").status_code == 404


# --- auto-select + record_use (service helpers) ---------------------------


def test_auto_select_prefers_local(client) -> None:
    _register(client, name="Ext", kind="external", cost=0.0)
    loc = _register(client, name="Loc", kind="local", cost=9.0)
    chosen = service.auto_select_model()
    assert chosen is not None and chosen.id == loc["id"]


def test_record_use_appends_use_event_and_guards(client) -> None:
    m = _register(client, name="Loc", kind="local")
    ev = service.record_use(m["id"], target_ref="task:T1")
    assert ev is not None and ev.action == "use"
    ext = _register(client, name="Ext", kind="external")
    with pytest.raises(SensitiveModelRoutingError):
        service.record_use(ext["id"], target_ref="task:S", sensitive=True)
