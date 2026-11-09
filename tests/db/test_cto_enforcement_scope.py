"""0037: activated CTO enforcement applies to enrolled work only and is reversible.

PostgreSQL-backed like the 0036 suite (skipped without AICC_TEST_PG_ADMIN_DSN).
The shared `cto_rig` enrolls TASK in the 0035 stage ledger; OTHER never is.
"""

from __future__ import annotations

import json

import pytest

from command_center.db import migrations, roles
from tests.db.test_cto_assignment import (
    PAYLOAD,
    TASK,
    activate,
    assign,
    claim,
    enqueue,
    start,
)

pytest_plugins = ("tests.db.test_cto_assignment",)
pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

OTHER = "VOYN-CTO-UNENROLLED"
REVIEW = {"kind": "agent_run", "task_type": "review", "prompt": "independent PR review"}


def deactivate(rig, reason="pilot paused by operator"):
    return rig[1].execute("SELECT cto_deactivate(%s)", (reason,)).fetchone()[0]


def enabled(rig):
    return rig[0].execute("SELECT enabled FROM cto_control").fetchone()[0]


def audit_count(rig, reason, action=None):
    sql = "SELECT count(*) FROM cto_assignment_event WHERE reason=%s"
    args = [reason]
    if action:
        sql += " AND action=%s"
        args.append(action)
    return rig[0].execute(sql, args).fetchone()[0]


def _other_task(app):
    assert app.execute(
        "SELECT * FROM backlog_upsert_task(%s,'1','P1','OPEN','task','other','bounded','repo-test')",
        (OTHER,),
    ).fetchone()[0]


def _role_payload(rig):
    revision, digest = rig[0].execute(
        "SELECT revision,input_digest FROM delivery_team_stage WHERE task_id=%s AND stage='ba'",
        (TASK,),
    ).fetchone()
    return {
        "kind": "agent_run",
        "task_type": "review",
        "untrusted": False,
        "delivery_team": {"task_id": TASK, "revision": revision, "stage": "ba", "input_digest": digest},
        "cascade": [{"executor": "claude", "task_type": "review"}],
    }


# --- non-enrolled work keeps its pre-activation behaviour --------------------


def test_unenrolled_review_and_retry_pass_after_activation(cto_rig):
    app = cto_rig[0]
    _other_task(app)
    activate(cto_rig)
    review = {**REVIEW, "backlog_task_id": OTHER}
    first = enqueue(cto_rig, key="pr-review", payload=review, task=OTHER)
    retry = enqueue(cto_rig, key="pr-review:retry:1", payload=review, task=OTHER)
    assert first and retry and first != retry
    # A taskless/other-kind item is not in scope either.
    assert enqueue(cto_rig, key="service", payload={"kind": "service"}, task=None)
    work = claim(cto_rig)
    assert work[0] and work[2] in (first, retry)
    assert start(cto_rig, work[3])[0]
    assert app.execute("SELECT count(*) FROM cto_work_binding").fetchone()[0] == 0
    assert audit_count(cto_rig, "assignment_required") == 0


def test_unenrolled_backlog_dispatch_and_coordinator_pass_after_activation(cto_rig):
    from tests.db.test_cto_assignment import _coordinator

    app = cto_rig[0]
    _other_task(app)
    activate(cto_rig)
    result = app.execute(
        "SELECT * FROM backlog_dispatch(%s,'planner',60,2,%s::jsonb,2)",
        (OTHER, json.dumps({"kind": "agent_run", "backlog_task_id": OTHER, "prompt": "legacy"})),
    ).fetchone()
    assert result[0] and result[2], result
    wid, failure = _coordinator(app)
    assert wid is not None and failure is None


def test_enrolled_unassigned_prefix_does_not_starve_unenrolled_work(cto_rig):
    for n in range(20):
        enqueue(cto_rig, key=f"enrolled-{n}")
    activate(cto_rig)
    wid = enqueue(cto_rig, key="pr-review", payload={**REVIEW, "backlog_task_id": OTHER}, task=OTHER)
    result = claim(cto_rig)
    assert result[0] and result[2] == wid
    assert not claim(cto_rig)[0]
    assert cto_rig[0].execute(
        "SELECT max(attempt_count) FROM work_item WHERE idempotency_key LIKE 'enrolled-%'"
    ).fetchone()[0] == 0


# --- enrolled work: exactly 0036's refusals -----------------------------------


def test_enrolled_role_item_with_null_queue_task_needs_assignment(cto_rig):
    activate(cto_rig)
    role = _role_payload(cto_rig)
    assert enqueue(cto_rig, key="role", payload=role, task=None) is None
    assert audit_count(cto_rig, "assignment_required", "enqueue") == 1
    assign(cto_rig, role)
    wid = enqueue(cto_rig, key="role", payload=role, task=None)
    assert wid
    work = claim(cto_rig)
    assert work[0] and work[2] == wid
    assert start(cto_rig, work[3])[0]


def test_enrolled_payload_with_valid_assignment_passes(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    wid = enqueue(cto_rig)
    work = claim(cto_rig)
    assert work[0] and work[2] == wid
    assert start(cto_rig, work[3])[0]


@pytest.mark.parametrize(
    "task,payload",
    [
        # Unenrolled queue task id, enrolled payload identity: ambiguous.
        (OTHER, {"kind": "agent_run", "backlog_task_id": TASK, "prompt": "bounded test"}),
        (OTHER, {"kind": "agent_run", "delivery_team": {"task_id": TASK}, "prompt": "bounded test"}),
        # Padded identities are still recognised as the enrolled task.
        (TASK + " ", {"kind": "agent_run", "prompt": "bounded test"}),
        (None, {"kind": "agent_run", "backlog_task_id": "\t" + TASK, "prompt": "bounded test"}),
        # Out-of-type kinds for an enrolled task stay refused.
        (TASK, {"kind": "service", "backlog_task_id": TASK}),
    ],
)
def test_enrolled_identity_cannot_be_laundered_onto_legacy_path(cto_rig, task, payload):
    assign(cto_rig)
    activate(cto_rig)
    assert enqueue(cto_rig, key="laundered", payload=payload, task=task) is None
    assert cto_rig[0].execute("SELECT count(*) FROM work_item").fetchone()[0] == 0
    assert audit_count(cto_rig, "assignment_required", "enqueue") == 1


def test_enrolled_digest_mismatch_is_refused(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    assert enqueue(cto_rig, payload={**PAYLOAD, "prompt": "changed after approval"}) is None


def test_enrolled_revoked_assignment_is_refused(cto_rig):
    aid = assign(cto_rig)
    cto_rig[1].execute("SELECT cto_revoke(%s,'operator revoked')", (aid,))
    activate(cto_rig)
    assert enqueue(cto_rig) is None


def test_enrolled_stale_revision_is_refused_at_claim(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)
    cto_rig[0].execute(
        "SELECT * FROM backlog_upsert_task(%s,'1','P1','OPEN','task','edited','bounded','repo-test')",
        (TASK,),
    )
    assert not claim(cto_rig)[0]
    assert audit_count(cto_rig, "stale_revision", "claim") >= 1


@pytest.mark.parametrize(
    "tamper", [{"prompt": "tampered"}, {"backlog_task_id": OTHER}]
)
def test_bound_work_never_falls_back_to_legacy_claim(cto_rig, tamper):
    """A binding keeps work in scope even if its payload stops naming the task."""
    assign(cto_rig)
    activate(cto_rig)
    wid = enqueue(cto_rig)
    cto_rig[3].execute(
        "UPDATE work_item SET payload=payload || %s::jsonb WHERE work_item_id=%s",
        (json.dumps(tamper), wid),
    )
    assert not claim(cto_rig)[0]
    assert cto_rig[0].execute(
        "SELECT attempt_count,state FROM work_item WHERE work_item_id=%s", (wid,)
    ).fetchone() == (0, "ready")
    assert audit_count(cto_rig, "binding_mismatch", "claim") == 1


# --- deactivation ---------------------------------------------------------------


def test_deactivation_restores_legacy_path_keeps_history_and_reactivates(cto_rig, psycopg):
    app, _, _, admin = cto_rig
    aid = assign(cto_rig)
    activate(cto_rig)
    unassigned = {**PAYLOAD, "prompt": "unassigned"}
    assert enqueue(cto_rig, key="unassigned", payload=unassigned) is None

    assert deactivate(cto_rig, "pilot paused: reviews blocked")
    assert not enabled(cto_rig)
    wid = enqueue(cto_rig, key="unassigned", payload=unassigned)
    assert wid
    work = claim(cto_rig)
    assert work[0] and work[2] == wid
    assert start(cto_rig, work[3])[0]
    # Nothing is deleted or revoked by deactivation.
    assert app.execute(
        "SELECT revoked_at FROM cto_assignment WHERE assignment_id=%s", (aid,)
    ).fetchone() == (None,)
    assert app.execute(
        "SELECT action,reference,actor FROM cto_control_event ORDER BY event_id"
    ).fetchall() == [
        ("activate", "all workers test-compatible", roles.OPERATOR_ROLE),
        ("deactivate", "pilot paused: reviews blocked", roles.OPERATOR_ROLE),
    ]
    assert audit_count(cto_rig, "granted", "deactivate") == 1
    # Idempotent when already inactive; no second history row.
    assert deactivate(cto_rig)
    assert app.execute("SELECT count(*) FROM cto_control_event").fetchone()[0] == 2

    activate(cto_rig)
    assert enabled(cto_rig)
    assert enqueue(cto_rig, key="after-reactivation", payload={**PAYLOAD, "prompt": "again"}) is None
    assert app.execute("SELECT count(*) FROM cto_control_event").fetchone()[0] == 3

    # The history is append-only even for the database owner.
    for statement in (
        "UPDATE cto_control_event SET reference='rewritten'",
        "DELETE FROM cto_control_event",
        "TRUNCATE cto_control_event",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            admin.execute(statement)


def test_deactivation_requires_operator_and_reason(cto_rig, psycopg):
    app, operator, worker, admin = cto_rig
    activate(cto_rig)
    for conn in (app, worker):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT cto_deactivate('forged')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO cto_control_event(action,reference,actor) VALUES('deactivate','x','x')")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE cto_control SET enabled=false")
    assert not admin.execute("SELECT cto_deactivate('admin masquerade')").fetchone()[0]
    admin.execute("SET ROLE aicc_operator")
    try:
        assert not admin.execute("SELECT cto_deactivate('set role masquerade')").fetchone()[0]
    finally:
        admin.execute("RESET ROLE")
    assert not operator.execute("SELECT cto_deactivate('   ')").fetchone()[0]
    assert enabled(cto_rig)
    assert audit_count(cto_rig, "operator_required", "deactivate") == 2
    assert app.execute(
        "SELECT count(*) FROM cto_control_event WHERE action='deactivate'"
    ).fetchone()[0] == 0


# --- migration path ---------------------------------------------------------------

CATALOG = (
    "SELECT 'function', p.oid::regprocedure::text, pg_get_functiondef(p.oid) || coalesce(p.proacl::text,'')"
    " FROM pg_proc p WHERE p.pronamespace='public'::regnamespace AND p.prokind='f'"
    " UNION ALL SELECT 'relation', c.relname, c.relkind::text || coalesce(c.relacl::text,'')"
    " FROM pg_class c WHERE c.relnamespace='public'::regnamespace"
    " UNION ALL SELECT 'trigger', t.tgname, t.tgrelid::regclass::text"
    " FROM pg_trigger t WHERE NOT t.tgisinternal ORDER BY 1,2"
)


def _migrate(connect_as, target=None):
    with connect_as(roles.MIGRATOR_ROLE) as conn:
        applied = migrations.upgrade(conn, target=target)
        roles.apply_table_grants(conn)
        return tuple(applied)


def test_upgrade_from_unactivated_36_then_clean_downgrade(admin_conn, connect_as):
    """The real production path: 0036 applied but never activated, then 0037."""
    roles.apply_bootstrap(admin_conn)
    _migrate(connect_as, target=36)
    baseline = admin_conn.execute(CATALOG).fetchall()
    admin_conn.execute("""INSERT INTO principal(principal_id,kind,db_role,display_name,host,state,trust_tier,created_at,updated_at)
      VALUES('worker:test','worker_host','aicc_worker','test','fixture','active',2,now(),now())""")
    with (
        connect_as(roles.APP_ROLE) as app,
        connect_as(roles.OPERATOR_ROLE) as operator,
        connect_as(roles.WORKER_ROLE) as worker,
    ):
        rig = (app, operator, worker, admin_conn)
        app.execute(
            "SELECT * FROM backlog_upsert_task(%s,'1','P1','OPEN','task','test','bounded','repo-test')",
            (TASK,),
        )
        app.execute("SELECT * FROM delivery_team_sync(100,%s::jsonb)", (json.dumps([TASK]),))
        aid = assign(rig)
        legacy = enqueue(rig, key="queued-before-upgrade", payload={**REVIEW, "backlog_task_id": OTHER}, task=OTHER)
        assert legacy

        assert _migrate(connect_as) == (37,)
        assert admin_conn.execute("SELECT max(version) FROM schema_migration").fetchone()[0] == 37
        assert not enabled(rig)
        assert app.execute("SELECT count(*) FROM cto_control_event").fetchone()[0] == 0
        assert app.execute(
            "SELECT revoked_at FROM cto_assignment WHERE assignment_id=%s", (aid,)
        ).fetchone() == (None,)
        # Disabled: identical legacy behaviour for queued work.
        work = claim(rig)
        assert work[0] and work[2] == legacy
        assert start(rig, work[3])[0]

        activate(rig)
        assert enqueue(rig, key="pr-review", payload={**REVIEW, "backlog_task_id": OTHER}, task=OTHER)
        assert enqueue(rig, key="enrolled-unassigned", payload={**PAYLOAD, "prompt": "x"}) is None
        assert enqueue(rig, key="enrolled-assigned")

        # Downgrade refuses while active, then restores exactly 0036 when off.
        with connect_as(roles.MIGRATOR_ROLE) as conn:
            with pytest.raises(Exception, match="forward migration"):
                migrations.downgrade(conn, target=36)
        assert enabled(rig)
        assert deactivate(rig)
        with connect_as(roles.MIGRATOR_ROLE) as conn:
            assert tuple(migrations.downgrade(conn, target=36)) == (37,)
            roles.apply_table_grants(conn)
    assert admin_conn.execute("SELECT max(version) FROM schema_migration").fetchone()[0] == 36
    assert admin_conn.execute(CATALOG).fetchall() == baseline
    # History created by 0037 is gone with its table; assignments survive.
    assert admin_conn.execute("SELECT to_regclass('cto_control_event')").fetchone()[0] is None
    assert admin_conn.execute("SELECT count(*) FROM cto_assignment").fetchone()[0] == 1


def test_claim_token_shape_is_checked_before_scope(cto_rig):
    """Unchanged 0036 front door: a malformed hash claims nothing in or out of scope."""
    activate(cto_rig)
    wid = enqueue(cto_rig, key="pr-review", payload={**REVIEW, "backlog_task_id": OTHER}, task=OTHER)
    denied = cto_rig[2].execute("SELECT * FROM queue_claim('execution',%s,300)", ("z" * 64,)).fetchone()
    assert not denied[0] and denied[1] == "bad_claim_token_hash"
    assert cto_rig[0].execute(
        "SELECT attempt_count FROM work_item WHERE work_item_id=%s", (wid,)
    ).fetchone()[0] == 0
