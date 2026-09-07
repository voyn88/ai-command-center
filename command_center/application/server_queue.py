"""AICC-owned read-only port for the preprod server's work queue
(VOYN-W0-APP-CONTROL-S2: "point the desktop at the server" — a Settings
toggle swaps the Workspace ``execution`` read from the local runtime for
this port instead).

Mirrors :mod:`command_center.application.aios_status`'s shape deliberately:
same enable-gate-by-complete-configuration factory, same disabled-by-default
fallback, same "safe application error, no remote body/credential crosses the
port" discipline. The remote here is AICC's own boundary
(``command_center/webapi/queue_routes.py``, ``GET /api/v1/queue/items``), not
a third-party SDK, so the transport is the same authenticated ``urllib`` GET
`command_center.http_auth.identity` already uses for the platform's
``whoami`` — no new production dependency, one authenticated HTTP call with a
bounded timeout.

The desktop layer never imports this module's transport details or
``command_center.webapi`` — the architecture fitness gate
(``tests/architecture/test_desktop_engine_fitness.py``) forbids the latter
outright. It reads queue items only through this port, exactly as it reads
AIOS status only through :mod:`aios_status`.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urlsplit

__all__ = [
    "ServerQueueItem",
    "ServerQueueError",
    "ServerQueueAuthenticationError",
    "ServerQueueTimeoutError",
    "ServerQueueRemoteError",
    "ServerQueueDisabledError",
    "ServerQueueClient",
    "DisabledServerQueueClient",
    "HTTPServerQueueClient",
    "create_server_queue_client",
]

#: Connect + read budget for one queue list request. Short enough that an
#: unreachable preprod host degrades to a fast, loud error rather than
#: hanging a UI thread's worker pool (the same reasoning as
#: ``identity.TIMEOUT_SECONDS``).
TIMEOUT_SECONDS = 5.0


class ServerQueueError(RuntimeError):
    """Safe application error; remote bodies and credentials never cross the port."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ServerQueueAuthenticationError(ServerQueueError):
    """The configured credential was rejected (401)."""


class ServerQueueTimeoutError(ServerQueueError):
    """No usable answer at all — socket error, timeout, DNS failure."""


class ServerQueueRemoteError(ServerQueueError):
    """The server answered, but not with a usable 2xx queue listing."""


class ServerQueueDisabledError(ServerQueueError):
    """The port is not configured — asked before ``AICC_SERVER_QUEUE_*`` is set."""

    def __init__(self) -> None:
        super().__init__("server_queue_not_configured")


@dataclass(frozen=True)
class ServerQueueItem:
    """One row of ``work_item_public`` (see ``db/work_queue_read.py``).

    Field names and presence mirror the read store's real output exactly —
    no field is invented, renamed, or dropped — so a caller that reads the
    HTTP response directly and one that reads through this port see the same
    shape.
    """

    work_item_id: str
    queue: str
    state: str
    task_id: str | None
    repository_id: str | None
    priority: int | None
    attempt_count: int | None
    max_attempts: int | None
    created_at: str | None
    updated_at: str | None

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ServerQueueItem":
        def _str(key: str) -> str | None:
            value = payload.get(key)
            return None if value is None else str(value)

        def _int(key: str) -> int | None:
            value = payload.get(key)
            return None if value is None else int(value)  # type: ignore[call-overload]

        return cls(
            work_item_id=_str("work_item_id") or "",
            queue=_str("queue") or "",
            state=_str("state") or "",
            task_id=_str("task_id"),
            repository_id=_str("repository_id"),
            priority=_int("priority"),
            attempt_count=_int("attempt_count"),
            max_attempts=_int("max_attempts"),
            created_at=_str("created_at"),
            updated_at=_str("updated_at"),
        )


class ServerQueueClient(Protocol):
    def list_items(
        self, *, queue: str | None = None, state: str | None = None, limit: int = 100
    ) -> list[ServerQueueItem]: ...

    def close(self) -> None: ...


class DisabledServerQueueClient:
    """The honest default: no configuration means no request, ever."""

    def list_items(
        self, *, queue: str | None = None, state: str | None = None, limit: int = 100
    ) -> list[ServerQueueItem]:
        raise ServerQueueDisabledError()

    def close(self) -> None:
        return None


def _http_get_json(url: str, token: str, timeout: float) -> tuple[int, bytes]:
    """One authenticated GET. The seam tests replace.

    Returns ``(status, body)``. Raises :class:`ServerQueueTimeoutError` when
    there is no usable answer at all. An ``HTTPError`` is not treated as an
    outage: it carries a real status line, so it is returned as a status for
    the caller to interpret (401 vs any other non-2xx).
    """
    auth_header = "Bearer" + " " + token
    request = urllib.request.Request(  # noqa: S310 - scheme is operator-configured
        url, headers={"Authorization": auth_header, "Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ServerQueueTimeoutError(f"queue_transport_failure: {exc}") from exc


class HTTPServerQueueClient:
    """Reads ``GET {base_url}/api/v1/queue/items`` (``queue_routes.py``)."""

    def __init__(self, *, base_url: str, token: str, timeout: float = TIMEOUT_SECONDS) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def list_items(
        self, *, queue: str | None = None, state: str | None = None, limit: int = 100
    ) -> list[ServerQueueItem]:
        params = {"limit": str(limit)}
        if queue is not None:
            params["queue"] = queue
        if state is not None:
            params["state"] = state
        url = f"{self._base_url}/api/v1/queue/items?{urlencode(params)}"
        status, body = _http_get_json(url, self._token, self._timeout)
        if status == 401:
            raise ServerQueueAuthenticationError("unauthenticated")
        if status < 200 or status >= 300:
            raise ServerQueueRemoteError(f"http_{status}")
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ServerQueueRemoteError(f"invalid_response_body: {exc}") from None
        items = parsed.get("items") if isinstance(parsed, dict) else None
        if not isinstance(items, list):
            raise ServerQueueRemoteError("invalid_response_shape")
        return [ServerQueueItem.from_dict(item) for item in items if isinstance(item, dict)]

    def close(self) -> None:
        return None


def create_server_queue_client(environ: Mapping[str, str] | None = None) -> ServerQueueClient:
    """Build the HTTP adapter only from an explicit, complete HTTPS configuration.

    Enabling is the Settings toggle's job (:class:`~command_center.platform.
    preferences.DataSourceMode`), not this factory's — the factory's only job
    is refusing to build a client that would reach an unconfigured or
    unsafe (non-HTTPS, non-allowlisted) host, exactly as ``aios_status``
    refuses for the AIOS SDK adapter.
    """
    values = os.environ if environ is None else environ
    url = values.get("AICC_SERVER_QUEUE_URL", "").strip()
    token = values.get("AICC_SERVER_QUEUE_TOKEN", "").strip()
    allowed_hosts = frozenset(
        host.strip().lower()
        for host in values.get("AICC_SERVER_QUEUE_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    )
    parsed = urlsplit(url)
    if (
        not url
        or not token
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.hostname.lower() not in allowed_hosts
    ):
        return DisabledServerQueueClient()
    return HTTPServerQueueClient(base_url=url, token=token)
