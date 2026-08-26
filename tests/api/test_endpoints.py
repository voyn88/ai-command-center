"""Endpoint tests for the read-only API (``command_center.api``).

Hermetic by construction: every backing read path is a module-level name on
``command_center.api.service`` (registry, health collector, tasks loader,
runtime run reader, git log), so each test monkeypatches those globals and the
real registry / database / git checkout is never touched. ``is_sensitive`` is
left real on purpose — the redaction tests must exercise the genuine
BANK/LEGAL policy, not a fake of it.

All routes are versioned under ``/api/v1``.

Fixtures use only generic project-namespace codes (``AICC``, ``BANK``) and
invented ids/paths — no real project names or machine paths — so the public
repo stays clean and the integration privacy fitness gate is unaffected.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import command_center.api.service as service
from command_center.api.app import create_app

# --- fakes ----------------------------------------------------------------

_ENTRIES = [
    {
        "id": "app-one",
        "name": "App One",
        "kind": "application",
        "project": "AICC",
        "repo_path": "/fake/app-one",
        "remote": None,
        "default_branch": "main",
    },
    {
        "id": "secret-one",
        "name": "Confidential Name",
        "kind": "service",
        "project": "BANK",
        "repo_path": "/fake/secret",
        "remote": None,
        "default_branch": "main",
    },
]

_TASKS = [
    {"id": "t1", "project": "AICC", "title": "Do a thing", "status": "Done",
     "task_type": "implementation"},
    {"id": "t2", "project": "AICC", "title": "Fix a thing", "status": "In Progress",
     "task_type": "bugfix", "launch_status": "Requires Attention"},
    # A BANK task that DOES trigger an attention condition — the regression
    # fixture for the redaction leak: an earlier no-attention Backlog task masked
    # the bug because it never reached the attention feed anyway. This one must
    # be dropped from the task list AND from dashboard attention.
    {"id": "t3", "project": "BANK", "title": "Secret leaky task", "status": "In Progress",
     "task_type": "bugfix", "launch_status": "Requires Attention"},
]

_RUNS = [
    {"id": "r1", "source": "v2", "project": "AICC", "task_type": "implementation",
     "state": "RUNNING", "status": "running", "created_at": "2026-08-12T10:00:00Z"},
    {"id": "r2", "source": "v2", "project": "AICC", "task_type": "review",
     "state": "FAILED", "status": "failed", "created_at": "2026-08-12T09:00:00Z"},
    {"id": "r3", "source": "v2", "project": "BANK", "task_type": "implementation",
     "state": "RUNNING", "status": "running", "created_at": "2026-08-12T08:00:00Z"},
]

_COMMITS = [
    {"hash": "abc123", "author": "Dev", "date": "2026-08-12", "subject": "Do work"},
]


def _fake_health(entry: dict) -> dict:
    return {
        "id": entry["id"],
        "worktree_state": "ok",
        "git": {"available": True, "branch": "main", "dirty": False,
                "last_activity": "2026-08-12T10:00:00Z"},
        "github": {"available": True, "open_pr_count": 2, "ci_state": "success"},
    }


@pytest.fixture
def client(monkeypatch) -> TestClient:
    monkeypatch.setattr(service, "load_entries", lambda: list(_ENTRIES))
    monkeypatch.setattr(
        service, "get_entry",
        lambda pid: next((e for e in _ENTRIES if e["id"] == pid), None),
    )
    monkeypatch.setattr(service, "collect_health", _fake_health)
    monkeypatch.setattr(service, "load_tasks", lambda root: list(_TASKS))
    monkeypatch.setattr(
        service, "list_unified_runs",
        lambda db_path, *, root, limit=None: list(_RUNS),
    )
    monkeypatch.setattr(service, "get_git_log", lambda limit=20: list(_COMMITS))
    return TestClient(create_app())


# --- health ---------------------------------------------------------------


def test_health_ok(client: TestClient) -> None:
    body = client.get("/api/v1/health").json()
    assert body["status"] == "ok"
    assert body["service"] == "ai-command-center-api"
    assert body["version"]


# --- dashboard ------------------------------------------------------------


def test_dashboard_shape_and_aggregates(client: TestClient) -> None:
    r = client.get("/api/v1/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"agents", "task_counts", "projects", "activity", "attention"}
    # BANK run is sensitive and dropped, so only the two AICC runs count.
    assert body["agents"]["running"] == 1
    assert body["agents"]["attention"] == 1
    assert body["agents"]["total"] == 2
    # BANK task is sensitive and dropped, so only the two AICC tasks count.
    assert body["task_counts"]["total"] == 2
    assert body["task_counts"]["done"] == 1


def test_dashboard_activity_merges_runs_and_commits(client: TestClient) -> None:
    body = client.get("/api/v1/dashboard").json()
    kinds = {item["kind"] for item in body["activity"]}
    assert kinds == {"run", "commit"}
    # No sensitive project leaks into the activity feed.
    assert all(item.get("project") != "BANK" for item in body["activity"])


def test_dashboard_attention_lists_flagged_task_and_failed_run(client: TestClient) -> None:
    body = client.get("/api/v1/dashboard").json()
    task_titles = {a["title"] for a in body["attention"] if a["kind"] == "task"}
    assert "Fix a thing" in task_titles
    assert any(a["kind"] == "run" for a in body["attention"])
    assert all(a.get("project") != "BANK" for a in body["attention"])


def test_dashboard_attention_never_leaks_flagged_sensitive_task(client: TestClient) -> None:
    """Regression: a flagged BANK task must not reach the attention feed by its
    title (the redaction-leak audit fix)."""
    body = client.get("/api/v1/dashboard").json()
    titles = {a["title"] for a in body["attention"]}
    assert "Secret leaky task" not in titles


# --- projects -------------------------------------------------------------


def test_projects_list_with_health_and_progress(client: TestClient) -> None:
    projects = client.get("/api/v1/projects").json()
    app_one = next(p for p in projects if p["id"] == "app-one")
    assert app_one["health"]["worktree_state"] == "ok"
    assert app_one["health"]["open_pr_count"] == 2
    # AICC has 2 tasks, 1 Done -> 50%.
    assert app_one["progress"] == 50


def test_projects_redacts_sensitive_project(client: TestClient) -> None:
    projects = client.get("/api/v1/projects").json()
    secret = next(p for p in projects if p["id"] == "secret-one")
    assert secret["redacted"] is True
    assert secret["health"] is None
    # The operator-supplied confidential name must not leak.
    assert secret["name"] != "Confidential Name"
    assert secret["name"] == "BANK"


def test_project_by_id(client: TestClient) -> None:
    body = client.get("/api/v1/projects/app-one").json()
    assert body["id"] == "app-one"
    assert body["project_ref"] == "AICC"


def test_project_not_found(client: TestClient) -> None:
    assert client.get("/api/v1/projects/nope").status_code == 404


# --- tasks ----------------------------------------------------------------


def test_tasks_list_excludes_sensitive_with_counts(client: TestClient) -> None:
    body = client.get("/api/v1/tasks").json()
    # The BANK task is dropped entirely — never listed, never counted.
    assert {t["id"] for t in body["tasks"]} == {"t1", "t2"}
    assert all(t["project"] != "BANK" for t in body["tasks"])
    assert body["counts"]["total"] == 2
    assert body["counts"]["done"] == 1


def test_tasks_filter_by_project(client: TestClient) -> None:
    body = client.get("/api/v1/tasks", params={"project": "AICC"}).json()
    assert {t["id"] for t in body["tasks"]} == {"t1", "t2"}
    assert body["counts"]["total"] == 2


def test_tasks_filter_by_status(client: TestClient) -> None:
    body = client.get("/api/v1/tasks", params={"status": "Done"}).json()
    assert {t["id"] for t in body["tasks"]} == {"t1"}


def test_task_by_id(client: TestClient) -> None:
    body = client.get("/api/v1/tasks/t1").json()
    assert body["id"] == "t1"
    assert body["project"] == "AICC"


def test_sensitive_task_detail_is_not_found(client: TestClient) -> None:
    """A BANK task must read as absent on detail, not leak its title."""
    assert client.get("/api/v1/tasks/t3").status_code == 404


def test_task_not_found(client: TestClient) -> None:
    assert client.get("/api/v1/tasks/nope").status_code == 404


# --- agents ---------------------------------------------------------------


def test_agents_summary_excludes_sensitive_runs(client: TestClient) -> None:
    body = client.get("/api/v1/agents").json()
    assert body["available"] is True
    assert {a["run_id"] for a in body["agents"]} == {"r1", "r2"}
    assert body["summary"]["running"] == 1
    assert body["summary"]["attention"] == 1


def test_agents_degrades_to_stub_when_runtime_store_missing(monkeypatch) -> None:
    monkeypatch.setattr(service, "load_entries", lambda: [])

    def _boom(db_path, *, root, limit=None):
        raise RuntimeError("no runtime.db yet")

    monkeypatch.setattr(service, "list_unified_runs", _boom)
    body = TestClient(create_app()).get("/api/v1/agents").json()
    assert body["available"] is False
    assert body["agents"] == []
    assert body["summary"]["total"] == 0
