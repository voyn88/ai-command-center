"""Durable lane transitions on real PostgreSQL (skipped without test DSN)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from command_center.orchestrator.control_plane import (
    Action,
    ActionOutcome,
    PostgresControlPlaneStore,
)
from tests.db.test_backlog_planner import _task, rig  # noqa: F401
from tests.db.test_review_merge import _ready

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]


def _record(factory, task_id, kind, value):
    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT ok,reason FROM backlog_record_evidence(%s,%s,%s)",
            (task_id, kind, value),
        )
        ok, reason = cur.fetchone()
        assert ok, reason
        conn.commit()


def test_exact_sha_evidence_schedules_and_advances_guarded_publish(rig):  # noqa: F811
    factory, backlog, _worker = rig
    task_id = "VOYN-W0-CP"
    sha = "a" * 40
    assert backlog.upsert_task(_task(task_id, repo="repo-cp"))[0]
    task = backlog.get_task(task_id)
    assert backlog.transition(task_id, "IN_PROGRESS", task["revision"])[0]
    _record(factory, task_id, "sha", sha)
    _record(factory, task_id, "ci", f"LOCAL_TESTS:PASS:{sha}")
    _record(factory, task_id, "acceptance", f"INDEPENDENT_REVIEW:PASS:{sha}")

    store = PostgresControlPlaneStore(factory)
    now = datetime.now(UTC)
    assert store.discover_ready_lanes(now=now) == 1
    lane = store.claim("reconciler-a", now=now, lease_seconds=60)

    assert lane is not None
    assert lane.task_id == task_id
    assert lane.action is Action.GUARDED_PUBLISH
    assert lane.payload == {"head_sha": sha}
    store.finish(lane, ActionOutcome.succeeded(Action.CI_WAIT), now=now)
    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT state,next_action,owner,claimant,lease_expires_at "
            "FROM control_plane_lane WHERE task_id=%s",
            (task_id,),
        )
        assert cur.fetchone() == (
            "READY",
            "CI_WAIT",
            "control-plane",
            None,
            None,
        )


def test_watchdog_requeues_once_then_blocks_at_the_retry_budget(rig):  # noqa: F811
    factory, backlog, _worker = rig
    task_id = "VOYN-W0-STALLED"
    assert backlog.upsert_task(_task(task_id, repo="repo-cp"))[0]
    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO control_plane_lane(task_id,state,next_action,owner,claimant,"
            "deadline_at,heartbeat_at,lease_expires_at,attempts,max_attempts) "
            "VALUES (%s,'RUNNING','MERGE','merge-controller','dead-owner',"
            "now()-interval '5 minutes',now()-interval '5 minutes',"
            "now()-interval '4 minutes',0,2)",
            (task_id,),
        )
        conn.commit()

    store = PostgresControlPlaneStore(factory)
    recovered = store.recover_stalled(now=datetime.now(UTC))
    assert recovered == [(task_id, "watchdog_requeue")]

    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE control_plane_lane SET state='RUNNING',claimant='dead-owner',"
            "lease_expires_at=now()-interval '1 minute',attempts=max_attempts "
            "WHERE task_id=%s",
            (task_id,),
        )
        conn.commit()
    recovered = store.recover_stalled(now=datetime.now(UTC) + timedelta(seconds=1))
    assert recovered == [(task_id, "attempt_budget_exhausted")]
    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT state,claimant,lease_expires_at,attempts FROM control_plane_lane WHERE task_id=%s",
            (task_id,),
        )
        assert cur.fetchone() == ("BLOCKED", None, None, 3)
        cur.execute(
            "SELECT kind,owner,state FROM control_plane_notification WHERE task_id=%s",
            (task_id,),
        )
        assert cur.fetchone() == ("LANE_STALLED", "merge-controller", "PENDING")


def test_expired_claim_cannot_finish_and_watchdog_returns_it_to_ready(rig):  # noqa: F811
    factory, backlog, _worker = rig
    task_id = "VOYN-W0-EXPIRED-CLAIM"
    assert backlog.upsert_task(_task(task_id, repo="repo-cp"))[0]
    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO control_plane_lane(task_id,next_action,owner,deadline_at) "
            "VALUES (%s,'MERGE','merge-controller',now())",
            (task_id,),
        )
    store = PostgresControlPlaneStore(factory)
    now = datetime.now(UTC)
    lane = store.claim("owner-a", now=now, lease_seconds=1)
    assert lane is not None

    with pytest.raises(RuntimeError, match="lane_ownership_or_revision_lost"):
        store.finish(lane, ActionOutcome.succeeded(), now=now + timedelta(seconds=2))
    assert store.recover_stalled(now=now + timedelta(seconds=2)) == [
        (task_id, "watchdog_requeue")
    ]


def test_deployment_blocker_split_is_a_separate_idempotent_backlog_task(rig):  # noqa: F811
    factory, backlog, _worker = rig
    task_id = "VOYN-W0-MERGED-SOURCE"
    assert backlog.upsert_task(_task(task_id, repo="repo-cp"))[0]
    store = PostgresControlPlaneStore(factory)
    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO control_plane_lane(task_id,next_action,owner,deadline_at) "
            "VALUES (%s,'DEPLOY','deployer',now())",
            (task_id,),
        )
        conn.commit()
    now = datetime.now(UTC)
    lane = store.claim("owner-a", now=now, lease_seconds=60)
    assert lane is not None

    first = store.split_deployment_blocker(lane, "worker target not ready", now=now)
    second = store.split_deployment_blocker(
        lane, "worker target not ready", now=now
    )

    assert first == second
    blocker = backlog.get_task(first)
    assert blocker["status"] == "OPEN"
    assert blocker["repo"] == "repo-cp"


def test_merged_lane_requires_deploy_then_backlog_sync(rig):  # noqa: F811
    factory, backlog, _worker = rig
    task_id = "VOYN-W0-MERGED-LANE"
    merged_sha = "6" * 40
    _ready(backlog, factory, task_id, "https://github.com/x/y/pull/94")
    _record(factory, task_id, "ci", f"MERGED:{merged_sha}")
    store = PostgresControlPlaneStore(factory)
    now = datetime.now(UTC)

    assert store.discover_ready_lanes(now=now) == 1
    lane = store.claim("owner-a", now=now, lease_seconds=60)
    assert lane is not None
    assert lane.action is Action.DEPLOY
    assert lane.payload["merged_sha"] == merged_sha
    store.finish(lane, ActionOutcome.deployment_blocked("deployer offline"), now=now)

    _record(factory, task_id, "ci", f"DEPLOYED:{merged_sha}")
    assert store.discover_ready_lanes(now=now + timedelta(seconds=1)) == 1
    lane = store.claim(
        "owner-b", now=now + timedelta(seconds=1), lease_seconds=60
    )
    assert lane is not None
    assert lane.action is Action.BACKLOG_SYNC


def test_component_circuit_opens_after_three_consecutive_failures(rig):  # noqa: F811
    factory, _backlog, _worker = rig
    store = PostgresControlPlaneStore(factory)
    now = datetime.now(UTC)
    for attempt in range(3):
        store.component_result(
            "aicc-backlog-review.timer",
            ok=False,
            detail=f"failure-{attempt}",
            now=now,
            circuit_seconds=60,
        )

    assert not store.component_allows_attempt(
        "aicc-backlog-review.timer", now=now + timedelta(seconds=30)
    )
    assert store.component_allows_attempt(
        "aicc-backlog-review.timer", now=now + timedelta(seconds=61)
    )


def test_backlog_sync_atomically_closes_only_after_matching_deploy_evidence(
    rig,  # noqa: F811
):
    factory, backlog, _worker = rig
    task_id = "VOYN-W0-DEPLOYED"
    merged_sha = "d" * 40
    _ready(backlog, factory, task_id, "https://github.com/x/y/pull/91")
    _record(factory, task_id, "ci", f"MERGED:{merged_sha}")
    _record(factory, task_id, "ci", f"DEPLOYED:{merged_sha}")
    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO control_plane_lane(task_id,next_action,owner,deadline_at,payload) "
            "VALUES (%s,'BACKLOG_SYNC','control-plane',now(),%s)",
            (task_id, {"merged_sha": merged_sha}),
        )
        conn.commit()

    store = PostgresControlPlaneStore(factory)
    now = datetime.now(UTC)
    lane = store.claim("owner-a", now=now, lease_seconds=60)
    assert lane is not None
    store.finish(lane, ActionOutcome.succeeded(), now=now)

    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT t.status,l.state,l.next_action FROM backlog_task t "
            "JOIN control_plane_lane l USING(task_id) WHERE t.task_id=%s",
            (task_id,),
        )
        assert cur.fetchone() == ("DONE", "DONE", "NONE")
        cur.execute(
            "SELECT count(*) FROM backlog_evidence WHERE task_id=%s "
            "AND kind='sha' AND value=%s",
            (task_id, merged_sha),
        )
        assert cur.fetchone()[0] == 1


def test_backlog_sync_refuses_done_without_deployment_evidence(rig):  # noqa: F811
    factory, backlog, _worker = rig
    task_id = "VOYN-W0-NOT-DEPLOYED"
    merged_sha = "e" * 40
    _ready(backlog, factory, task_id, "https://github.com/x/y/pull/92")
    _record(factory, task_id, "ci", f"MERGED:{merged_sha}")
    with factory() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO control_plane_lane(task_id,next_action,owner,deadline_at,payload) "
            "VALUES (%s,'BACKLOG_SYNC','control-plane',now(),%s)",
            (task_id, {"merged_sha": merged_sha}),
        )
        conn.commit()

    store = PostgresControlPlaneStore(factory)
    now = datetime.now(UTC)
    lane = store.claim("owner-a", now=now, lease_seconds=60)
    assert lane is not None
    with pytest.raises(RuntimeError, match="deployment_evidence_not_durable"):
        store.finish(lane, ActionOutcome.succeeded(), now=now)

    with factory() as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        assert cur.fetchone()[0] == "READY_TO_REVIEW"
