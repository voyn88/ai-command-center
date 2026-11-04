"""Existing review hooks retain real event identity under actual DB grants."""
import pytest

from command_center.orchestrator.review_merge import _record_delivery_incident, _remediate_rejection
from tests.db.test_delivery_team import SHA, TASK, enroll, rows
from tests.db.test_backlog_planner import _task

pytest_plugins = ("tests.db.test_backlog_planner",)
pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]


def test_review_hook_replay_keeps_one_durable_incident(rig, monkeypatch):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig)
    monkeypatch.setenv("AICC_DELIVERY_TEAM_ENABLED", "1")
    args = dict(task_id=TASK, revision=1, repo="repo-one",
                pr_url="https://github.com/example/repo/pull/1", sha=SHA,
                detail="Independent review found an unmet criterion", kind="review_rejection")
    for _ in range(2):
        with factory() as conn:
            _record_delivery_incident(conn, **args)
    assert rows(factory, "SELECT count(*) FROM delivery_team_incident") == [(1,)]
    assert rows(factory, "SELECT evidence->>'kind' FROM delivery_team_incident") == [("review_rejection",)]


def test_review_hook_refusal_rolls_back_callers_transaction(rig, monkeypatch):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig)
    monkeypatch.setenv("AICC_DELIVERY_TEAM_ENABLED", "1")
    with pytest.raises(RuntimeError, match="incident refused"), factory() as conn, conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SELECT backlog_record_evidence(%s,'ci',%s)", (TASK, "https://ci.example/should-rollback"))
        _record_delivery_incident(conn, task_id=TASK, revision=999, repo="repo-one",
                                  pr_url="https://github.com/example/repo/pull/1", sha=SHA,
                                  detail="Actual merge failure", kind="merge_failure")
    assert rows(factory, "SELECT count(*) FROM delivery_team_incident") == [(0,)]
    assert rows(factory, "SELECT count(*) FROM backlog_evidence WHERE kind='ci'") == [(0,)]


def test_existing_correction_does_not_hide_later_distinct_failures(rig, monkeypatch):  # noqa: F811
    factory, store, _queue = rig
    enroll(rig)
    correction = TASK + "-CORRECTION"
    pr_url = "https://github.com/example/repo/pull/1"
    assert store.upsert_task(_task(correction, repo="repo-one"))[0]
    assert store.record_remediation(correction, TASK, pr_url, SHA)[0]
    monkeypatch.setenv("AICC_DELIVERY_TEAM_ENABLED", "1")
    for sha, reason in ((SHA, "First rejected criterion"), ("b"*40, "Second distinct defect"),
                        ("b"*40, "Second distinct defect")):
        assert _remediate_rejection(factory, TASK, pr_url, sha, reason) is None
    assert rows(factory, "SELECT count(*) FROM delivery_team_incident") == [(2,)]
    assert rows(factory, "SELECT count(*) FROM backlog_task_remediation WHERE parent_task_id=%s", (TASK,)) == [(1,)]
    assert store.get_task(TASK)["status"] == "OPEN"
