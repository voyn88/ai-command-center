from types import SimpleNamespace

from command_center.application.operations_adapter import OperationsAdapter
from command_center.application.server_queue import ServerQueueItem
from command_center.platform.preferences import DataSourceMode


class FakeAPI:
    db_path = "runtime.db"

    def list_runs(self, **_kwargs):
        return [{"session_id": "s1", "state": "RUNNING"}]

    def list_sessions(self):
        return [{"id": "s1", "project": "AIOS", "created_at": "t1", "updated_at": "t2", "repository_path": "/secret"}]


class FakeWorkspace:
    execution_center_api = FakeAPI()

    def snapshot(self, **kwargs):
        if kwargs.get("artifacts_limit"):
            return {"artifacts": [{"project": "AIOS", "path": "/artifact"}]}
        if kwargs.get("reports_limit"):
            return {"reports": [{"project": "AIOS", "path": "/report"}]}
        return {
            "active_runs": [{"project": "AIOS", "state": "RUNNING"}],
            "recent_runs": [{"project": "AIOS", "state": "COMPLETED"}],
            "projects": [
                {"id": "AIOS", "repository_state": "ok", "repository_path": "/aios"},
                {"id": "BANK", "repository_state": "ok", "repository_path": "/bank"},
            ],
            "worktrees_by_project": {
                "AIOS": {"worktrees": [{"branch": "main"}]},
                "BANK": {"worktrees": [{"branch": "classified"}]},
            },
        }

    def provider_capabilities(self):
        return [{"provider_id": "codex", "display_name": "Codex", "readiness": "available", "detail": ""}]


def test_sessions_expose_operational_fields_only():
    rows = OperationsAdapter(workspace_home_adapter=FakeWorkspace()).sessions()
    assert rows == [{"id": "s1", "project": "AIOS", "state": "RUNNING", "created_at": "t1", "updated_at": "t2"}]


def test_git_redacts_sensitive_paths_and_branches():
    rows = OperationsAdapter(workspace_home_adapter=FakeWorkspace()).git()
    by_project = {row["project"]: row for row in rows}
    assert by_project["AIOS"]["path"] == "/aios"
    assert by_project["AIOS"]["branch"] == "main"
    assert by_project["BANK"]["path"] is None
    assert by_project["BANK"]["branch"] is None


def test_agents_include_live_load(monkeypatch):
    monkeypatch.setattr(
        "command_center.application.operations_adapter.scheduler.build_load_snapshot",
        lambda _path: SimpleNamespace(running_by_agent={"codex": 2}),
    )
    rows = OperationsAdapter(workspace_home_adapter=FakeWorkspace()).agents()
    assert rows[0]["running"] == 2


def test_execution_reads_local_runtime_by_default():
    rows = OperationsAdapter(workspace_home_adapter=FakeWorkspace()).execution()
    assert rows == [{"project": "AIOS", "state": "RUNNING"}, {"project": "AIOS", "state": "COMPLETED"}]


class FakeServerQueueClient:
    def __init__(self, items):
        self._items = items

    def list_items(self, **_kwargs):
        return self._items

    def close(self):
        return None


def test_execution_reads_the_server_queue_when_toggled_to_server():
    item = ServerQueueItem(
        work_item_id="wi-1",
        queue="execution",
        state="ready",
        task_id="T-1",
        repository_id="repo-1",
        priority=1,
        attempt_count=0,
        max_attempts=3,
        created_at="2026-01-01T00:00:00Z",
        updated_at=None,
    )
    adapter = OperationsAdapter(
        workspace_home_adapter=FakeWorkspace(),
        data_source_mode=lambda: DataSourceMode.SERVER,
        server_queue_client=FakeServerQueueClient([item]),
    )
    rows = adapter.execution()
    assert rows == [
        {
            "project": "repo-1",
            "state": "ready",
            "task_type": "execution",
            "run_id": "wi-1",
            "created_at": "2026-01-01T00:00:00Z",
        }
    ]


def test_execution_server_row_falls_back_to_task_id_without_a_repository():
    item = ServerQueueItem(
        work_item_id="wi-2",
        queue="execution",
        state="claimed",
        task_id="T-2",
        repository_id=None,
        priority=1,
        attempt_count=1,
        max_attempts=3,
        created_at=None,
        updated_at=None,
    )
    adapter = OperationsAdapter(
        workspace_home_adapter=FakeWorkspace(),
        data_source_mode=lambda: DataSourceMode.SERVER,
        server_queue_client=FakeServerQueueClient([item]),
    )
    assert adapter.execution()[0]["project"] == "T-2"


def test_toggling_mode_takes_effect_on_the_next_read_without_reconstruction():
    mode = {"value": DataSourceMode.LOCAL}
    item = ServerQueueItem(
        work_item_id="wi-3",
        queue="execution",
        state="ready",
        task_id=None,
        repository_id=None,
        priority=None,
        attempt_count=None,
        max_attempts=None,
        created_at=None,
        updated_at=None,
    )
    adapter = OperationsAdapter(
        workspace_home_adapter=FakeWorkspace(),
        data_source_mode=lambda: mode["value"],
        server_queue_client=FakeServerQueueClient([item]),
    )
    assert adapter.execution() == [{"project": "AIOS", "state": "RUNNING"}, {"project": "AIOS", "state": "COMPLETED"}]
    mode["value"] = DataSourceMode.SERVER
    assert adapter.execution()[0]["run_id"] == "wi-3"
