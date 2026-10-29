"""Remote-mode gateway client: parsing, error mapping, token hygiene.

Pure application-layer tests (no Qt): a fake transport stands in for the
network, and the snapshot body is produced by the gateway's own projection
mapper from the shared sample fixture, so the client is checked against the
real wire shape rather than a hand-copied one.
"""

from __future__ import annotations

import json
import socket
import ssl
from datetime import UTC, datetime
from pathlib import Path

import pytest

from command_center.application.remote_gateway import (
    FetchOutcome,
    GatewayConfig,
    HealthLevel,
    PinMismatchError,
    fetch_snapshot,
    fetch_with_config,
    health_level,
    normalize_pin,
    parse_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE = REPO_ROOT / "native_gateway" / "fixtures" / "projection.sample.json"
BASE = "https://control-01.example.ts.net:8443"
TOKEN = "device-token-secret-value"


def sample_snapshot() -> dict:
    """The sample projection, served the way `/v1/snapshot` serves it."""
    pytest.importorskip("pydantic")
    from native_gateway.config import GatewaySettings
    from native_gateway.source import FileProjectionSource

    source = FileProjectionSource(
        GatewaySettings(projection_path=FIXTURE, token_file=Path("unused"))
    )
    now = datetime(2026, 8, 26, 0, 1, tzinfo=UTC)  # 60 s after generated_at
    return source.load(now=now).snapshot.model_dump(mode="json")


class FakeTransport:
    def __init__(self, status: int = 200, body: bytes | None = None, exc=None):
        self.status = status
        self.body = body if body is not None else b"{}"
        self.exc = exc
        self.calls: list[tuple[str, dict, float, str | None]] = []

    def __call__(self, url, headers, timeout, pin):
        self.calls.append((url, dict(headers), timeout, pin))
        if self.exc is not None:
            raise self.exc
        return self.status, self.body


def _json_transport(payload: dict) -> FakeTransport:
    return FakeTransport(body=json.dumps(payload).encode("utf-8"))


def test_parses_pipeline_health_from_the_sample_fixture():
    transport = _json_transport(sample_snapshot())
    snap = fetch_snapshot(BASE, TOKEN, transport=transport)

    assert snap.outcome is FetchOutcome.OK
    assert snap.freshness == "fresh"
    assert snap.projection_age_seconds == 60
    health = snap.health
    assert health is not None
    assert (health.ready, health.claimed, health.dead) == (3, 2, 1)
    assert health.succeeded_last_hour == 7
    assert health.oldest_ready_age_seconds == 95
    assert [(lane.role, lane.attempts) for lane in health.lanes] == [("aicc_worker", 2)]
    assert health.auth_failures_30m == 0
    assert health.backlog_by_status["DEFER_TO_USER"] == 1
    assert [e.task_id for e in health.escalations] == ["VOYN-EXAMPLE-002"]
    assert health.monitor is not None and health.monitor.failing
    assert health.dead_growth
    # Escalation + failing monitor → attention, not ok.
    assert health_level(health) is HealthLevel.ATTENTION


def test_request_shape_and_token_placement():
    transport = _json_transport(sample_snapshot())
    fetch_snapshot(BASE + "/", TOKEN, pin_sha256="AA:bb", timeout=3, transport=transport)

    url, headers, timeout, pin = transport.calls[0]
    assert url == BASE + "/v1/snapshot"
    assert TOKEN not in url
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-AICC-Client-Version"] == "1.0"
    assert headers["Accept"] == "application/json"
    assert (timeout, pin) == (3, "AA:bb")


def test_token_never_appears_in_repr():
    config = GatewayConfig(BASE, TOKEN, None)
    assert TOKEN not in repr(config)


@pytest.mark.parametrize(
    ("transport", "outcome"),
    [
        (FakeTransport(exc=ConnectionRefusedError()), FetchOutcome.UNREACHABLE),
        (FakeTransport(exc=socket.timeout()), FetchOutcome.UNREACHABLE),
        (FakeTransport(exc=ssl.SSLError("bad cert")), FetchOutcome.UNREACHABLE),
        (FakeTransport(exc=PinMismatchError()), FetchOutcome.PIN_MISMATCH),
        (FakeTransport(status=401), FetchOutcome.UNAUTHORIZED),
        (FakeTransport(status=403), FetchOutcome.UNAUTHORIZED),
        (FakeTransport(status=429), FetchOutcome.HTTP_ERROR),
        (FakeTransport(status=500), FetchOutcome.HTTP_ERROR),
        (FakeTransport(body=b"not json"), FetchOutcome.INVALID_RESPONSE),
        (FakeTransport(body=b"[]"), FetchOutcome.INVALID_RESPONSE),
    ],
)
def test_errors_map_to_friendly_outcomes(transport, outcome):
    snap = fetch_snapshot(BASE, TOKEN, transport=transport)
    assert snap.outcome is outcome
    assert snap.health is None


def test_degraded_snapshot_keeps_health_but_flags_outcome():
    payload = sample_snapshot()
    payload["freshness"] = "degraded"
    snap = fetch_snapshot(BASE, TOKEN, transport=_json_transport(payload))
    assert snap.outcome is FetchOutcome.DEGRADED
    assert snap.health is not None


def test_missing_pipeline_health_block():
    payload = sample_snapshot()
    payload.pop("pipelineHealth")
    snap = parse_snapshot(payload)
    assert snap.outcome is FetchOutcome.NO_HEALTH
    assert snap.health is None


@pytest.mark.parametrize(
    ("url", "token"),
    [("", TOKEN), (BASE, ""), ("http://control-01:8443", TOKEN), ("control-01", TOKEN)],
)
def test_incomplete_or_plaintext_config_never_hits_the_network(url, token):
    transport = FakeTransport()
    assert fetch_snapshot(url, token, transport=transport).outcome is FetchOutcome.NOT_CONFIGURED
    assert transport.calls == []
    assert fetch_with_config(None, transport=transport).outcome is FetchOutcome.NOT_CONFIGURED


def test_health_levels():
    payload = sample_snapshot()
    block = payload["pipelineHealth"]
    block["ownerEscalations"] = []
    block["monitor"] = {"verdict": "ok", "openFindings": 0, "failures": []}
    assert health_level(parse_snapshot(payload).health) is HealthLevel.OK

    block["executorAuthFailures30m"] = 2
    assert health_level(parse_snapshot(payload).health) is HealthLevel.ATTENTION

    block["activeLanes"] = []
    assert health_level(parse_snapshot(payload).health) is HealthLevel.DOWN


def test_normalize_pin():
    assert normalize_pin(None) is None
    assert normalize_pin("  ") is None
    assert normalize_pin("AB:cd 12") == "abcd12"
