from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from command_center.application import tasks_gateway
from command_center.application.aios_tasks import (
    AIOSIdMap,
    AIOSSDKTasksGateway,
    AIOSTasksRepository,
    aicc_dict_to_create_request,
    aios_task_to_aicc_dict,
    task_idempotency_key,
)


def _task_dict(**overrides) -> dict:
    task = {
        "id": "aicc-1",
        "project": "AICC",
        "title": "Boundary task",
        "task_type": "implementation",
        "status": "Backlog",
        "priority": "High",
        "owner": "operator",
        "estimate_hours": 2.0,
        "depends_on": ["aicc-0"],
        "goal": "Exercise the public contract",
        "workflow_stage": "Ready",
        "prompt": "secret prompt",
        "notes": "private notes",
        "repository_path": "/private/repository",
        "workspace_path": "/private/worktree",
        "report_path": "/private/report.md",
        "executor": "private-provider",
        "agent": "private-agent",
        "branch": "private-branch",
    }
    task.update(overrides)
    return task


def _gateway_task(*, state: str = "open", payload: dict | None = None) -> tasks_gateway.TaskDTO:
    return tasks_gateway.TaskDTO(
        id="aios-1",
        subject_ref="AICC/aicc-1",
        type="implementation",
        title="Boundary task",
        state=state,
        priority=3,
        payload=payload or {"aicc_id": "aicc-1", "project": "AICC"},
        created_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
        updated_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )


class FakeGateway:
    def __init__(self, tasks: list[tasks_gateway.TaskDTO] | None = None) -> None:
        self.tasks = list(tasks or [])
        self.create_keys: list[str] = []
        self.created_by_key: dict[str, tasks_gateway.TaskDTO] = {}
        self.transitions: list[tuple[str, str]] = []

    def list_tasks(self) -> tasks_gateway.TaskListResult:
        return tasks_gateway.TaskListResult(
            tuple(self.tasks),
            (tasks_gateway.GatewayEvidence("task.list", "req-list"),),
        )

    def create_task(
        self, request: tasks_gateway.CreateTaskDTO, *, idempotency_key: str
    ) -> tasks_gateway.TaskResult:
        self.create_keys.append(idempotency_key)
        task = self.created_by_key.get(idempotency_key)
        if task is None:
            task = _gateway_task(payload=request.payload)
            self.created_by_key[idempotency_key] = task
            self.tasks.append(task)
        return tasks_gateway.TaskResult(
            task,
            (tasks_gateway.GatewayEvidence("task.create", "req-create"),),
        )

    def get_task(self, task_id: str) -> tasks_gateway.TaskResult:
        task = next(task for task in self.tasks if task.id == task_id)
        return tasks_gateway.TaskResult(
            task,
            (tasks_gateway.GatewayEvidence("task.get", "req-get"),),
        )

    def assign_task(self, task_id: str, assignee: str) -> tasks_gateway.TaskResult:
        assert assignee
        return self._transition(task_id, "assign", "assigned", "req-assign")

    def start_task(self, task_id: str) -> tasks_gateway.TaskResult:
        return self._transition(task_id, "start", "in_progress", "req-start")

    def complete_task(self, task_id: str) -> tasks_gateway.TaskResult:
        return self._transition(task_id, "complete", "completed", "req-complete")

    def cancel_task(self, task_id: str) -> tasks_gateway.TaskResult:
        return self._transition(task_id, "cancel", "cancelled", "req-cancel")

    def _transition(
        self, task_id: str, event: str, state: str, request_id: str
    ) -> tasks_gateway.TaskResult:
        current = next(task for task in self.tasks if task.id == task_id)
        updated = replace(current, state=state)
        self.tasks[self.tasks.index(current)] = updated
        self.transitions.append((task_id, event))
        return tasks_gateway.TaskResult(
            updated,
            (tasks_gateway.GatewayEvidence(f"task.{event}", request_id),),
        )


def _concurrent_map_put(
    path: str,
    aicc_id: str,
    aios_id: str,
    ready: multiprocessing.Queue,
    start: multiprocessing.Event,
) -> None:
    id_map = AIOSIdMap(Path(path))
    ready.put(True)
    if not start.wait(timeout=10):
        raise RuntimeError("concurrent map test start timed out")
    id_map.put(aicc_id, aios_id)


def test_gateway_contract_imports_without_aios_sdk_and_is_runtime_checkable():
    assert tasks_gateway.TasksGateway is not None
    assert tasks_gateway.TaskDTO.__module__ == "command_center.application.tasks_gateway"

    assert isinstance(FakeGateway(), tasks_gateway.TasksGateway)

    class _MissingCreateTask:
        def list_tasks(self):
            ...

        def get_task(self, task_id):
            ...

        def assign_task(self, task_id, assignee):
            ...

        def start_task(self, task_id):
            ...

        def complete_task(self, task_id):
            ...

        def cancel_task(self, task_id):
            ...

    assert not isinstance(_MissingCreateTask(), tasks_gateway.TasksGateway), (
        "runtime_checkable must reject an object missing part of the contract, "
        "or isinstance() checks against TasksGateway are decorative"
    )

    probe = (
        "import sys\n"
        "from command_center.application import tasks_gateway\n"
        "leaked = sorted(\n"
        "    name for name in sys.modules\n"
        "    if name == 'aios_sdk' or name.startswith('aios_sdk.')\n"
        "    or name == 'command_center.application.aios_tasks'\n"
        ")\n"
        "sys.stdout.write(','.join(leaked))\n"
    )
    source_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=source_root,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(source_root)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "", (
        "importing tasks_gateway alone pulled in the AIOS SDK adapter: "
        f"{completed.stdout.strip()}"
    )


def test_create_request_is_minimal_and_idempotency_key_is_stable():
    request, target = aicc_dict_to_create_request(_task_dict())
    assert target == "open"
    assert request.payload == {
        "aicc_id": "aicc-1",
        "project": "AICC",
        "workflow_stage": "Ready",
        "owner": "operator",
        "estimate_hours": 2.0,
        "depends_on": ["aicc-0"],
        "goal": "Exercise the public contract",
    }
    rendered = json.dumps(request.payload)
    for forbidden in (
        "secret prompt",
        "private notes",
        "/private/repository",
        "/private/worktree",
        "/private/report.md",
        "private-provider",
        "private-agent",
        "private-branch",
    ):
        assert forbidden not in rendered
    assert task_idempotency_key(_task_dict()) == task_idempotency_key(
        _task_dict(updated_at="later", prompt="different secret")
    )
    assert "aicc-1" not in task_idempotency_key(_task_dict())


@pytest.mark.parametrize(
    ("state", "lane"),
    [
        ("open", "Backlog"),
        ("assigned", "Next"),
        ("in_progress", "In Progress"),
        ("completed", "Done"),
    ],
)
def test_remote_state_is_authoritative_over_stale_payload_lane(state: str, lane: str):
    task = _gateway_task(state=state, payload={"aicc_id": "aicc-1", "kanban_status": "Done"})
    assert aios_task_to_aicc_dict(task)["status"] == lane


def test_unsupported_remote_state_fails_closed():
    with pytest.raises(tasks_gateway.UnsupportedTaskStateError, match="cancelled"):
        aios_task_to_aicc_dict(_gateway_task(state="cancelled"))


def test_corrupt_mapping_file_fails_closed(tmp_path):
    path = tmp_path / "aios_task_map.json"
    path.write_text('{"aicc-1": 42}', encoding="utf-8")
    with pytest.raises(tasks_gateway.CorruptTaskMapError):
        AIOSIdMap(path)


def test_separate_map_instances_preserve_each_others_updates(tmp_path):
    path = tmp_path / "aios_task_map.json"
    first = AIOSIdMap(path)
    second = AIOSIdMap(path)
    first.put("aicc-1", "aios-1")
    second.put("aicc-2", "aios-2")
    assert AIOSIdMap(path).get("aicc-1") == "aios-1"
    assert AIOSIdMap(path).get("aicc-2") == "aios-2"


def test_cross_process_mapping_updates_do_not_overwrite_each_other(tmp_path):
    path = tmp_path / "aios_task_map.json"
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    start = context.Event()
    processes = [
        context.Process(
            target=_concurrent_map_put,
            args=(str(path), f"aicc-{index}", f"aios-{index}", ready, start),
        )
        for index in (1, 2)
    ]
    for process in processes:
        process.start()
    assert ready.get(timeout=10) is True
    assert ready.get(timeout=10) is True
    start.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "aicc-1": "aios-1",
        "aicc-2": "aios-2",
    }


def test_create_success_then_map_write_crash_reconciles_without_duplicate(tmp_path, monkeypatch):
    gateway = FakeGateway()
    first_map = AIOSIdMap(tmp_path / "map.json")
    repo = AIOSTasksRepository(gateway, first_map)

    def crash_after_remote_success(aicc_id: str, aios_id: str) -> None:
        raise tasks_gateway.TaskMapWriteError("simulated crash window")

    monkeypatch.setattr(first_map, "put", crash_after_remote_success)
    with pytest.raises(tasks_gateway.TaskMapWriteError):
        repo.create(_task_dict())
    assert len(gateway.tasks) == 1

    recovered_map = AIOSIdMap(tmp_path / "map.json")
    recovered = AIOSTasksRepository(gateway, recovered_map).create(_task_dict())
    assert recovered["aios_id"] == "aios-1"
    assert recovered_map.get("aicc-1") == "aios-1"
    assert len(gateway.tasks) == 1
    assert len(gateway.create_keys) == 2
    assert len(set(gateway.create_keys)) == 1


def test_create_and_transition_preserve_only_safe_request_evidence(tmp_path):
    gateway = FakeGateway()
    repo = AIOSTasksRepository(gateway, AIOSIdMap(tmp_path / "map.json"))
    created = repo.create(_task_dict(status="Done"))
    assert created["status"] == "Done"
    assert created["aios_evidence"] == [
        {"event": "task.create", "request_id": "req-create"},
        {"event": "task.start", "request_id": "req-start"},
        {"event": "task.complete", "request_id": "req-complete"},
    ]
    assert "secret" not in repr(created["aios_evidence"])


def test_reverse_and_unrepresentable_transitions_are_explicit(tmp_path):
    gateway = FakeGateway([_gateway_task(state="completed")])
    id_map = AIOSIdMap(tmp_path / "map.json")
    id_map.put("aicc-1", "aios-1")
    repo = AIOSTasksRepository(gateway, id_map)
    with pytest.raises(tasks_gateway.UnsupportedTaskTransitionError):
        repo.update_status("aicc-1", "In Progress")
    with pytest.raises(tasks_gateway.UnsupportedTaskTransitionError, match="Blocked"):
        repo.update_status("aicc-1", "Blocked")


def test_delete_tombstone_does_not_poison_load_all(tmp_path):
    gateway = FakeGateway([_gateway_task()])
    id_map = AIOSIdMap(tmp_path / "map.json")
    id_map.put("aicc-1", "aios-1")
    repo = AIOSTasksRepository(gateway, id_map)
    assert repo.delete("aicc-1") is True
    assert gateway.tasks[0].state == "cancelled"
    assert repo.load_all() == []


def test_repository_explicitly_closes_owned_sdk_client(tmp_path):
    client = SimpleNamespace(tasks=SimpleNamespace(), close=lambda: setattr(client, "closed", True))
    client.closed = False
    sdk = SimpleNamespace(AIOSSDKError=RuntimeError)
    repo = AIOSTasksRepository(AIOSSDKTasksGateway(client, sdk), AIOSIdMap(tmp_path / "map.json"))
    repo.close()
    assert client.closed is True


def test_sdk_error_is_translated_to_safe_product_error_with_request_evidence():
    class SDKError(RuntimeError):
        code = "temporarily_unavailable"
        request_id = "req-error"
        retryable = True

    class Tasks:
        @staticmethod
        def get(task_id: str):
            assert task_id == "aios-1"
            raise SDKError("secret upstream response")

    gateway = AIOSSDKTasksGateway(
        SimpleNamespace(tasks=Tasks()),
        SimpleNamespace(AIOSSDKError=SDKError),
    )
    with pytest.raises(tasks_gateway.TasksGatewayRemoteError) as caught:
        gateway.get_task("aios-1")
    assert caught.value.code == "temporarily_unavailable"
    assert caught.value.request_id == "req-error"
    assert caught.value.retryable is True
    assert "secret upstream response" not in str(caught.value)
