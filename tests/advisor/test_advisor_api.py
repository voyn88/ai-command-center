"""Endpoint tests for the advisor API (``/api/v1/advisor/*``).

Hermetic via the shared ``AICC_DATA_DIR`` sandbox and a per-test bus reset. A
fake collector is injected by monkeypatching the registry the default engine
builds, so ``POST /advisor/run`` exercises the whole controller→service→repo
path without depending on ambient run history.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.advisor import service as advisor_service
from command_center.advisor.collectors.base import Collector
from command_center.advisor.registry import CollectorRegistry
from command_center.advisor.types import Candidate, CollectorContext
from command_center.api.app import create_app
from command_center.events import Event, ProposalCreated, default_bus


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture
def events() -> list[Event]:
    captured: list[Event] = []
    bus = default_bus()
    bus.clear()
    unsubscribe = bus.subscribe(Event, captured.append)
    try:
        yield captured
    finally:
        unsubscribe()
        bus.clear()


class _FakeCollector(Collector):
    name = "fake"
    kind = "feedback"

    def collect(self, ctx: CollectorContext) -> list[Candidate]:
        return [
            Candidate(kind="feedback", title="Investigate flakes", project_ref="AICC"),
            Candidate(kind="feedback", title="Investigate flakes", project_ref="BANK"),
        ]


@pytest.fixture
def _inject_fake_collector(monkeypatch) -> None:
    def _registry() -> CollectorRegistry:
        reg = CollectorRegistry()
        reg.register("fake", _FakeCollector)
        return reg

    monkeypatch.setattr(advisor_service, "default_registry", _registry)


def test_run_on_empty_history_returns_summary(client) -> None:
    r = client.post("/api/v1/advisor/run", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 0
    assert body["collectors"]  # the default registry's collectors are named


def test_run_persists_and_lists_proposals(client, events, _inject_fake_collector) -> None:
    r = client.post("/api/v1/advisor/run", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    # BANK candidate is dropped by the privacy filter; only AICC persists.
    assert body["created"] == 1
    assert body["skipped_sensitive"] == 1
    assert body["by_kind"] == {"feedback": 1}
    assert [e for e in events if isinstance(e, ProposalCreated)]

    listed = client.get("/api/v1/advisor/proposals", params={"kind": "feedback"})
    assert listed.status_code == 200
    titles = [p["title"] for p in listed.json()["proposals"]]
    assert titles == ["Investigate flakes"]


def test_advisor_proposals_filter_by_kind(client, _inject_fake_collector) -> None:
    client.post("/api/v1/advisor/run", json={})
    # A kind with no proposals returns an empty page, not an error.
    empty = client.get("/api/v1/advisor/proposals", params={"kind": "trend"})
    assert empty.status_code == 200
    assert empty.json()["proposals"] == []


def test_run_with_unknown_collector_is_a_client_error(client) -> None:
    # An unknown collector name is a client error, surfaced as 400 — never a
    # silent success with a bogus collector list.
    r = client.post("/api/v1/advisor/run", json={"collectors": ["does-not-exist"]})
    assert r.status_code == 400
