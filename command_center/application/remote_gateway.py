"""Read-only client for the AICC native gateway's ``GET /v1/snapshot``.

The desktop's remote mode: the owner watches the autonomous pipeline on
control-01 without a laptop session. This module is Qt-free (application
layer) and stdlib-only — ``http.client`` over TLS with the platform trust
store, plus an optional SHA-256 pin of the server certificate's DER bytes,
checked *before* the bearer token is sent.

`fetch_snapshot` never raises: every failure is mapped to a
:class:`FetchOutcome` the page renders as a friendly state. The device token is
never logged, never put in a URL and never included in a ``repr``.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import ssl
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

SNAPSHOT_PATH = "/v1/snapshot"
CLIENT_VERSION = "1.0"
DEFAULT_TIMEOUT = 10.0
MAX_BODY_BYTES = 4 * 1024 * 1024

# Named, not inline (ruff-format py314 target rewrites inline except-tuples).
_NUMBER_ERRORS = (TypeError, ValueError)


class PinMismatchError(ConnectionError):
    """The server certificate does not match the configured SHA-256 pin."""


#: ``transport(url, headers, timeout, pin_sha256) -> (status, body)``. Raises
#: :class:`PinMismatchError` on a pin mismatch and ``OSError`` (incl. ``ssl``
#: errors and timeouts) when the gateway cannot be reached.
Transport = Callable[[str, Mapping[str, str], float, "str | None"], tuple[int, bytes]]


class FetchOutcome(str, Enum):
    OK = "ok"
    NOT_CONFIGURED = "not_configured"
    UNREACHABLE = "unreachable"
    PIN_MISMATCH = "pin_mismatch"
    UNAUTHORIZED = "unauthorized"
    HTTP_ERROR = "http_error"
    INVALID_RESPONSE = "invalid_response"
    DEGRADED = "degraded"  # snapshot freshness degraded/offline
    NO_HEALTH = "no_health"  # snapshot has no pipelineHealth block


class HealthLevel(str, Enum):
    OK = "ok"
    ATTENTION = "attention"
    DOWN = "down"


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    token: str = field(repr=False)
    pin_sha256: str | None = None

    @property
    def is_complete(self) -> bool:
        return bool(self.base_url.strip() and self.token.strip())


@dataclass(frozen=True)
class Lane:
    role: str
    attempts: int
    heartbeat_age_seconds: int


@dataclass(frozen=True)
class Escalation:
    task_id: str
    title: str
    priority: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class MonitorVerdict:
    verdict: str
    open_findings: int
    failures: tuple[str, ...] = ()
    last_seen_at: str | None = None

    @property
    def failing(self) -> bool:
        return self.verdict != "ok" or bool(self.failures)


@dataclass(frozen=True)
class PipelineHealth:
    observed_at: str | None
    ready: int
    claimed: int
    dead: int
    succeeded_last_hour: int
    oldest_ready_age_seconds: int | None
    lanes: tuple[Lane, ...]
    auth_failures_30m: int
    backlog_by_status: Mapping[str, int]
    escalations: tuple[Escalation, ...]
    monitor: MonitorVerdict | None = None

    @property
    def dead_growth(self) -> bool:
        return self.monitor is not None and any(
            "dead" in code for code in self.monitor.failures
        )


@dataclass(frozen=True)
class RemoteSnapshot:
    outcome: FetchOutcome
    freshness: str | None = None
    generated_at: str | None = None
    projection_age_seconds: int | None = None
    health: PipelineHealth | None = None
    http_status: int | None = None


def health_level(health: PipelineHealth) -> HealthLevel:
    """ok / attention / down for the page's status header."""
    if not health.lanes:
        return HealthLevel.DOWN
    if (
        health.auth_failures_30m > 0
        or (health.monitor is not None and health.monitor.failing)
        or health.dead_growth
        or health.escalations
    ):
        return HealthLevel.ATTENTION
    return HealthLevel.OK


# --- parsing ------------------------------------------------------------------


def _int(value: object, default: int = 0) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except _NUMBER_ERRORS:
        return default


def _opt_int(value: object) -> int | None:
    return None if value is None else _int(value)


def _opt_str(value: object) -> str | None:
    return None if value in (None, "") else str(value)


def _list(value: object) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


def parse_pipeline_health(raw: object) -> PipelineHealth | None:
    if not isinstance(raw, dict):
        return None
    queue = raw.get("queue") if isinstance(raw.get("queue"), dict) else {}
    backlog = raw.get("backlogByStatus")
    monitor_raw = raw.get("monitor")
    monitor = None
    if isinstance(monitor_raw, dict):
        failures = monitor_raw.get("failures")
        monitor = MonitorVerdict(
            verdict=str(monitor_raw.get("verdict") or "ok"),
            open_findings=_int(monitor_raw.get("openFindings")),
            failures=tuple(str(f) for f in failures) if isinstance(failures, list) else (),
            last_seen_at=_opt_str(monitor_raw.get("lastSeenAt")),
        )
    return PipelineHealth(
        observed_at=_opt_str(raw.get("observedAt")),
        ready=_int(queue.get("ready")),
        claimed=_int(queue.get("claimed")),
        dead=_int(queue.get("dead")),
        succeeded_last_hour=_int(queue.get("succeededLastHour")),
        oldest_ready_age_seconds=_opt_int(queue.get("oldestReadyAgeSeconds")),
        lanes=tuple(
            Lane(
                role=str(lane.get("role") or ""),
                attempts=_int(lane.get("attempts")),
                heartbeat_age_seconds=_int(lane.get("heartbeatAgeSeconds")),
            )
            for lane in _list(raw.get("activeLanes"))
        ),
        auth_failures_30m=_int(raw.get("executorAuthFailures30m")),
        backlog_by_status=(
            {str(k): _int(v) for k, v in backlog.items()} if isinstance(backlog, dict) else {}
        ),
        escalations=tuple(
            Escalation(
                task_id=str(item.get("taskID") or ""),
                title=str(item.get("title") or ""),
                priority=_opt_str(item.get("priority")),
                updated_at=_opt_str(item.get("updatedAt")),
            )
            for item in _list(raw.get("ownerEscalations"))
        ),
        monitor=monitor,
    )


def parse_snapshot(payload: object) -> RemoteSnapshot:
    """Map a decoded ``/v1/snapshot`` body to a :class:`RemoteSnapshot`."""
    if not isinstance(payload, dict) or "freshness" not in payload:
        return RemoteSnapshot(FetchOutcome.INVALID_RESPONSE)
    freshness = str(payload.get("freshness"))
    connection = payload.get("connection")
    age = _opt_int(connection.get("projectionAgeSeconds")) if isinstance(connection, dict) else None
    health = parse_pipeline_health(payload.get("pipelineHealth"))
    if freshness in ("degraded", "offline"):
        outcome = FetchOutcome.DEGRADED
    elif health is None:
        outcome = FetchOutcome.NO_HEALTH
    else:
        outcome = FetchOutcome.OK
    return RemoteSnapshot(
        outcome=outcome,
        freshness=freshness,
        generated_at=_opt_str(payload.get("generatedAt")),
        projection_age_seconds=age,
        health=health,
        http_status=200,
    )


# --- transport ----------------------------------------------------------------


def normalize_pin(pin: str | None) -> str | None:
    """Hex SHA-256, case/colon/space-insensitive; ``None`` when empty."""
    if not pin:
        return None
    cleaned = pin.replace(":", "").replace(" ", "").strip().lower()
    return cleaned or None


def https_transport(
    url: str, headers: Mapping[str, str], timeout: float, pin_sha256: str | None
) -> tuple[int, bytes]:
    """Default transport: TLS with system trust, optional leaf-cert pin."""
    parts = urlsplit(url)
    context = ssl.create_default_context()
    conn = http.client.HTTPSConnection(
        parts.hostname or "", parts.port or 443, timeout=timeout, context=context
    )
    try:
        conn.connect()
        expected = normalize_pin(pin_sha256)
        if expected is not None:
            der = conn.sock.getpeercert(binary_form=True) or b""
            if hashlib.sha256(der).hexdigest() != expected:
                raise PinMismatchError("server certificate does not match the pin")
        path = parts.path or "/"
        if parts.query:
            path = f"{path}?{parts.query}"
        conn.request("GET", path, headers=dict(headers))
        response = conn.getresponse()
        return response.status, response.read(MAX_BODY_BYTES + 1)
    finally:
        conn.close()


def _snapshot_url(base_url: str) -> str | None:
    parts = urlsplit(base_url.strip())
    if parts.scheme != "https" or not parts.hostname:
        return None  # the gateway is TLS-only; never send the token in clear
    return f"https://{parts.netloc}{parts.path.rstrip('/')}{SNAPSHOT_PATH}"


def fetch_snapshot(
    base_url: str,
    token: str,
    pin_sha256: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    transport: Transport | None = None,
) -> RemoteSnapshot:
    """Fetch and parse the gateway snapshot; every failure maps to an outcome."""
    url = _snapshot_url(base_url or "")
    if url is None or not (token or "").strip():
        return RemoteSnapshot(FetchOutcome.NOT_CONFIGURED)
    headers = {
        "Accept": "application/json",
        "X-AICC-Client-Version": CLIENT_VERSION,
        "Authorization": f"Bearer {token.strip()}",
    }
    send = transport or https_transport
    try:
        status, body = send(url, headers, timeout, pin_sha256)
    except PinMismatchError:
        return RemoteSnapshot(FetchOutcome.PIN_MISMATCH)
    except (OSError, http.client.HTTPException):
        return RemoteSnapshot(FetchOutcome.UNREACHABLE)
    if status in (401, 403):
        return RemoteSnapshot(FetchOutcome.UNAUTHORIZED, http_status=status)
    if status != 200:
        return RemoteSnapshot(FetchOutcome.HTTP_ERROR, http_status=status)
    if len(body) > MAX_BODY_BYTES:
        return RemoteSnapshot(FetchOutcome.INVALID_RESPONSE, http_status=status)
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return RemoteSnapshot(FetchOutcome.INVALID_RESPONSE, http_status=status)
    return parse_snapshot(payload)


def fetch_with_config(
    config: GatewayConfig | None, *, transport: Transport | None = None
) -> RemoteSnapshot:
    if config is None or not config.is_complete:
        return RemoteSnapshot(FetchOutcome.NOT_CONFIGURED)
    return fetch_snapshot(
        config.base_url, config.token, config.pin_sha256, transport=transport
    )
