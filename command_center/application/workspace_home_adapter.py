"""Application-layer adapter for the Workspace Home read model (Desktop D2A).

`WorkspaceHomeAdapter` owns the one application-wide :class:`ExecutionCenterAPI`
(`docs/desktop/ARCHITECTURE.md` §3, §5) and exposes the Workspace Home snapshot to
the presentation layer. It is a *thin* wrapper: it returns
:func:`build_workspace_home_snapshot`'s dict unchanged — no field is dropped,
added, or transformed — so every sensitivity-redaction guarantee of the read model
is inherited verbatim. No Qt import lives here.
"""

from __future__ import annotations

from command_center.application.aios_status import (
    AIOSCoreReadiness,
    AIOSCoreStatus,
    AIOSStatusClient,
    AIOSStatusError,
    create_aios_status_client,
)
from command_center.application.provider_capabilities import (
    ProviderCapabilityPort,
    create_provider_capability_client,
)
from command_center.runtime.api import ExecutionCenterAPI
from command_center.workspace_home import build_workspace_home_snapshot


class WorkspaceHomeAdapter:
    """Adapter owning the single ``ExecutionCenterAPI`` for Workspace Home reads.

    Pass an explicit ``execution_center_api`` in tests to point it at an isolated
    runtime database; in the running application it is constructed once and shared.
    """

    def __init__(
        self,
        *,
        execution_center_api: ExecutionCenterAPI | None = None,
        aios_status_client: AIOSStatusClient | None = None,
        provider_capability_client: ProviderCapabilityPort | None = None,
    ) -> None:
        self._api = execution_center_api if execution_center_api is not None else ExecutionCenterAPI()
        self._aios_status_client = aios_status_client or create_aios_status_client()
        self._provider_capability_client = (
            provider_capability_client or create_provider_capability_client()
        )

    @property
    def execution_center_api(self) -> ExecutionCenterAPI:
        """The one ``ExecutionCenterAPI`` this adapter reads through."""
        return self._api

    def snapshot(
        self,
        *,
        active_runs_limit: int = 20,
        recent_runs_limit: int = 20,
        activity_limit: int = 20,
        artifacts_limit: int = 20,
        reports_limit: int = 20,
    ) -> dict:
        """Return the local read model plus a separately sourced AIOS status."""
        snapshot = build_workspace_home_snapshot(
            execution_center_api=self._api,
            active_runs_limit=active_runs_limit,
            recent_runs_limit=recent_runs_limit,
            activity_limit=activity_limit,
            artifacts_limit=artifacts_limit,
            reports_limit=reports_limit,
        )
        return snapshot

    def aios_core_status(self) -> dict:
        """Fetch AIOS independently so remote latency cannot block local Home."""
        try:
            status = self._aios_status_client.get_core_status()
        except AIOSStatusError as error:
            status = AIOSCoreStatus(
                readiness=AIOSCoreReadiness.ERROR,
                source="AIOS SDK",
                detail=error.code,
                evidence=(),
                accepted_sha=None,
                deployed_sha=None,
            )
        return {
            "readiness": status.readiness.value,
            "source": status.source,
            "version": status.version,
            "health": status.health,
            "tenant_id": status.tenant_id,
            "capabilities": list(status.capabilities),
            "evidence": [item.as_dict() for item in status.evidence],
            "timeline": [item.as_dict() for item in status.timeline],
            "accepted_sha": status.accepted_sha,
            "deployed_sha": status.deployed_sha,
            "detail": status.detail,
        }

    def provider_capabilities(self) -> list[dict]:
        return [
            {
                "provider_id": item.provider_id,
                "display_name": item.display_name,
                "readiness": item.readiness.value,
                "provenance": item.provenance,
                "detail": item.detail,
            }
            for item in self._provider_capability_client.list_capabilities()
        ]
