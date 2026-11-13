"""Tests for AIOSIdMap and AIOSTasksRepository (Task 2 of AICC Sprint 4)."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import httpx

from command_center.application import aios_tasks  # noqa: E402
from command_center.application.aios_tasks import AIOSIdMap, AIOSTasksRepository  # noqa: E402

AIOSClient = aios_tasks._load_aios_sdk().AIOSClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _task_payload(
    task_id: str = "aios-t-1",
    *,
    aicc_id: str = "aicc-abc123",
    state: str = "open",
    title: str = "Test task",
    kanban_status: str = "Backlog",
) -> dict[str, Any]:
    return {
        "data": {
            "id": task_id,
            "state": state,
            "title": title,
            "type": "task",
            "subject_ref": f"AICC/{aicc_id}",
            "assignee": None,
            "priority": 2,
            "escalated": False,
            "escalation_target": None,
            "due_at": None,
            "payload": {"aicc_id": aicc_id, "kanban_status": kanban_status, "project": "AICC"},
            "created_by": "aicc",
            "created_at": "2026-08-06T10:00:00Z",
            "updated_at": "2026-08-06T10:00:00Z",
        },
        "meta": {"request_id": "req-1"},
    }


def _list_payload(tasks: list[dict]) -> dict[str, Any]:
    return {
        "data": tasks,
        "page": {"next_cursor": None, "has_more": False},
        "meta": {"request_id": "req-1"},
    }


def _json_response(status: int, body: dict) -> httpx.Response:
    return httpx.Response(status_code=status, json=body, headers={"X-Request-Id": "req-1"})


def _make_repo(handler, id_map=None) -> tuple[AIOSTasksRepository, Path]:
    tmp = Path(tempfile.mkdtemp())
    client = AIOSClient("https://test.example", token="tok", transport=httpx.MockTransport(handler))
    if id_map is None:
        id_map = AIOSIdMap(tmp / "aios_task_map.json")
    repo = AIOSTasksRepository(client, id_map)
    return repo, tmp


# ---------------------------------------------------------------------------
# AIOSIdMap
# ---------------------------------------------------------------------------

def test_idmap_empty_on_new_file():
    with tempfile.TemporaryDirectory() as d:
        m = AIOSIdMap(Path(d) / "map.json")
        assert m.get("nonexistent") is None


def test_idmap_put_and_get():
    with tempfile.TemporaryDirectory() as d:
        m = AIOSIdMap(Path(d) / "map.json")
        m.put("aicc-1", "aios-999")
        assert m.get("aicc-1") == "aios-999"


def test_idmap_persists_across_instances():
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "map.json"
        m1 = AIOSIdMap(path)
        m1.put("aicc-1", "aios-999")
        m2 = AIOSIdMap(path)
        assert m2.get("aicc-1") == "aios-999"


def test_idmap_remove():
    with tempfile.TemporaryDirectory() as d:
        m = AIOSIdMap(Path(d) / "map.json")
        m.put("aicc-1", "aios-999")
        m.remove("aicc-1")
        assert m.get("aicc-1") is None


# ---------------------------------------------------------------------------
# AIOSTasksRepository.load_all
# ---------------------------------------------------------------------------

def test_load_all_returns_aicc_dicts():
    def handler(req: httpx.Request) -> httpx.Response:
        raw_task = _task_payload("aios-t-1", aicc_id="abc123")["data"]
        return _json_response(200, _list_payload([raw_task]))

    repo, _ = _make_repo(handler)
    tasks = repo.load_all()
    assert len(tasks) == 1
    assert tasks[0]["id"] == "abc123"
    assert tasks[0]["status"] == "Backlog"


def test_load_all_empty():
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(200, _list_payload([]))

    repo, _ = _make_repo(handler)
    assert repo.load_all() == []


# ---------------------------------------------------------------------------
# AIOSTasksRepository.create
# ---------------------------------------------------------------------------

def test_create_calls_post_and_returns_aicc_dict():
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        return _json_response(201, _task_payload("aios-new", aicc_id="abc123"))

    repo, tmp = _make_repo(handler)
    task_dict = {
        "id": "abc123",
        "project": "AICC",
        "title": "New task",
        "task_type": "implementation",
        "status": "Backlog",
        "priority": "Medium",
        "owner": "",
        "depends_on": [],
        "created_at": "2026-08-06T10:00:00Z",
        "updated_at": "2026-08-06T10:00:00Z",
    }
    result = repo.create(task_dict)
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/api/v1/tasks"
    assert result["id"] == "abc123"
    assert result["aios_id"] == "aios-new"


def test_create_records_id_mapping():
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(201, _task_payload("aios-new", aicc_id="abc123"))

    repo, tmp = _make_repo(handler)
    task_dict = {
        "id": "abc123", "project": "AICC", "title": "T", "task_type": "task",
        "status": "Backlog", "priority": "Medium", "owner": "", "depends_on": [],
        "created_at": "2026-08-06T10:00:00Z", "updated_at": "2026-08-06T10:00:00Z",
    }
    repo.create(task_dict)
    # Map file must now contain aicc->aios mapping
    map_data = json.loads((tmp / "aios_task_map.json").read_text())
    assert map_data["abc123"] == "aios-new"


def test_create_in_progress_task_calls_start():
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "POST" and req.url.path == "/api/v1/tasks":
            return _json_response(201, _task_payload("aios-t1", aicc_id="aicc-1", state="open"))
        # start call
        return _json_response(200, _task_payload("aios-t1", aicc_id="aicc-1", state="in_progress"))

    repo, _ = _make_repo(handler)
    task_dict = {
        "id": "aicc-1", "project": "AICC", "title": "WIP task", "task_type": "task",
        "status": "In Progress", "priority": "Medium", "owner": "", "depends_on": [],
        "created_at": "2026-08-06T10:00:00Z", "updated_at": "2026-08-06T10:00:00Z",
    }
    repo.create(task_dict)
    paths = [f"{r.method} {r.url.path}" for r in requests]
    assert "POST /api/v1/tasks" in paths
    assert "POST /api/v1/tasks/aios-t1/start" in paths


def test_create_done_task_calls_complete():
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "POST" and req.url.path == "/api/v1/tasks":
            return _json_response(201, _task_payload("aios-t1", aicc_id="aicc-1", state="open"))
        if req.url.path.endswith("/start"):
            return _json_response(200, _task_payload("aios-t1", aicc_id="aicc-1", state="in_progress"))
        return _json_response(200, _task_payload("aios-t1", aicc_id="aicc-1", state="completed"))

    repo, _ = _make_repo(handler)
    task_dict = {
        "id": "aicc-1", "project": "AICC", "title": "Done", "task_type": "task",
        "status": "Done", "priority": "Medium", "owner": "", "depends_on": [],
        "created_at": "2026-08-06T10:00:00Z", "updated_at": "2026-08-06T10:00:00Z",
    }
    repo.create(task_dict)
    paths = [f"{r.method} {r.url.path}" for r in requests]
    assert "POST /api/v1/tasks/aios-t1/start" in paths
    assert "POST /api/v1/tasks/aios-t1/complete" in paths


# ---------------------------------------------------------------------------
# AIOSTasksRepository.update_status
# ---------------------------------------------------------------------------

def test_update_status_to_done_calls_complete():
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "GET":
            return _json_response(200, _task_payload("aios-t1", aicc_id="aicc-1", state="in_progress"))
        return _json_response(200, _task_payload("aios-t1", aicc_id="aicc-1", state="completed"))

    with tempfile.TemporaryDirectory() as d:
        id_map = AIOSIdMap(Path(d) / "map.json")
        id_map.put("aicc-1", "aios-t1")
        repo, _ = _make_repo(handler, id_map=id_map)

        repo.update_status("aicc-1", "Done")
        paths = [f"{r.method} {r.url.path}" for r in requests]
        assert "POST /api/v1/tasks/aios-t1/complete" in paths


def test_update_status_to_in_progress_calls_start():
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "GET":
            return _json_response(200, _task_payload("aios-t1", aicc_id="aicc-1", state="open"))
        return _json_response(200, _task_payload("aios-t1", aicc_id="aicc-1", state="in_progress"))

    with tempfile.TemporaryDirectory() as d:
        id_map = AIOSIdMap(Path(d) / "map.json")
        id_map.put("aicc-1", "aios-t1")
        repo, _ = _make_repo(handler, id_map=id_map)

        repo.update_status("aicc-1", "In Progress")
        paths = [f"{r.method} {r.url.path}" for r in requests]
        assert "POST /api/v1/tasks/aios-t1/start" in paths


def test_update_status_unknown_task_is_noop():
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(200, _list_payload([]))

    repo, _ = _make_repo(handler)
    # No entry in map -> should not raise, just return None
    result = repo.update_status("nonexistent-aicc-id", "Done")
    assert result is None


# ---------------------------------------------------------------------------
# AIOSTasksRepository.delete
# ---------------------------------------------------------------------------

def test_delete_known_task_calls_cancel():
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "GET":
            return _json_response(200, _task_payload("aios-t1", aicc_id="aicc-1", state="open"))
        return _json_response(200, _task_payload("aios-t1", aicc_id="aicc-1", state="cancelled"))

    with tempfile.TemporaryDirectory() as d:
        id_map = AIOSIdMap(Path(d) / "map.json")
        id_map.put("aicc-1", "aios-t1")
        repo, _ = _make_repo(handler, id_map=id_map)

        result = repo.delete("aicc-1")
        assert result is True
        paths = [f"{r.method} {r.url.path}" for r in requests]
        assert "POST /api/v1/tasks/aios-t1/cancel" in paths


def test_delete_unknown_task_returns_false():
    def handler(req: httpx.Request) -> httpx.Response:
        return _json_response(200, _list_payload([]))

    repo, _ = _make_repo(handler)
    result = repo.delete("nonexistent")
    assert result is False


def test_delete_already_cancelled_task_is_idempotent_false():
    """A reconciled cancelled task is already deleted: no second cancel call."""
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        requests.append(req)
        if req.method == "GET":
            return _json_response(
                200,
                _list_payload(
                    [_task_payload("aios-t1", aicc_id="aicc-1", state="cancelled")["data"]]
                ),
            )
        raise AssertionError(f"unexpected mutation request: {req.method} {req.url.path}")

    repo, _ = _make_repo(handler)
    result = repo.delete("aicc-1")
    assert result is False
    assert all(r.method == "GET" for r in requests)


# ---------------------------------------------------------------------------
# AIOSTasksRepository.upsert / upsert_all — mirrors tasks_repository.save_tasks's
# structural drop of master-projection records (VOYN-W0-AICC-READONLY-LAUNCH-
# SURFACES): the local JSON store refuses these on write, and every other
# backend this repository facade can front must refuse them the same way.
# ---------------------------------------------------------------------------

def test_upsert_refuses_master_projection_task():
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("gateway must not be called for a master-projection record")

    repo, _ = _make_repo(handler)
    repo.upsert({"id": "master-1", "source": "master", "read_only": True, "status": "Backlog"})


def test_upsert_all_refuses_master_projection_tasks():
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("gateway must not be called for a master-projection record")

    repo, _ = _make_repo(handler)
    repo.upsert_all(
        [{"id": "master-1", "source": "master", "read_only": True, "status": "Backlog"}]
    )
