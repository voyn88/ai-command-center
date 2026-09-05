"""review_once (BO-S3b 2/3) on live PostgreSQL: the store side is real
(READY_TO_REVIEW tasks with pr evidence) and enqueue is a recording stub.

The merge side (BO-S3b 3/3) is `command_center.orchestrator.merge_gateway`,
tested in tests/db/test_merge_gateway.py — this module holds no merge
capability at all (VOYN-W0-AICC-MERGE-GATEWAY)."""

from __future__ import annotations


from tests.db.test_backlog_planner import _test_repo_routes, rig  # noqa: F401 — pytest fixtures
from command_center.orchestrator.review_merge import review_once


def _ready(store, factory, task_id, pr):
    """A task in READY_TO_REVIEW with a pr evidence row — the state part 1
    leaves behind."""
    from tests.db.test_backlog_planner import _task
    assert store.upsert_task(_task(task_id, repo="repo-x", status="OPEN"))[0]
    with factory() as c, c.cursor() as cur:
        # walk OPEN -> IN_PROGRESS -> READY_TO_REVIEW via the real machine;
        # transition's third arg is the bigint revision, re-read each step.
        def _rev():
            cur.execute("SELECT revision FROM backlog_task WHERE task_id=%s", (task_id,))
            return cur.fetchone()[0]
        cur.execute("SELECT ok FROM backlog_transition(%s,'IN_PROGRESS',%s)", (task_id, _rev()))
        cur.execute("SELECT backlog_record_evidence(%s,'pr',%s)", (task_id, pr))
        cur.execute("SELECT ok FROM backlog_transition(%s,'READY_TO_REVIEW',%s)", (task_id, _rev()))
        c.commit()


def test_review_enqueues_one_run_per_ready_task(rig):  # noqa: F811

    app_factory, store, _ = rig
    _ready(store, app_factory, "VOYN-W0-R1", "https://github.com/x/y/pull/7")
    calls = []
    report = review_once(app_factory, lambda q, k, p: calls.append((q, k, p)))
    assert ("VOYN-W0-R1", "https://github.com/x/y/pull/7") in report.reviewed
    assert len(calls) == 1
    q, key, payload = calls[0]
    assert key == "review:VOYN-W0-R1"  # idempotency key
    assert payload["task_type"] == "review" and "pull/7" in payload["prompt"]
