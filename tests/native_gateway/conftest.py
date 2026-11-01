"""Shared fixtures: a gateway wired to a tmp projection + one provisioned device."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from native_gateway.app import GatewayRuntime, create_app
from native_gateway.auth import DeviceRegistry
from native_gateway.config import GatewaySettings
from native_gateway.provision import mint
from native_gateway.ratelimit import RateLimiter
from native_gateway.source import FileProjectionSource

SAMPLE = (
    Path(__file__).resolve().parents[2]
    / "native_gateway/fixtures/projection.sample.json"
)


def fresh_sample(projection_path: Path) -> dict:
    """Copy the sample projection with generated_at = now (so it is 'fresh')."""
    data = json.loads(SAMPLE.read_text(encoding="utf-8"))
    data["generated_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    projection_path.write_text(json.dumps(data), encoding="utf-8")
    return data


@pytest.fixture()
def projection_path(tmp_path: Path) -> Path:
    path = tmp_path / "projection.json"
    fresh_sample(path)
    return path


@pytest.fixture()
def registry_path(tmp_path: Path) -> Path:
    return tmp_path / "device_tokens.json"


@pytest.fixture()
def device_token(registry_path: Path) -> str:
    return mint(registry_path, "mac-owner-01", "Owner MacBook")


@pytest.fixture()
def settings(projection_path: Path, registry_path: Path) -> GatewaySettings:
    return GatewaySettings(projection_path=projection_path, token_file=registry_path)


@pytest.fixture()
def client(settings: GatewaySettings) -> TestClient:
    runtime = GatewayRuntime(
        settings=settings,
        source=FileProjectionSource(settings),
        registry=DeviceRegistry(settings.token_file),
        limiter=RateLimiter(settings.rate_limit_requests, settings.rate_limit_window_s),
    )
    return TestClient(create_app(runtime), raise_server_exceptions=False)


def auth_headers(token: str, version: str = "1.0") -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-AICC-Client-Version": version,
        "Authorization": f"Bearer {token}",
    }
