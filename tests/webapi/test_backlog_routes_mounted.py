"""The Postgres-backed backlog surface is reachable from this app too
(VOYN-W0-APP-CONTROL-S6a/S6c/S6d).

`command_center.api.backlog_routes` / `backlog_intake_routes` /
`backlog_reassign_routes` were written for `command_center.api.app` (the
wave-1 app) and are now ALSO mounted on `command_center.webapi.app` — the one
process actually served to the owner's browser at the same origin as the SPA
(see the mount site in `webapi/app.py` and the count in
`tests/http_auth/test_routing_coverage.py`).

This suite only proves the wiring: a 404 here would mean the routes are not
mounted at all, which none of the other suites (written against
`command_center.api.app`) would catch. It does not re-prove backlog
semantics — `tests/api/test_backlog_intake_routes.py`,
`tests/api/test_backlog_reassign_routes.py` and the `BacklogStore` unit tests
already do that against the same, shared route modules — nor auth coverage,
which `tests/http_auth/test_routing_coverage.py` already sweeps for both
apps. This package's autouse `authenticated_caller` fixture (see
`tests/webapi/conftest.py`) makes every request here an authenticated, fully
granted principal, same as the rest of this suite.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.webapi.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_backlog_status_is_mounted_here_not_404(client):
    # No AICC_PG_HOST in the test process, so the pool guard fires: 503, not
    # the 404 a route that was never mounted on this app would give.
    assert client.get("/api/v1/backlog/status").status_code == 503


def test_backlog_tasks_is_mounted_here_not_404(client):
    assert client.get("/api/v1/backlog/tasks").status_code == 503


def test_backlog_intake_draft_reaches_the_handler_not_404(client, monkeypatch):
    import command_center.api.backlog_intake_routes as intake_mod

    monkeypatch.setattr(intake_mod, "_call_model", lambda prompt: "irrelevant")
    response = client.post("/api/v1/backlog/intake/draft", json={"text": "add a task"})
    # The pool guard only fires inside `confirm` (it writes); `draft` never
    # touches the store, so a stubbed model reply reaches a normal 200 here —
    # what matters is that it is not the 404 an unmounted route would give.
    assert response.status_code == 200


def test_backlog_reassign_is_mounted_here_not_404(client):
    response = client.post(
        "/api/v1/backlog/tasks/no-such-id/reassign",
        json={"wave": "W1", "priority": "P1", "expected_revision": 1},
    )
    assert response.status_code == 503
