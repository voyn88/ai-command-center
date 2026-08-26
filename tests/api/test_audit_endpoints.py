"""Endpoint tests for the Wave-2 Audit surface
(``command_center.api.audit_routes`` → ``audit_service`` → ``runtime.db.audit``).

Hermetic: ``tests/conftest.py`` points ``AICC_DATA_DIR`` at a per-test sandbox
and resets its contents between cases, so the runtime db and ``tasks.json`` the
service writes are throwaway. A fresh :class:`EventBus` is installed per test so
published events are observable and never leak. The check registry is swapped for
a deterministic fake so a run's findings do not depend on ruff output over the
live tree.

Fixtures use only generic project codes (``AICC``, ``BANK``) and invented ids —
no real names or paths — keeping the public-repo privacy gate green.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from command_center.api import audit_service
from command_center.api.app import create_app
from command_center.audit import CheckContext, Finding
from command_center.audit.checks.base import Check
from command_center.audit.registry import CheckRegistry
from command_center.events import (
    AuditFindingCreated,
    AuditFindingPromotedToTask,
    AuditRunCompleted,
    Event,
    default_bus,
)
from command_center.runtime import db
from command_center.tasks_repository import load_tasks


class _FakeSecurityCheck(Check):
    name = "security"
    category = "security"

    def run(self, ctx: CheckContext) -> list[Finding]:
        return [
            Finding(
                category="security", summary="hardcoded secret", owner="security",
                severity="high", file_path="a.py", loc="10:4",
            ),
            Finding(
                category="security", summary="shell=True", owner="security",
                severity="high", file_path="b.py", loc="3:1",
            ),
        ]


class _FakeLintCheck(Check):
    name = "lint"
    category = "lint"

    def run(self, ctx: CheckContext) -> list[Finding]:
        return [
            Finding(
                category="lint", summary="F401 unused import", owner="engineering",
                severity="low", file_path="a.py", loc="1:1",
            )
        ]


def _fake_registry() -> CheckRegistry:
    reg = CheckRegistry()
    reg.register("security", _FakeSecurityCheck)
    reg.register("lint", _FakeLintCheck)
    return reg


@pytest.fixture(autouse=True)
def fake_checks(monkeypatch) -> None:
    """Deterministic checks and a fast target for every test on this surface."""
    monkeypatch.setattr(audit_service, "_registry", _fake_registry)
    monkeypatch.setattr(audit_service, "_resolve_target", lambda project: audit_service.ROOT)


@pytest.fixture
def events() -> list[Event]:
    captured: list[Event] = []
    bus = default_bus()
    bus.clear()
    unsubscribe = bus.subscribe(Event, captured.append)
    try:
        yield captured
    finally:
        unsubscribe()
        bus.clear()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


# --- run ------------------------------------------------------------------


def test_run_persists_findings_all_with_status_and_owner(client, events) -> None:
    r = client.post("/api/v1/audit/run", json={"project": "AICC"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run"]["status"] == "completed"
    assert body["run"]["project_ref"] == "AICC"
    assert body["run"]["finding_count"] == 3
    assert set(body["run"]["checks"]) == {"security", "lint"}
    # every finding carries both triage axes — the acceptance invariant
    for finding in body["findings"]:
        assert finding["status"] == "open"
        assert finding["owner"]
    # events: one completed + one per finding
    assert any(isinstance(e, AuditRunCompleted) for e in events)
    assert sum(isinstance(e, AuditFindingCreated) for e in events) == 3


def test_run_subset_of_checks(client) -> None:
    body = client.post("/api/v1/audit/run", json={"project": "AICC", "checks": ["lint"]}).json()
    assert body["run"]["checks"] == ["lint"]
    assert body["run"]["finding_count"] == 1


def test_run_rejects_sensitive_project(client) -> None:
    for project in ("BANK", "LEGAL"):
        r = client.post("/api/v1/audit/run", json={"project": project})
        assert r.status_code == 400


def test_run_unknown_check_is_client_error(client) -> None:
    r = client.post("/api/v1/audit/run", json={"project": "AICC", "checks": ["nope"]})
    assert r.status_code == 400


def test_run_marks_failed_when_finding_persist_raises_mid_loop(monkeypatch) -> None:
    # A raise while persisting findings must not leave a dangling ``running``
    # run: the finalize loop is guarded so the run is marked ``failed`` and the
    # original error still propagates.
    boom = RuntimeError("db exploded mid-persist")

    def _raise(*_a, **_k):
        raise boom

    monkeypatch.setattr(audit_service.db, "create_audit_finding", _raise)
    client = TestClient(create_app(), raise_server_exceptions=False)

    r = client.post("/api/v1/audit/run", json={"project": "AICC"})
    assert r.status_code == 500

    # The run exists and is terminal-failed, never left ``running``.
    path = audit_service._db_path()
    runs = db.list_audit_runs(path, project="AICC")
    assert runs, "the run row should have been created before the failure"
    assert all(run["status"] == "failed" for run in runs)


# --- runs: list / get -----------------------------------------------------


def test_list_and_get_runs(client) -> None:
    run_id = client.post("/api/v1/audit/run", json={"project": "AICC"}).json()["run"]["id"]
    runs = client.get("/api/v1/audit/runs").json()["runs"]
    assert any(r["id"] == run_id for r in runs)
    got = client.get(f"/api/v1/audit/runs/{run_id}")
    assert got.status_code == 200
    assert got.json()["id"] == run_id
    assert client.get("/api/v1/audit/runs/does-not-exist").status_code == 404


# --- findings: list / filter / status / promote ---------------------------


def test_list_findings_filters(client) -> None:
    run_id = client.post("/api/v1/audit/run", json={"project": "AICC"}).json()["run"]["id"]
    by_run = client.get(f"/api/v1/audit/findings?run={run_id}").json()["findings"]
    assert len(by_run) == 3
    assert len(client.get("/api/v1/audit/findings?owner=security").json()["findings"]) == 2
    assert len(client.get("/api/v1/audit/findings?owner=engineering").json()["findings"]) == 1
    assert len(client.get("/api/v1/audit/findings?status=open").json()["findings"]) == 3


def test_set_finding_status_and_illegal_transition(client) -> None:
    fid = client.post("/api/v1/audit/run", json={"project": "AICC"}).json()["findings"][0]["id"]
    assert client.post(f"/api/v1/audit/findings/{fid}/status", json={"status": "ack"}).json()["status"] == "ack"
    assert client.post(f"/api/v1/audit/findings/{fid}/status", json={"status": "fixed"}).json()["status"] == "fixed"
    # fixed -> ack is an illegal lifecycle edge -> 409
    assert client.post(f"/api/v1/audit/findings/{fid}/status", json={"status": "ack"}).status_code == 409
    assert client.post("/api/v1/audit/findings/nope/status", json={"status": "ack"}).status_code == 404


def test_promote_finding_creates_task_and_emits_event(client, events) -> None:
    fid = client.post("/api/v1/audit/run", json={"project": "AICC"}).json()["findings"][0]["id"]
    r = client.post(f"/api/v1/audit/findings/{fid}/promote")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"]
    assert body["finding"]["status"] == "ack"
    # the task really landed on the board through the single writer
    task_ids = {t["id"] for t in load_tasks(audit_service.ROOT)}
    assert body["task_id"] in task_ids
    assert any(isinstance(e, AuditFindingPromotedToTask) for e in events)


def test_promote_fixed_finding_is_conflict(client) -> None:
    fid = client.post("/api/v1/audit/run", json={"project": "AICC"}).json()["findings"][0]["id"]
    client.post(f"/api/v1/audit/findings/{fid}/status", json={"status": "fixed"})
    assert client.post(f"/api/v1/audit/findings/{fid}/promote").status_code == 409
    assert client.post("/api/v1/audit/findings/nope/promote").status_code == 404


def test_re_promote_is_clean_conflict_not_a_second_orphan_task(client) -> None:
    # A finding promoted once must not be promotable again: the second call is
    # rejected with a clean 409 *before* any task is created — it must never
    # spawn a second orphan task nor 500 on the illegal ack -> ack transition.
    fid = client.post("/api/v1/audit/run", json={"project": "AICC"}).json()["findings"][0]["id"]
    first = client.post(f"/api/v1/audit/findings/{fid}/promote")
    assert first.status_code == 200, first.text
    task_id = first.json()["task_id"]

    before = {t["id"] for t in load_tasks(audit_service.ROOT)}

    second = client.post(f"/api/v1/audit/findings/{fid}/promote")
    assert second.status_code == 409, second.text

    # No second task landed on the board, and the recorded link is unchanged.
    after = {t["id"] for t in load_tasks(audit_service.ROOT)}
    assert after == before
    row = db.get_audit_finding(audit_service._db_path(), fid)
    assert row["promoted_task_id"] == task_id
    assert row["status"] == "ack"


# --- redaction: sensitive rows never leave the surface --------------------


def test_sensitive_runs_and_findings_are_redacted_on_read(client) -> None:
    # Seed a BANK run + finding directly in the repository (bypassing the
    # service's up-front reject) to prove the *read* path excludes them in SQL.
    path = audit_service._db_path()
    bank_run = db.create_audit_run(path, project_ref="BANK")
    db.create_audit_finding(
        path, run_id=bank_run["id"], category="security", summary="secret",
        owner="security", project_ref="BANK",
    )
    client.post("/api/v1/audit/run", json={"project": "AICC"})

    listed = client.get("/api/v1/audit/runs").json()["runs"]
    assert all(r["project_ref"] != "BANK" for r in listed)
    # detail of a sensitive run reads as not found
    assert client.get(f"/api/v1/audit/runs/{bank_run['id']}").status_code == 404
    # sensitive findings never appear in the findings inbox
    findings = client.get("/api/v1/audit/findings").json()["findings"]
    assert all(f["project_ref"] != "BANK" for f in findings)
