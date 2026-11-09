"""CTO authority with foundation 0035, real roles; not a production rollout."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from command_center.db import roles
from tests.db.test_backlog_planner import _provision
from tests.db.test_queue_claim import _worker_hosts

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]
SQL = (
    Path(__file__).parents[2]
    / "command_center/db/sql/0036_cto_assignment_enforcement.up.sql"
)
TASK = "VOYN-CTO-TEST"
PAYLOAD = {"kind": "agent_run", "backlog_task_id": TASK, "prompt": "bounded test"}
TOKEN = hashlib.sha256(b"disposable claim fixture").hexdigest()


@pytest.fixture
def cto_rig(admin_conn, psycopg, test_dsn, role_passwords, connect_as):
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with connect_as(roles.MIGRATOR_ROLE) as migrator:
        assert migrator.execute("SELECT max(version) FROM schema_migration").fetchone()[0] == 36
        # Production reasserts grants after upgrade; no provisional-only grants.
        roles.apply_table_grants(migrator)
    admin_conn.execute("""INSERT INTO principal(principal_id,kind,db_role,display_name,host,state,trust_tier,created_at,updated_at)
      VALUES('worker:test','worker_host','aicc_worker','test','fixture','active',2,now(),now())""")
    with (
        connect_as(roles.APP_ROLE) as app,
        connect_as(roles.OPERATOR_ROLE) as operator,
        connect_as(roles.WORKER_ROLE) as worker,
    ):
        row = app.execute(
            "SELECT * FROM backlog_upsert_task(%s,'1','P1','OPEN','task','test','bounded','repo-test')",
            (TASK,),
        ).fetchone()
        assert row[0]
        yield app, operator, worker, admin_conn


def assign(rig, payload=None):
    app, operator, _, _ = rig
    rev = app.execute(
        "SELECT revision FROM backlog_task WHERE task_id=%s", (TASK,)
    ).fetchone()[0]
    row = operator.execute(
        "SELECT * FROM cto_assign(%s,%s,'worker:test',%s::jsonb,'operator test assignment')",
        (TASK, rev, json.dumps(payload or PAYLOAD)),
    ).fetchone()
    assert row[0], row
    return row[2]


def activate(rig):
    assert (
        rig[1]
        .execute("SELECT cto_activate('all workers test-compatible')")
        .fetchone()[0]
    )


def enqueue(rig, key="key", payload=None, task=TASK):
    return (
        rig[0]
        .execute(
            "SELECT queue_enqueue('execution',%s,%s::jsonb,%s,'repo-test')",
            (key, json.dumps(payload or PAYLOAD), task),
        )
        .fetchone()[0]
    )


def claim(rig):
    return (
        rig[2]
        .execute("SELECT * FROM queue_claim('execution',%s,300)", (TOKEN,))
        .fetchone()
    )


def start(rig, attempt):
    return (
        rig[2]
        .execute("SELECT * FROM cto_authorize_start(%s,%s)", (attempt, TOKEN))
        .fetchone()
    )


def test_valid_assignment_claim_and_start(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    wid = enqueue(cto_rig)
    result = claim(cto_rig)
    assert result[0] and result[2] == wid
    assert start(cto_rig, result[3])[0]


def test_dispatch_carries_only_its_own_revision_transition(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    result = (
        cto_rig[0]
        .execute(
            "SELECT * FROM backlog_dispatch(%s,'planner',60,2,%s::jsonb,2)",
            (TASK, json.dumps(PAYLOAD)),
        )
        .fetchone()
    )
    assert result[0], result
    bound = (
        cto_rig[0]
        .execute(
            "SELECT task_revision FROM cto_work_binding WHERE work_item_id=%s",
            (result[2],),
        )
        .fetchone()[0]
    )
    assert bound == result[3]
    assert claim(cto_rig)[0]


def test_missing_assignment_does_not_enqueue_or_transition(cto_rig):
    activate(cto_rig)
    assert enqueue(cto_rig) is None
    result = (
        cto_rig[0]
        .execute(
            "SELECT * FROM backlog_dispatch(%s,'planner',60,2,%s::jsonb,2)",
            (TASK, json.dumps(PAYLOAD)),
        )
        .fetchone()
    )
    assert not result[0]
    assert (
        cto_rig[0]
        .execute("SELECT status FROM backlog_task WHERE task_id=%s", (TASK,))
        .fetchone()[0]
        == "OPEN"
    )
    assert (
        cto_rig[0]
        .execute(
            "SELECT count(*) FROM cto_assignment_event WHERE reason='assignment_required'"
        )
        .fetchone()[0]
        == 2
    )


def test_cto_assignment_does_not_bypass_foundation_readiness(cto_rig, psycopg):
    app = cto_rig[0]
    app.execute("SELECT * FROM delivery_team_sync(100,%s::jsonb)", (json.dumps([TASK]),))
    assert app.execute("SELECT count(*) FROM delivery_team_stage WHERE task_id=%s", (TASK,)).fetchone()[0] > 0
    payload = {**PAYLOAD, "task_type": "implementation"}
    assign(cto_rig, payload)
    activate(cto_rig)
    with pytest.raises(psycopg.errors.CheckViolation, match="delivery_team_requirements_not_ready"):
        enqueue(cto_rig, payload=payload)
    assert app.execute("SELECT count(*) FROM work_item WHERE task_id=%s", (TASK,)).fetchone()[0] == 0
    assert app.execute("SELECT count(*) FROM cto_work_binding").fetchone()[0] == 0
    assert app.execute("SELECT status FROM backlog_task WHERE task_id=%s", (TASK,)).fetchone()[0] == "OPEN"


@pytest.mark.parametrize(
    "change",
    [
        {"backlog_task_id": "FOREIGN"},
        {"kind": "simple_task"},
        {"prompt": "forged"},
        {"kind": "service"},
    ],
)
def test_payload_scope_and_type_cannot_be_laundered(cto_rig, change):
    assign(cto_rig)
    activate(cto_rig)
    assert enqueue(cto_rig, payload={**PAYLOAD, **change}) is None


def test_revoke_after_enqueue_refuses_without_budget_use(cto_rig):
    aid = assign(cto_rig)
    activate(cto_rig)
    wid = enqueue(cto_rig)
    assert (
        cto_rig[1]
        .execute("SELECT cto_revoke(%s,'operator revoked')", (aid,))
        .fetchone()[0]
    )
    assert not claim(cto_rig)[0]
    assert cto_rig[0].execute(
        "SELECT attempt_count,state FROM work_item WHERE work_item_id=%s", (wid,)
    ).fetchone() == (0, "ready")


def test_revoke_after_claim_before_start_refuses(cto_rig):
    aid = assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)
    result = claim(cto_rig)
    assert result[0]
    cto_rig[1].execute("SELECT cto_revoke(%s,'operator revoked')", (aid,))
    denied = start(cto_rig, result[3])
    assert not denied[0] and denied[1] == "assignment_revoked"


def test_revoke_after_authorized_start_preserves_same_attempt_drain(cto_rig):
    aid = assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)
    result = claim(cto_rig)
    assert start(cto_rig, result[3])[0]
    cto_rig[1].execute("SELECT cto_revoke(%s,'operator revoked')", (aid,))
    assert start(cto_rig, result[3])[0]


def test_activation_only_grandfathers_active_attempt_identity(cto_rig):
    enqueue(cto_rig)
    result = claim(cto_rig)
    assert result[0]
    enqueue(cto_rig, key="waiting")
    activate(cto_rig)
    assert start(cto_rig, result[3])[0]
    assert not claim(cto_rig)[0]


def test_operator_and_hidden_legacy_functions_are_not_reachable(cto_rig, psycopg):
    app, operator, worker, admin = cto_rig
    for conn in (app, worker):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT cto_activate('forged')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT _cto_legacy_claim('execution',%s,300)", (TOKEN,))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE cto_control SET enabled=true")
    assert not admin.execute("SELECT cto_activate('admin masquerade')").fetchone()[0]
    admin.execute("SET ROLE aicc_operator")
    try:
        assert not admin.execute(
            "SELECT cto_activate('set role masquerade')"
        ).fetchone()[0]
    finally:
        admin.execute("RESET ROLE")


def test_wrong_token_does_not_get_start_permission(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)
    result = claim(cto_rig)
    denied = (
        cto_rig[2]
        .execute("SELECT * FROM cto_authorize_start(%s,%s)", (result[3], "0" * 64))
        .fetchone()
    )
    assert not denied[0] and denied[1] == "invalid_claim"


@pytest.mark.parametrize("count", [1, 101])
def test_unauthorized_prefix_does_not_starve_valid_work(cto_rig, count):
    for n in range(count):
        enqueue(cto_rig, key=f"old-{n}")
    assign(cto_rig)
    activate(cto_rig)
    wid = enqueue(cto_rig, key="approved")
    result = claim(cto_rig)
    assert result[0] and result[2] == wid
    assert (
        cto_rig[0]
        .execute(
            "SELECT max(attempt_count) FROM work_item WHERE idempotency_key LIKE 'old-%'"
        )
        .fetchone()[0]
        == 0
    )


def test_stale_revision_and_retry_revalidate(cto_rig):
    aid = assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)
    result = claim(cto_rig)
    # Failure returns the same work item to ready, and does not grant new authority.
    raw = "disposable claim fixture"
    assert (
        cto_rig[2]
        .execute("SELECT (queue_fail(%s,%s,'retry',true)).ok", (result[3], raw))
        .fetchone()[0]
    )
    cto_rig[3].execute(
        "UPDATE work_item SET available_at=now() WHERE work_item_id=%s", (result[2],)
    )
    cto_rig[1].execute("SELECT cto_revoke(%s,'operator revoked')", (aid,))
    assert not claim(cto_rig)[0]
    # An unrelated canonical edit must not inherit the original assignment.
    cto_rig[0].execute(
        "SELECT * FROM backlog_upsert_task(%s,'1','P1','OPEN','task','edited','bounded','repo-test')",
        (TASK,),
    )
    assert enqueue(cto_rig, key="newrevision") is None


def test_foreign_authenticated_principal_cannot_claim(cto_rig, psycopg, test_dsn):
    assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)
    with _worker_hosts(cto_rig[3], psycopg, test_dsn, 1) as (dsns, names):
        cto_rig[3].execute(
            """INSERT INTO principal(principal_id,kind,db_role,display_name,host,state,trust_tier,created_at,updated_at)
          VALUES('worker:foreign','worker_host',%s,'foreign','fixture','active',2,now(),now())""",
            (names[0],),
        )
        revision = cto_rig[0].execute(
            "SELECT revision FROM backlog_task WHERE task_id=%s", (TASK,)
        ).fetchone()[0]
        conflicting = cto_rig[1].execute(
            "SELECT * FROM cto_assign(%s,%s,'worker:foreign',%s::jsonb,'competing owner')",
            (TASK, revision, json.dumps(PAYLOAD)),
        ).fetchone()
        assert not conflicting[0] and conflicting[1] == "assignment_conflict"
        with psycopg.connect(dsns[0], autocommit=True) as foreign:
            assert not foreign.execute(
                "SELECT (queue_claim('execution',%s,300)).ok", (TOKEN,)
            ).fetchone()[0]
        assert claim(cto_rig)[0]


def test_two_claims_cannot_share_one_work_item(cto_rig, connect_as):
    assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)

    def one():
        with connect_as(roles.WORKER_ROLE) as conn:
            return conn.execute(
                "SELECT (queue_claim('execution',%s,300)).ok", (TOKEN,)
            ).fetchone()[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: one(), range(2)))
    assert results.count(True) == 1


def test_rollback_denial_is_logged_without_creating_work(cto_rig):
    activate(cto_rig)
    app = cto_rig[0]
    before = app.execute("SELECT count(*) FROM cto_assignment_event").fetchone()[0]
    with pytest.raises(RuntimeError, match="caller rollback"):
        with app.transaction():
            assert enqueue(cto_rig) is None
            raise RuntimeError("caller rollback")
    # Transactional ledger correctly rolls back; _cto_audit RAISE LOG is
    # inspected independently in the disposable server log by the harness.
    assert (
        app.execute("SELECT count(*) FROM cto_assignment_event").fetchone()[0] == before
    )
    assert app.execute("SELECT count(*) FROM work_item").fetchone()[0] == 0


def test_disabled_downgrade_restores_existing_queue(cto_rig):
    cto_rig[3].execute(SQL.with_name("0036_cto_assignment_enforcement.down.sql").read_text())
    enqueue(cto_rig)
    assert claim(cto_rig)[0]


def test_explicit_reassignment_after_revoke_needs_new_work_binding(cto_rig):
    old = assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)
    cto_rig[1].execute("SELECT cto_revoke(%s,'replace assignment')", (old,))
    new = assign(cto_rig)
    assert old != new
    assert enqueue(cto_rig) is None  # no laundering an old work identity
    wid = enqueue(cto_rig, key="replacement")
    result = claim(cto_rig)
    assert result[0] and result[2] == wid


@pytest.mark.parametrize("winner", ["revoke", "start"])
def test_start_and_revoke_serialize_at_database_boundary(cto_rig, connect_as, winner):
    aid = assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)
    result = claim(cto_rig)
    first = cto_rig[1] if winner == "revoke" else cto_rig[2]
    second_role = roles.WORKER_ROLE if winner == "revoke" else roles.OPERATOR_ROLE

    def competing():
        with connect_as(second_role) as conn:
            if winner == "revoke":
                return conn.execute(
                    "SELECT (cto_authorize_start(%s,%s)).ok", (result[3], TOKEN)
                ).fetchone()[0]
            return conn.execute(
                "SELECT cto_revoke(%s,'racing revoke')", (aid,)
            ).fetchone()[0]

    with ThreadPoolExecutor(max_workers=1) as pool:
        with first.transaction():
            if winner == "revoke":
                assert first.execute(
                    "SELECT cto_revoke(%s,'racing revoke')", (aid,)
                ).fetchone()[0]
            else:
                assert first.execute(
                    "SELECT (cto_authorize_start(%s,%s)).ok", (result[3], TOKEN)
                ).fetchone()[0]
            future = pool.submit(competing)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                waiting = (
                    cto_rig[3]
                    .execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND wait_event='advisory'"
                    )
                    .fetchone()[0]
                )
                if waiting:
                    break
                time.sleep(0.01)
            assert waiting, "competing transaction never waited for authorization lock"
            assert not future.done()
        assert future.result(timeout=3) is (winner == "start")
    assert start(cto_rig, result[3])[0] is (winner == "start")


def test_active_enforcement_cannot_be_downgraded_to_bypass(cto_rig, psycopg):
    activate(cto_rig)
    with pytest.raises(psycopg.errors.RaiseException, match="forward migration"):
        cto_rig[3].execute(SQL.with_name("0036_cto_assignment_enforcement.down.sql").read_text())
    assert cto_rig[0].execute("SELECT enabled FROM cto_control").fetchone()[0]


def test_unknown_attempt_does_not_copy_caller_text_into_audit(cto_rig):
    denied = start(cto_rig, "untrusted-attempt-text")
    assert not denied[0]
    assert (
        cto_rig[0]
        .execute("SELECT attempt_id FROM cto_assignment_event WHERE action='start'")
        .fetchone()[0]
        is None
    )


def test_malformed_claim_hash_is_refused_without_claiming(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    wid = enqueue(cto_rig)
    denied = (
        cto_rig[2]
        .execute("SELECT * FROM queue_claim('execution',%s,300)", ("z" * 64,))
        .fetchone()
    )
    assert not denied[0] and denied[1] == "bad_claim_token_hash"
    assert cto_rig[0].execute(
        "SELECT attempt_count,state FROM work_item WHERE work_item_id=%s", (wid,)
    ).fetchone() == (0, "ready")


@pytest.mark.parametrize("legacy", [False, True])
def test_principal_revocation_prevents_another_start_even_for_drain(cto_rig, legacy):
    if not legacy:
        assign(cto_rig)
        activate(cto_rig)
    enqueue(cto_rig)
    result = claim(cto_rig)
    if legacy:
        activate(cto_rig)
    assert start(cto_rig, result[3])[0]
    cto_rig[3].execute(
        "UPDATE principal SET state='retired' WHERE principal_id='worker:test'"
    )
    denied = start(cto_rig, result[3])
    assert not denied[0] and denied[1] == "principal_inactive"


def test_unrevoked_assignment_does_not_follow_unrelated_revision_edit(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)
    cto_rig[0].execute(
        "SELECT * FROM backlog_upsert_task(%s,'1','P1','OPEN','task','edited','bounded','repo-test')",
        (TASK,),
    )
    assert not claim(cto_rig)[0]
    assert cto_rig[0].execute(
        "SELECT count(*) FROM cto_assignment_event WHERE reason='stale_revision'"
    ).fetchone()[0] == 1
