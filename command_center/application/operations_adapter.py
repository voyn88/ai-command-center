"""Read/action adapter for native operational desktop sections."""

from __future__ import annotations

from collections.abc import Callable

from command_center import project_config
from command_center.application.server_queue import (
    ServerQueueClient,
    create_server_queue_client,
)
from command_center.platform.preferences import DataSourceMode
from command_center.runtime import db as runtime_db
from command_center.runtime import scheduler
from command_center.runtime.api import ExecutionCenterAPI

from .workspace_home_adapter import WorkspaceHomeAdapter


class OperationsAdapter:
    """Expose existing runtime read models without putting domain logic in Qt."""

    def __init__(
        self,
        *,
        execution_center_api: ExecutionCenterAPI | None = None,
        workspace_home_adapter: WorkspaceHomeAdapter | None = None,
        data_source_mode: Callable[[], DataSourceMode] | None = None,
        server_queue_client: ServerQueueClient | None = None,
    ) -> None:
        self._workspace = workspace_home_adapter or WorkspaceHomeAdapter(
            execution_center_api=execution_center_api
        )
        self._api = self._workspace.execution_center_api
        # Resolved lazily (a callable, not a captured value) so a Settings
        # toggle flipped mid-session takes effect on the very next read
        # without reconstructing this adapter (VOYN-W0-APP-CONTROL-S2).
        self._data_source_mode = data_source_mode or (lambda: DataSourceMode.LOCAL)
        self._server_queue_client = server_queue_client or create_server_queue_client()

    def sessions(self) -> list[dict]:
        runs = self._api.list_runs(limit=500)
        latest_by_session: dict[str, dict] = {}
        for run in runs:
            session_id = run.get("session_id")
            if session_id and session_id not in latest_by_session:
                latest_by_session[session_id] = run
        rows = []
        for item in self._api.list_sessions()[:200]:
            latest = latest_by_session.get(item["id"], {})
            rows.append(
                {
                    "id": item["id"],
                    "project": item.get("project"),
                    "state": latest.get("state") or "UNKNOWN",
                    "created_at": item.get("created_at"),
                    "updated_at": item.get("updated_at"),
                }
            )
        return rows

    def execution(self) -> list[dict]:
        if self._data_source_mode() is DataSourceMode.SERVER:
            return self._server_execution_rows()
        snapshot = self._workspace.snapshot(
            active_runs_limit=100,
            recent_runs_limit=100,
            activity_limit=0,
            artifacts_limit=0,
            reports_limit=0,
        )
        return [*snapshot.get("active_runs", []), *snapshot.get("recent_runs", [])]

    def _server_execution_rows(self) -> list[dict]:
        """The "server" toggle's read (VOYN-W0-APP-CONTROL-S2): the same
        five-column shape the local branch returns (``project``, ``state``,
        ``task_type``, ``run_id``, ``created_at`` — see
        ``main_window.py``'s ``operational_columns["execution"]``), populated
        from the preprod work queue instead of the local runtime. A queue
        item carries no ``project``/``task_type`` of its own (those live in
        the work item's payload, not the list view), so the queue name
        stands in for ``task_type`` and the repository id (falling back to
        the task id) stands in for ``project`` — an honest, if coarser,
        identification of "what this row is about" rather than a fabricated
        field. Raises :class:`ServerQueueError` on failure/misconfiguration;
        the caller (``OperationalPage``) already renders that as its generic
        load-error state, same as any other operational read failing.
        """
        items = self._server_queue_client.list_items(limit=200)
        return [
            {
                "project": item.repository_id or item.task_id,
                "state": item.state,
                "task_type": item.queue,
                "run_id": item.work_item_id,
                "created_at": item.created_at,
            }
            for item in items
        ]

    def git(self) -> list[dict]:
        snapshot = self._workspace.snapshot(
            active_runs_limit=0,
            recent_runs_limit=0,
            activity_limit=0,
            artifacts_limit=0,
            reports_limit=0,
        )
        worktrees = snapshot.get("worktrees_by_project", {})
        rows = []
        for project in snapshot.get("projects", []):
            project_id = project["id"]
            sensitive = project_config.is_sensitive(project_id)
            discovered = worktrees.get(project_id, {}).get("worktrees", [])
            rows.append(
                {
                    "project": project_id,
                    "state": project.get("repository_state"),
                    "branch": None if sensitive else (discovered[0].get("branch") if discovered else None),
                    "path": None if sensitive else project.get("repository_path"),
                    "worktrees": len(discovered),
                }
            )
        return rows

    def artifacts(self) -> list[dict]:
        return self._workspace.snapshot(
            active_runs_limit=0,
            recent_runs_limit=0,
            activity_limit=0,
            artifacts_limit=200,
            reports_limit=0,
        ).get("artifacts", [])

    def reports(self) -> list[dict]:
        return self._workspace.snapshot(
            active_runs_limit=0,
            recent_runs_limit=0,
            activity_limit=0,
            artifacts_limit=0,
            reports_limit=200,
        ).get("reports", [])

    def agents(self) -> list[dict]:
        load = scheduler.build_load_snapshot(self._api.db_path)
        return [
            {
                **provider,
                "running": load.running_by_agent.get(provider["provider_id"], 0),
            }
            for provider in self._workspace.provider_capabilities()
        ]

    def cancel_run(self, run_id: str, *, confirmed: bool) -> dict:
        return self._api.request_cancel(run_id, confirmed=confirmed)

    @staticmethod
    def is_active_state(state: object) -> bool:
        return state in runtime_db.EXECUTION_CENTER_ACTIVE_STATES
