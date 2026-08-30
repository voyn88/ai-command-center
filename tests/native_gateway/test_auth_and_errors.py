"""Auth, version negotiation, rate limiting and the safe error envelope."""

from __future__ import annotations

import json

from native_gateway.provision import revoke

from .conftest import auth_headers


def _assert_safe_error(response, status: int, code: str) -> None:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "traceId"}
    assert body["error"]["code"] == code
    # No infrastructure detail may leak through an error.
    lowered = response.text.lower()
    for needle in ("traceback", "postgres", "/users/", "exception", "sqlalch"):
        assert needle not in lowered


def test_unauthorized_without_token(client):
    response = client.get(
        "/v1/snapshot",
        headers={"Accept": "application/json", "X-AICC-Client-Version": "1.0"},
    )
    _assert_safe_error(response, 401, "unauthorized")
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_unauthorized_with_wrong_token(client):
    response = client.get("/v1/snapshot", headers=auth_headers("not-a-real-token"))
    _assert_safe_error(response, 401, "unauthorized")


def test_unauthorized_when_device_disabled(client, device_token, registry_path):
    """Revocation actually bites: a device revoked through the real operator
    lever is refused on its very next request, with no cache to outlast it."""
    assert revoke(registry_path, "mac-owner-01", "lost device") is True

    response = client.get("/v1/snapshot", headers=auth_headers(device_token))

    _assert_safe_error(response, 401, "unauthorized")


def test_forbidden_when_scope_is_not_read(client, device_token, registry_path):
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["devices"][0]["scope"] = "admin"
    registry_path.write_text(json.dumps(registry), encoding="utf-8")
    response = client.get("/v1/snapshot", headers=auth_headers(device_token))
    _assert_safe_error(response, 403, "forbidden")


def test_422_when_client_version_missing(client, device_token):
    headers = auth_headers(device_token)
    del headers["X-AICC-Client-Version"]
    response = client.get("/v1/snapshot", headers=headers)
    _assert_safe_error(response, 422, "client_version_required")


def test_422_when_client_version_unsupported(client, device_token):
    response = client.get(
        "/v1/snapshot", headers=auth_headers(device_token, version="2.0")
    )
    _assert_safe_error(response, 422, "unsupported_client_version")


def test_422_when_accept_excludes_json(client, device_token):
    headers = {**auth_headers(device_token), "Accept": "text/html"}
    response = client.get("/v1/snapshot", headers=headers)
    _assert_safe_error(response, 422, "unsupported_accept")


def test_404_unknown_task_is_safe(client, device_token):
    response = client.get("/v1/tasks/NOPE-000", headers=auth_headers(device_token))
    _assert_safe_error(response, 404, "not_found")


def test_404_unknown_route_is_safe(client, device_token):
    response = client.get("/v1/does-not-exist", headers=auth_headers(device_token))
    _assert_safe_error(response, 404, "not_found")


def test_write_methods_do_not_exist(client, device_token):
    response = client.post("/v1/snapshot", headers=auth_headers(device_token))
    assert response.status_code == 405
    assert set(response.json()) == {"error"}


def test_429_after_rate_limit(settings, device_token, projection_path, registry_path):
    from fastapi.testclient import TestClient

    from native_gateway.app import GatewayRuntime, create_app
    from native_gateway.auth import DeviceRegistry
    from native_gateway.ratelimit import RateLimiter
    from native_gateway.source import FileProjectionSource

    runtime = GatewayRuntime(
        settings=settings,
        source=FileProjectionSource(settings),
        registry=DeviceRegistry(registry_path),
        limiter=RateLimiter(max_requests=2, window_s=60),
    )
    limited = TestClient(create_app(runtime), raise_server_exceptions=False)
    assert (
        limited.get("/v1/snapshot", headers=auth_headers(device_token)).status_code
        == 200
    )
    assert (
        limited.get("/v1/snapshot", headers=auth_headers(device_token)).status_code
        == 200
    )
    response = limited.get("/v1/snapshot", headers=auth_headers(device_token))
    _assert_safe_error(response, 429, "rate_limited")
    assert int(response.headers["Retry-After"]) >= 1


def test_500_is_opaque_when_source_raises(client, device_token):
    class Exploding:
        def load(self, now=None):
            raise RuntimeError(
                "dsn=postgres://user:password@db/aios"
            )  # must never leak

    client.app.state.runtime.source = Exploding()
    response = client.get("/v1/snapshot", headers=auth_headers(device_token))
    _assert_safe_error(response, 500, "internal")
