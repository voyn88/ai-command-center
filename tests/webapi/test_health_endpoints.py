"""Liveness and readiness endpoints (VOYN-W0-AICC-SRV-01a).

The important behaviours are the ones that only show up during an outage:
liveness must stay green when the database is gone (otherwise every replica is
killed at once), readiness must go red, and neither may leak connection
details into a payload that is typically served unauthenticated.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.db import health
from command_center.webapi.app import create_app


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_healthz_is_ok_without_a_database(client: TestClient) -> None:
    """No PostgreSQL is configured in this test process — liveness still passes."""
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "checks": {"process": "ok"}}


def test_readyz_is_503_when_the_database_is_unreachable(client: TestClient) -> None:
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["database"] == "unreachable"


def test_readyz_payload_never_leaks_connection_details(client: TestClient) -> None:
    """Probe endpoints are usually unauthenticated; the body must stay boring."""
    body = client.get("/readyz").text
    for secret in ("password", "dbname", "sslmode", "@", "postgresql://"):
        assert secret not in body


def test_readyz_reports_schema_mismatch_as_degraded(client: TestClient, monkeypatch) -> None:
    """A process talking to a newer schema returns wrong answers, not errors."""

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            return None

        def fetchone(self):
            return (1,)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(health.pool, "connection", lambda: _Conn())
    monkeypatch.setattr(
        health.migrations, "current_version", lambda conn: health.EXPECTED_SCHEMA_VERSION + 1
    )
    monkeypatch.setattr(health.pool, "pool_stats", dict)

    response = client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "schema_mismatch"


def test_readyz_is_200_when_schema_matches(client: TestClient, monkeypatch) -> None:
    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, *args, **kwargs):
            return None

        def fetchone(self):
            return (1,)

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def cursor(self):
            return _Cursor()

    monkeypatch.setattr(health.pool, "connection", lambda: _Conn())
    monkeypatch.setattr(
        health.migrations, "current_version", lambda conn: health.EXPECTED_SCHEMA_VERSION
    )
    monkeypatch.setattr(health.pool, "pool_stats", lambda: {"pool_size": 2})

    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["schema_version"] == health.EXPECTED_SCHEMA_VERSION
    assert body["checks"]["pool_size"] == 2
