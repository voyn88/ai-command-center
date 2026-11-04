"""Collectors bind retained external facts through real PostgreSQL grants."""
import hashlib
import json

from types import SimpleNamespace

import pytest

from command_center.orchestrator import delivery_failure_collector as collector
from tests.db.test_backlog_planner import _task
from tests.db.test_delivery_team import SHA, TASK, enroll, rows

pytest_plugins = ("tests.db.test_backlog_planner",)
pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]


def setup_ci(rig, monkeypatch, attempt=1):
    factory, store, _queue = rig
    enroll(rig)
    assert store.record_evidence(TASK, "sha", SHA)[0]
    monkeypatch.setenv("AICC_DELIVERY_TEAM_ENABLED", "1")
    monkeypatch.setattr(collector.gh_access, "current_quota", lambda: SimpleNamespace(
        identity=collector.gh_access.FLEET, allow_ambient_fallback=False, degraded=False))
    monkeypatch.setattr(collector.review_merge, "_origin_owner_repo", lambda path: ("example", "repo"))
    run = dict(id=12, run_attempt=attempt, repository={"full_name": "example/repo"},
               head_sha=SHA, status="completed", conclusion="failure", updated_at="2026-11-04T12:00:00Z")
    monkeypatch.setattr(collector.review_merge, "_rest_json", lambda path, endpoint: ("ok", run))
    return run


def ci(conn, attempt=1):
    return collector.collect_ci_failure(conn, task_id=TASK, revision=1, expected_sha=SHA, run_id=12, attempt=attempt)


def test_actual_ci_attempt_is_idempotent_and_new_attempt_is_distinct(rig, monkeypatch):  # noqa: F811
    factory, _store, _queue = rig
    run = setup_ci(rig, monkeypatch)
    with factory() as conn:
        first = ci(conn)
        assert first.outcome == "admitted"
        assert ci(conn) == first
        run["run_attempt"] = 2
        second = ci(conn, 2)
        assert second.outcome == "admitted" and second.key != first.key
    assert rows(factory, "SELECT count(*) FROM delivery_team_incident") == [(2,)]
    assert rows(factory, "SELECT DISTINCT evidence->>'kind' FROM delivery_team_incident") == [("ci_failure",)]


def test_scope_change_during_ci_fetch_refuses_without_incident(rig, monkeypatch):  # noqa: F811
    factory, store, _queue = rig
    run = setup_ci(rig, monkeypatch)

    def changed_scope(path, endpoint):
        assert store.upsert_task(_task(TASK, repo="repo-one", body="Changed requirement"))[0]
        return "ok", run

    monkeypatch.setattr(collector.review_merge, "_rest_json", changed_scope)
    with factory() as conn:
        assert ci(conn) == collector.CollectionResult("refused", "scope_changed")
    assert rows(factory, "SELECT count(*) FROM delivery_team_incident") == [(0,)]


def test_caller_rollback_does_not_leave_admitted_incident(rig, monkeypatch):  # noqa: F811
    factory, _store, _queue = rig
    setup_ci(rig, monkeypatch)
    with pytest.raises(RuntimeError, match="caller rollback"), factory() as conn, conn.transaction():
        assert ci(conn).outcome == "admitted"
        raise RuntimeError("caller rollback")
    assert rows(factory, "SELECT count(*) FROM delivery_team_incident") == [(0,)]


def test_worker_cannot_collect_or_query_canonical_scope(rig, monkeypatch, psycopg):  # noqa: F811
    _factory, _store, queue = rig
    setup_ci(rig, monkeypatch)
    with queue._connection() as conn, pytest.raises(psycopg.errors.InsufficientPrivilege):
        ci(conn)


def test_retained_deploy_record_is_bound_and_never_copies_stderr(rig, monkeypatch, tmp_path):  # noqa: F811
    factory, store, _queue = rig
    enroll(rig)
    assert store.record_evidence(TASK, "sha", SHA)[0]
    monkeypatch.setenv("AICC_DELIVERY_TEAM_ENABLED", "1")
    record = dict(at="2026-11-04T12:00:00Z", outcome="rolled_back", detail="private stderr fixture-value",
                  previous_sha="b"*40, target_sha=SHA)
    raw = (json.dumps(record)+"\n").encode()
    path = tmp_path / "provenance.jsonl"
    path.write_bytes(raw)
    source = collector.DeploymentSource("repo-one", "host-test", "self-deploy", path)
    kwargs = dict(task_id=TASK, revision=1, expected_sha=SHA, source=source, offset=0,
                  sha256=hashlib.sha256(raw).hexdigest())
    with factory() as conn:
        first = collector.collect_deploy_failure(conn, **kwargs)
        assert first.outcome == "admitted"
        assert collector.collect_deploy_failure(conn, **kwargs) == first
    evidence = rows(factory, "SELECT evidence FROM delivery_team_incident")[0][0]
    assert evidence["kind"] == "deployment_failure"
    assert "private stderr" not in json.dumps(evidence)
    assert hashlib.sha256(raw).hexdigest() in evidence["reference"]
    assert store.get_task(TASK)["status"] == "OPEN"
