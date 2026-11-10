"""CTO authority with foundation 0035, real roles; not a production rollout."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from command_center.db import roles
from tests.db.test_backlog_planner import _provision
from tests.db.test_queue_claim import _worker_hosts

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]
SQL = (
    Path(__file__).parents[2]
    / "command_center/db/sql/0036_cto_assignment_enforcement.up.sql"
)
SCOPE_SQL = SQL.with_name("0037_cto_enforcement_scope.up.sql")
TASK = "VOYN-CTO-TEST"
PAYLOAD = {"kind": "agent_run", "backlog_task_id": TASK, "prompt": "bounded test"}
TOKEN = hashlib.sha256(b"disposable claim fixture").hexdigest()


@pytest.fixture
def cto_rig(admin_conn, psycopg, test_dsn, role_passwords, connect_as, request):
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with connect_as(roles.MIGRATOR_ROLE) as migrator:
        assert migrator.execute("SELECT max(version) FROM schema_migration").fetchone()[0] == 37
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
        # 0037 applies activated enforcement only to the operator's explicit
        # scope (an enrolled task the operator added), so every assertion in
        # this module exercises that scoped path unless a test opts out.
        app.execute("SELECT * FROM delivery_team_sync(100,%s::jsonb)", (json.dumps([TASK]),))
        if getattr(request, "param", None) != "unscoped":
            assert operator.execute(
                "SELECT cto_scope_add(%s,'pilot scope for tests')", (TASK,)
            ).fetchone()[0]
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


def try_activate(rig):
    return rig[1].execute("SELECT cto_activate('all workers test-compatible')").fetchone()[0]


def activate(rig):
    assert try_activate(rig)


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
    key = (
        cto_rig[0]
        .execute(
            "SELECT idempotency_key FROM work_item WHERE work_item_id=%s", (result[2],)
        )
        .fetchone()[0]
    )
    assert enqueue(cto_rig, key=key) == result[2]
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


def test_activation_refuses_unbound_scoped_work_instead_of_grandfathering(cto_rig):
    # 0037: ready/claimed scoped work admitted while enforcement was off is
    # neither grandfathered nor left silently unclaimable; activation refuses.
    enqueue(cto_rig)
    result = claim(cto_rig)
    assert result[0]
    enqueue(cto_rig, key="waiting")
    assert not try_activate(cto_rig)
    assert cto_rig[0].execute(
        "SELECT count(*) FROM cto_assignment_event WHERE action='activate' AND reason='unbound_scoped_work'"
    ).fetchone()[0] == 1
    assert not cto_rig[0].execute("SELECT enabled FROM cto_control").fetchone()[0]


def test_operator_and_hidden_legacy_functions_are_not_reachable(cto_rig, psycopg):
    app, operator, worker, admin = cto_rig
    for conn in (app, worker):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT cto_activate('forged')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT cto_deactivate('forged')")
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
    activate(cto_rig)
    # A denied prefix of revoked bound work (0037 forbids unbound scoped work).
    for n in range(count):
        prefix = {**PAYLOAD, "prompt": f"old-{n}"}
        aid = assign(cto_rig, prefix)
        assert enqueue(cto_rig, key=f"old-{n}", payload=prefix)
        cto_rig[1].execute("SELECT cto_revoke(%s,'operator revoked prefix')", (aid,))
    assign(cto_rig)
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
        revision = (
            cto_rig[0]
            .execute("SELECT revision FROM backlog_task WHERE task_id=%s", (TASK,))
            .fetchone()[0]
        )
        conflicting = (
            cto_rig[1]
            .execute(
                "SELECT * FROM cto_assign(%s,%s,'worker:foreign',%s::jsonb,'competing owner')",
                (TASK, revision, json.dumps(PAYLOAD)),
            )
            .fetchone()
        )
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


@pytest.mark.parametrize("cto_rig", ["unscoped"], indirect=True)
def test_disabled_downgrade_restores_existing_queue(cto_rig):
    cto_rig[3].execute(SCOPE_SQL.with_name("0037_cto_enforcement_scope.down.sql").read_text())
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


@pytest.mark.parametrize(
    "down", ["0037_cto_enforcement_scope.down.sql", "0036_cto_assignment_enforcement.down.sql"]
)
def test_active_enforcement_cannot_be_downgraded_to_bypass(cto_rig, psycopg, down):
    activate(cto_rig)
    with pytest.raises(psycopg.errors.RaiseException, match="forward migration"):
        cto_rig[3].execute(SQL.with_name(down).read_text())
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
    assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)
    result = claim(cto_rig)
    if legacy:
        # Re-activation snapshots the active attempt as a legacy drain.
        assert cto_rig[1].execute("SELECT cto_deactivate('drain test')").fetchone()[0]
        activate(cto_rig)
        assert cto_rig[3].execute(
            "SELECT legacy_drain FROM cto_execution_start WHERE attempt_id=%s", (result[3],)
        ).fetchone() == (True,)
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
    assert (
        cto_rig[0]
        .execute(
            "SELECT count(*) FROM cto_assignment_event WHERE reason='stale_revision'"
        )
        .fetchone()[0]
        == 1
    )


def _null_enqueue(admin):
    """Fault injection: a producer must not turn an explicit refusal into work."""
    admin.execute("""CREATE OR REPLACE FUNCTION queue_enqueue(
      p_queue text,p_idempotency_key text,p_payload jsonb,p_task_id text DEFAULT NULL,
      p_repository_id text DEFAULT NULL,p_max_attempts integer DEFAULT 3,
      p_priority integer DEFAULT 0,p_delay_seconds integer DEFAULT 0,p_backoff_seconds integer DEFAULT 2)
      RETURNS text LANGUAGE sql AS 'SELECT NULL::text'""")


COORDINATION = {"kind": "agent_run", "mode": "coordination_v2"}


def _coordinator(app, payload=None):
    return app.execute(
        "SELECT * FROM coordinator_enqueue('cto-test','snapshot','blocker','coord-key','execution',%s::jsonb)",
        (json.dumps(payload or COORDINATION),),
    ).fetchone()


def test_enabled_coordinator_is_explicitly_held_without_cas_or_work(cto_rig):
    activate(cto_rig)
    # Held only when its payload names enrolled work (0037 scope).
    enrolled = {**COORDINATION, "backlog_task_id": TASK}
    assert _coordinator(cto_rig[0], enrolled) == (None, "cto_assignment_required")
    assert (
        cto_rig[0].execute("SELECT count(*) FROM coordinator_state").fetchone()[0] == 0
    )
    assert cto_rig[0].execute("SELECT count(*) FROM work_item").fetchone()[0] == 0
    assert (
        cto_rig[0]
        .execute(
            "SELECT reason FROM cto_assignment_event WHERE action='coordinator_enqueue'"
        )
        .fetchone()[0]
        == "assignment_required"
    )


def test_disabled_coordinator_preserves_legacy_enqueue_and_cas(cto_rig):
    wid, failure = _coordinator(cto_rig[0])
    assert wid is not None and failure is None
    assert cto_rig[0].execute(
        "SELECT last_status,in_flight_key FROM coordinator_state"
    ).fetchone() == ("queued", "coord-key")
    assert claim(cto_rig)[0]


def test_null_coordinator_enqueue_clears_cas_and_returns_real_failure(cto_rig):
    _null_enqueue(cto_rig[3])
    assert _coordinator(cto_rig[0]) == (None, "enqueue_failed:cto_enqueue_refused")
    assert cto_rig[0].execute(
        "SELECT last_status,in_flight_key FROM coordinator_state"
    ).fetchone() == ("enqueue_failed", None)
    assert (
        cto_rig[0]
        .execute(
            "SELECT reason FROM cto_assignment_event WHERE action='coordinator_enqueue'"
        )
        .fetchone()[0]
        == "enqueue_refused"
    )


def test_null_backlog_enqueue_rolls_back_status_and_lease_but_keeps_denial(cto_rig):
    _null_enqueue(cto_rig[3])
    result = (
        cto_rig[0]
        .execute(
            "SELECT * FROM backlog_dispatch(%s,'planner',60,2,%s::jsonb,2)",
            (TASK, json.dumps(PAYLOAD)),
        )
        .fetchone()
    )
    assert (
        result[0] is False and result[1] == "cto_enqueue_refused" and result[2] is None
    )
    assert (
        cto_rig[0]
        .execute("SELECT status FROM backlog_task WHERE task_id=%s", (TASK,))
        .fetchone()[0]
        == "OPEN"
    )
    assert (
        cto_rig[0]
        .execute(
            "SELECT count(*) FROM backlog_writer_lease WHERE authority='repo:repo-test'"
        )
        .fetchone()[0]
        == 0
    )
    assert (
        cto_rig[0]
        .execute("SELECT reason FROM cto_assignment_event WHERE action='dispatch'")
        .fetchone()[0]
        == "enqueue_refused"
    )


@pytest.mark.parametrize("cto_rig", ["foundation"], indirect=True)
@pytest.mark.parametrize("enabled", [False, True])
def test_foundation_role_dispatch_handles_enqueue_refusal(cto_rig, enabled):
    app = cto_rig[0]
    app.execute("SELECT delivery_team_sync(100,NULL)")
    revision, digest = app.execute(
        "SELECT revision,input_digest FROM delivery_team_stage WHERE task_id=%s AND stage='ba'",
        (TASK,),
    ).fetchone()
    payload = {
        "kind": "agent_run",
        "task_type": "review",
        "untrusted": False,
        "delivery_team": {
            "task_id": TASK,
            "revision": revision,
            "stage": "ba",
            "input_digest": digest,
        },
        "cascade": [{"executor": "claude", "task_type": "review"}],
    }
    if enabled:
        activate(cto_rig)  # missing assignment: real fail-closed queue refusal
    else:
        _null_enqueue(cto_rig[3])
    result = app.execute(
        "SELECT * FROM delivery_team_dispatch(%s,%s,'ba',%s,%s::jsonb,2)",
        (TASK, revision, digest, json.dumps(payload)),
    ).fetchone()
    assert result == (False, "cto_enqueue_refused", None)
    assert app.execute(
        "SELECT status,work_item_id,dispatch_count FROM delivery_team_stage WHERE task_id=%s AND stage='ba'",
        (TASK,),
    ).fetchone() == ("pending", None, 0)
    assert (
        app.execute(
            "SELECT reason FROM cto_assignment_event WHERE action='delivery_team_dispatch'"
        ).fetchone()[0]
        == "enqueue_refused"
    )


@pytest.mark.parametrize("cto_rig", ["foundation"], indirect=True)
def test_foundation_role_dispatch_disabled_baseline_still_queues(cto_rig):
    app = cto_rig[0]
    app.execute("SELECT delivery_team_sync(100,NULL)")
    revision, digest = app.execute(
        "SELECT revision,input_digest FROM delivery_team_stage WHERE task_id=%s AND stage='ba'",
        (TASK,),
    ).fetchone()
    payload = {
        "kind": "agent_run",
        "task_type": "review",
        "untrusted": False,
        "delivery_team": {
            "task_id": TASK,
            "revision": revision,
            "stage": "ba",
            "input_digest": digest,
        },
        "cascade": [{"executor": "claude", "task_type": "review"}],
    }
    result = app.execute(
        "SELECT * FROM delivery_team_dispatch(%s,%s,'ba',%s,%s::jsonb,2)",
        (TASK, revision, digest, json.dumps(payload)),
    ).fetchone()
    assert result[0] and result[2]
    assert app.execute(
        "SELECT status,work_item_id FROM delivery_team_stage WHERE task_id=%s AND stage='ba'",
        (TASK,),
    ).fetchone() == ("queued", result[2])


def test_large_denied_prefix_uses_indexed_binding_selection_and_revoke_progresses(
    cto_rig, connect_as, tmp_path
):
    app, operator, _, admin = cto_rig
    # 5,000 out-of-scope legacy items queued behind (priority -1), plus 2,000
    # revoked bindings ahead that are never eligible. (0037: unbound scoped
    # work cannot exist while enforcement is active.)
    outside = "VOYN-CTO-OUTSIDE"
    app.execute(
        "SELECT queue_enqueue('execution','legacy-'||n,%s::jsonb,%s,'repo-test',3,-1) FROM generate_series(1,5000) n",
        (json.dumps({**PAYLOAD, "backlog_task_id": outside}), outside),
    )
    activate(cto_rig)
    operator.execute(
        "SELECT cto_assign(%s,1,'worker:test',jsonb_build_object('kind','agent_run','backlog_task_id',%s::text,'prompt','revoked-'||n),'perf revoked seed') FROM generate_series(1,2000) n",
        (TASK, TASK),
    )
    seeded = app.execute(
        "SELECT queue_enqueue('execution','revoked-'||n,jsonb_build_object('kind','agent_run','backlog_task_id',%s::text,'prompt','revoked-'||n),%s,'repo-test') FROM generate_series(1,2000) n",
        (TASK, TASK),
    ).fetchall()
    assert len(seeded) == 2000 and all(row[0] for row in seeded)
    operator.execute(
        "SELECT cto_revoke(assignment_id,'replace perf assignment') FROM cto_assignment WHERE reference='perf revoked seed'"
    )
    assign(cto_rig)
    approved = enqueue(cto_rig, key="approved-tail")
    for table in ("work_item", "cto_assignment", "cto_work_binding", "backlog_task"):
        admin.execute("ANALYZE " + table)
    # The active claim is 0037's; its authorized selection is 0036's verbatim.
    source = SCOPE_SQL.read_text()
    query = source[source.index("    WITH authorized AS MATERIALIZED (") :]
    query = query[: query.index("    -- END AUTHORIZED SELECTION")].strip().rstrip(";")
    query = (
        query.replace("INTO it ", "")
        .replace("p_queue", "'execution'")
        .replace("current_principal()", "'worker:test'")
    )
    plan = admin.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + query).fetchone()[0]
    nodes = []

    def walk(node):
        nodes.append(
            {
                key: node[key]
                for key in (
                    "Node Type",
                    "Relation Name",
                    "Index Name",
                    "Actual Rows",
                    "Actual Loops",
                    "Rows Removed by Filter",
                )
                if key in node
            }
        )
        for child in node.get("Plans", []):
            walk(child)

    walk(plan[0]["Plan"])
    work_nodes = [n for n in nodes if n.get("Relation Name") == "work_item"]
    assert work_nodes and all(
        n["Node Type"] in ("Index Scan", "Index Only Scan") for n in work_nodes
    )
    assert sum(n["Actual Rows"] * n["Actual Loops"] for n in work_nodes) <= 2
    binding_nodes = [n for n in nodes if n.get("Relation Name") == "cto_work_binding"]
    assert binding_nodes and all(
        n["Node Type"] in ("Index Scan", "Index Only Scan") for n in binding_nodes
    )
    assert sum(n["Actual Rows"] * n["Actual Loops"] for n in binding_nodes) == 1
    # 0037's unbound (out-of-scope) selection: index order plus one binding
    # probe per row, stopping at the first unbound row. Its cost is the 2,001
    # bound rows ahead, never the 5,000 ready rows behind them.
    unbound = source[source.index("    SELECT wi.* INTO unscoped FROM work_item wi") :]
    unbound = unbound[: unbound.index("FOR UPDATE OF wi SKIP LOCKED LIMIT 1;")] + "LIMIT 1"
    unbound = unbound.replace("SELECT wi.* INTO unscoped", "SELECT wi.*").replace("p_queue", "'execution'")
    unbound_plan = admin.execute("EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) " + unbound).fetchone()[0]
    unbound_nodes = []

    def walk_unbound(node):
        unbound_nodes.append(node)
        for child in node.get("Plans", []):
            walk_unbound(child)

    walk_unbound(unbound_plan[0]["Plan"])
    scanned = [n for n in unbound_nodes if n.get("Relation Name") == "work_item"]
    assert scanned and all(n["Node Type"] in ("Index Scan", "Index Only Scan") for n in scanned)
    examined = sum(
        n["Actual Rows"] * n["Actual Loops"] + n.get("Rows Removed by Filter", 0) for n in scanned
    )
    assert examined <= 2001 + 2, examined
    assert not any(n["Node Type"] == "Function Scan" for n in unbound_nodes)
    started = time.monotonic()
    assert claim(cto_rig)[2] == approved
    claim_ms = (time.monotonic() - started) * 1000
    # Generous CI budget for one claim over 7,001 ready rows under the lock.
    assert claim_ms < 2000, claim_ms
    # A second approved item races revocation. Both decisions must progress;
    # any granted claim still fails start after the winning revoke commits.
    race_payload = {**PAYLOAD, "prompt": "race tail"}
    current = assign(cto_rig, race_payload)
    assert enqueue(cto_rig, key="race-tail", payload=race_payload)
    barrier = Barrier(2)

    def claimant():
        with connect_as(roles.WORKER_ROLE) as conn:
            barrier.wait(timeout=5)
            return conn.execute(
                "SELECT * FROM queue_claim('execution',%s,300)", (TOKEN,)
            ).fetchone()

    def revoker():
        with connect_as(roles.OPERATOR_ROLE) as conn:
            barrier.wait(timeout=5)
            return conn.execute(
                "SELECT cto_revoke(%s,'concurrent perf revoke')", (current,)
            ).fetchone()[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        cf = pool.submit(claimant)
        rf = pool.submit(revoker)
        outcome = cf.result(timeout=10)
        assert rf.result(timeout=10)
    if outcome[0]:
        assert start(cto_rig, outcome[3])[0] is False
    artifact = {
        "denied_items": 2000,
        "out_of_scope_ready_items": 5000,
        "plan_observer": "test admin, explicit principal literal; real worker claim separately verified",
        "nodes": nodes,
        "execution_ms": plan[0]["Execution Time"],
        "unbound_execution_ms": unbound_plan[0]["Execution Time"],
        "unbound_rows_examined": examined,
        "claim_ms": claim_ms,
        "revoke_finished": True,
        "claim_won_race": outcome[0],
    }
    print("CTO_INDEXED_PLAN " + json.dumps(artifact, sort_keys=True))
    (tmp_path / "plan.json").write_text(json.dumps(artifact, indent=2))


def test_assignment_one_identity_replay_and_durable_conflict(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    first = enqueue(cto_rig)
    assert first and enqueue(cto_rig) == first
    assert enqueue(cto_rig, key="another-identity") is None
    assert cto_rig[3].execute("SELECT count(*) FROM work_item").fetchone()[0] == 1
    assert (
        cto_rig[3].execute("SELECT count(*) FROM cto_work_binding").fetchone()[0] == 1
    )
    assert (
        cto_rig[3]
        .execute(
            "SELECT count(*) FROM cto_assignment_event WHERE reason='binding_conflict'"
        )
        .fetchone()[0]
        == 1
    )


def test_assignment_concurrent_different_identities_create_one_work(
    cto_rig, connect_as
):
    assign(cto_rig)
    activate(cto_rig)
    barrier = Barrier(2)

    def submit(key):
        with connect_as(roles.APP_ROLE) as conn:
            barrier.wait(timeout=5)
            return conn.execute(
                "SELECT queue_enqueue('execution',%s,%s::jsonb,%s,'repo-test')",
                (key, json.dumps(PAYLOAD), TASK),
            ).fetchone()[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(submit, "identity-a")
        b = pool.submit(submit, "identity-b")
        results = [a.result(timeout=10), b.result(timeout=10)]
    assert sum(value is not None for value in results) == 1
    assert cto_rig[3].execute("SELECT count(*) FROM work_item").fetchone()[0] == 1
    assert (
        cto_rig[3].execute("SELECT count(*) FROM cto_work_binding").fetchone()[0] == 1
    )
    assert (
        cto_rig[3]
        .execute(
            "SELECT count(*) FROM cto_assignment_event WHERE reason='binding_conflict'"
        )
        .fetchone()[0]
        == 1
    )
