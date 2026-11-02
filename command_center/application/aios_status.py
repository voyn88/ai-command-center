"""AICC-owned read-only port for AIOS Core status evidence."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol
from urllib.parse import urlsplit


class AIOSCoreReadiness(str, Enum):
    READY = "ready"
    NOT_READY = "not_ready"
    OFFLINE = "offline"
    ERROR = "error"
    CONTRACT_PENDING = "contract_pending"


class AIOSStatusError(RuntimeError):
    """Safe application error; remote bodies and credentials never cross the port."""

    def __init__(self, code: str, *, request_id: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.request_id = request_id


class AIOSStatusAuthenticationError(AIOSStatusError):
    pass


class AIOSStatusAuthorizationError(AIOSStatusError):
    pass


class AIOSStatusTimeoutError(AIOSStatusError):
    pass


class AIOSStatusTenantError(AIOSStatusError):
    pass


class AIOSStatusContractError(AIOSStatusError):
    pass


class AIOSStatusRemoteError(AIOSStatusError):
    pass


@dataclass(frozen=True)
class AIOSStatusEvidence:
    event: str
    request_id: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {"event": self.event, "request_id": self.request_id}


@dataclass(frozen=True)
class AIOSStatusTimelineEvent:
    event_id: str
    event_type: str
    occurred_at: datetime

    def as_dict(self) -> dict[str, str]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
        }


@dataclass(frozen=True)
class AIOSCoreStatus:
    readiness: AIOSCoreReadiness
    source: str
    version: str | None = None
    health: str | None = None
    tenant_id: str | None = None
    capabilities: tuple[str, ...] = ()
    evidence: tuple[AIOSStatusEvidence, ...] = ()
    timeline: tuple[AIOSStatusTimelineEvent, ...] = ()
    # Neither a healthy process nor an SDK/API response proves acceptance/deployment.
    accepted_sha: str | None = None
    deployed_sha: str | None = None
    detail: str | None = None


class AIOSStatusClient(Protocol):
    def get_core_status(self) -> AIOSCoreStatus: ...

    def close(self) -> None: ...


class DisabledAIOSStatusClient:
    def get_core_status(self) -> AIOSCoreStatus:
        return AIOSCoreStatus(
            readiness=AIOSCoreReadiness.CONTRACT_PENDING,
            source="configuration",
            detail="AIOS SDK status contract is not configured",
        )

    def close(self) -> None:
        return None


def create_aios_status_client(environ: Mapping[str, str] | None = None) -> AIOSStatusClient:
    """Build the SDK adapter only from an explicit, complete HTTPS configuration."""
    values = os.environ if environ is None else environ
    enabled = values.get("AICC_AIOS_STATUS_ENABLED", "").lower() in {"1", "true", "yes"}
    url = values.get("AICC_AIOS_STATUS_URL", "").strip()
    token = values.get("AICC_AIOS_STATUS_TOKEN", "").strip()
    tenant_id = values.get("AICC_AIOS_TENANT_ID", "").strip()
    workspace_id = values.get("AICC_AIOS_WORKSPACE_ID", "").strip()
    allowed_hosts = frozenset(
        host.strip().lower()
        for host in values.get("AICC_AIOS_STATUS_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    )
    parsed = urlsplit(url)
    if (
        not enabled
        or not token
        or not tenant_id
        or not workspace_id
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
        or parsed.hostname.lower() not in allowed_hosts
    ):
        return DisabledAIOSStatusClient()
    from command_center.application.aios_tasks import build_aios_status_client

    return build_aios_status_client(
        url=url,
        token=token,
        tenant_id=tenant_id,
        workspace_id=workspace_id,
    )
