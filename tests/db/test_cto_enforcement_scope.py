"""0037: activated CTO enforcement applies to the operator's scope and is reversible.

PostgreSQL-backed like the 0036 suite: every test here is skipped without
AICC_TEST_PG_ADMIN_DSN. The shared `cto_rig` enrolls TASK and the operator puts
it in `cto_scope`; OTHER is never in scope. Payloads below are built by the
real producers (delivery_team.build_payload, planner._payload_for, the
review_merge cascade helpers) or copied field-for-field from review_merge.
"""

from __future__ import annotations

import json

import pytest

from command_center.db import migrations, roles
from command_center.orchestrator import delivery_team, review_merge
from command_center.orchestrator.planner import PlanLimits, _payload_for
from tests.db.test_cto_assignment import (
    PAYLOAD,
    TASK,
    activate,
    assign,
    claim,
    enqueue,
    start,
    try_activate,
)

pytest_plugins = ("tests.db.test_cto_assignment",)
pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

OTHER = "VOYN-CTO-UNSCOPED"
REPO = "repo-test"
ROUTE = ("AICC", "/srv/aicc")
HEAD = "b" * 40


def review_payload(task_id, *, chunk=None, cascade=None):
    """Field-for-field the payload review_merge._review_once enqueues."""
    cascade = cascade if cascade is not None else [
        {"executor": "claude", "task_type": "independent_review", "capability": "model_only"},
        {"executor": "codex", "task_type": "independent_review", "capability": "model_only"},
    ]
    payload = {
        "kind": "agent_run", "v": 1, "project_id": ROUTE[0],
        "repository_path": ROUTE[1],
        "task_type": "independent_review",
        "prompt": f"Independent review of {task_id} https://github.com/o/{REPO}/pull/7 at {HEAD}",
        "timeout_seconds": 900,
        "untrusted": True,
        "cascade": cascade,
        "review_head": {"pr_number": "7", "head_sha": HEAD},
    }
    if chunk is not None:
        payload["review_chunk"] = {
            "version": 3, "index": chunk, "count": 2, "content_bytes": 10,
            "content_hash": "c" * 64, "manifest_hash": "d" * 64,
            "base_sha": "a" * 40, "head_sha": HEAD, "diff_hash": "e" * 64,
        }
    return payload


def verification_payload(task_id):
    """Field-for-field review_merge's finding-verification payload."""
    return {
        "kind": "agent_run", "v": 1, "project_id": ROUTE[0],
        "repository_path": ROUTE[1],
        "task_type": "verification_review",
        "prompt": f"Verify the findings for {task_id}",
        "timeout_seconds": 900,
        "untrusted": True,
        "cascade": [{"executor": "claude", "task_type": "verification_review", "capability": "read_only"}],
        "review_head": {"pr_number": "7", "head_sha": HEAD},
    }


def audit_payload():
    """Field-for-field the taskless payload webapi/queue_routes.py enqueues."""
    return {
        "kind": "agent_run", "v": 1, "project_id": ROUTE[0],
        "repository_path": ROUTE[1], "prompt": "audit the repository",
        "task_type": "review", "untrusted": False, "timeout_seconds": 600,
    }


def raw_enqueue(rig, key, payload, task, repo=REPO):
    return rig[0].execute(
        "SELECT queue_enqueue('execution',%s,%s::jsonb,%s,%s)",
        (key, json.dumps(payload), task, repo),
    ).fetchone()[0]


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


def _other_task(app, enroll=False):
    assert app.execute(
        "SELECT * FROM backlog_upsert_task(%s,'1','P1','OPEN','task','other','bounded',%s)",
        (OTHER, REPO),
    ).fetchone()[0]
    if enroll:
        app.execute("SELECT * FROM delivery_team_sync(100,%s::jsonb)", (json.dumps([OTHER]),))


def _candidate(app, task=TASK):
    return next(
        row[0] for row in app.execute("SELECT * FROM delivery_team_candidates(100)").fetchall()
        if row[0]["task_id"] == task
    )


# --- out of scope: pre-activation admission ---------------------------------------


def test_unscoped_review_chunks_and_retry_pass_after_activation(cto_rig):
    app = cto_rig[0]
    _other_task(app)
    activate(cto_rig)
    chunk = review_payload(OTHER, chunk=0)
    retry = review_payload(OTHER, chunk=0, cascade=review_merge._failover_cascade(
        chunk["cascade"], "claude"))
    first = raw_enqueue(cto_rig, "review:OTHER:chunk0", chunk, OTHER)
    again = raw_enqueue(cto_rig, "review:OTHER:chunk0:retry:1", retry, OTHER)
    # The production review writer used to send repository_id NULL; out of
    # scope that is still admitted exactly as before activation.
    legacy_null = raw_enqueue(cto_rig, "review:OTHER:chunk1", review_payload(OTHER, chunk=1), OTHER, None)
    assert first and again and legacy_null
    work = claim(cto_rig)
    assert work[0] and work[2] in (first, again, legacy_null)
    assert start(cto_rig, work[3])[0]
    assert app.execute("SELECT count(*) FROM cto_work_binding").fetchone()[0] == 0
    assert audit_count(cto_rig, "assignment_required") == 0


def test_enrollment_growth_alone_does_not_extend_enforcement(cto_rig):
    """F4: the app role enrolls OTHER; only the operator's scope is enforced."""
    app = cto_rig[0]
    _other_task(app, enroll=True)
    assert app.execute(
        "SELECT count(*) FROM delivery_team_stage WHERE task_id=%s", (OTHER,)
    ).fetchone()[0] > 0
    activate(cto_rig)
    role = delivery_team.build_payload(_candidate(app, OTHER), ROUTE)
    candidate = _candidate(app, OTHER)
    result = app.execute(
        "SELECT * FROM delivery_team_dispatch(%s,%s,%s,%s,%s::jsonb,2)",
        (OTHER, candidate["revision"], candidate["stage"], candidate["input_digest"], json.dumps(role)),
    ).fetchone()
    assert result[0] and result[2], result
    assert app.execute("SELECT count(*) FROM cto_scope WHERE task_id=%s", (OTHER,)).fetchone()[0] == 0


def test_unscoped_backlog_dispatch_and_coordinator_pass_after_activation(cto_rig):
    from tests.db.test_cto_assignment import _coordinator

    app = cto_rig[0]
    _other_task(app)
    activate(cto_rig)
    task = {"task_id": OTHER, "title": "other", "body": "bounded", "wave": "1", "priority": "P1"}
    payload, budget = _payload_for(task, PlanLimits(), ROUTE)
    result = app.execute(
        "SELECT * FROM backlog_dispatch(%s,'planner',60,2,%s::jsonb,%s)",
        (OTHER, json.dumps(payload), budget),
    ).fetchone()
    assert result[0] and result[2], result
    wid, failure = _coordinator(app)
    assert wid is not None and failure is None


def test_taskless_read_only_work_passes_and_taskless_writing_work_is_refused(cto_rig):
    """F3: work naming no task is out of scope only if it cannot write."""
    activate(cto_rig)
    assert raw_enqueue(cto_rig, "audit-1", audit_payload(), None, None)
    writing = {**audit_payload(), "task_type": "implementation", "untrusted": False}
    laundered = {**audit_payload(), "cascade": [{"executor": "codex", "task_type": "implementation"}]}
    untyped = {k: v for k, v in audit_payload().items() if k != "task_type"}
    simple = {"kind": "simple_task", "prompt": "write a file"}
    for key, payload in (("w1", writing), ("w2", laundered), ("w3", untyped), ("w4", simple)):
        assert raw_enqueue(cto_rig, key, payload, None) is None, key
    assert cto_rig[0].execute("SELECT count(*) FROM work_item").fetchone()[0] == 1
    assert audit_count(cto_rig, "assignment_required", "enqueue") == 4


# --- in scope: every pilot work kind, real shapes ----------------------------------


def test_scoped_review_with_null_repository_is_refused_not_matched(cto_rig):
    """F1: the old review writer's NULL repository can never match a binding."""
    payload = review_payload(TASK)
    assign(cto_rig, payload)
    activate(cto_rig)
    assert raw_enqueue(cto_rig, "review:TASK", payload, TASK, None) is None
    assert audit_count(cto_rig, "repository_unresolved", "enqueue") == 1
    assert cto_rig[0].execute("SELECT count(*) FROM work_item").fetchone()[0] == 0


def test_scoped_review_with_repository_and_exact_assignment_is_admitted(cto_rig):
    payload = review_payload(TASK)
    assign(cto_rig, payload)
    activate(cto_rig)
    wid = raw_enqueue(cto_rig, "review:TASK", payload, TASK)
    assert wid
    work = claim(cto_rig)
    assert work[0] and work[2] == wid
    assert start(cto_rig, work[3])[0]


@pytest.mark.parametrize("kind", ["chunk", "retry", "verification"])
def test_scoped_review_variants_each_need_their_own_assignment(cto_rig, kind):
    """Each chunk, :retry:N and verification is a distinct exact payload."""
    base = review_payload(TASK, chunk=0)
    assign(cto_rig, base)
    activate(cto_rig)
    variant = {
        "chunk": review_payload(TASK, chunk=1),
        "retry": review_payload(TASK, chunk=0, cascade=review_merge._failover_cascade(
            base["cascade"], "claude")),
        "verification": verification_payload(TASK),
    }[kind]
    assert raw_enqueue(cto_rig, f"review:{kind}", variant, TASK) is None
    assign(cto_rig, variant)
    assert raw_enqueue(cto_rig, f"review:{kind}", variant, TASK)


def test_scoped_role_stage_from_real_dispatcher_payload(cto_rig):
    """Role items carry a NULL queue task_id; delivery_team.task_id scopes them."""
    app = cto_rig[0]
    candidate = _candidate(app)
    role = delivery_team.build_payload(candidate, ROUTE)
    activate(cto_rig)
    args = (TASK, candidate["revision"], candidate["stage"], candidate["input_digest"], json.dumps(role))
    sql = "SELECT * FROM delivery_team_dispatch(%s,%s,%s,%s,%s::jsonb,2)"
    assert app.execute(sql, args).fetchone() == (False, "cto_enqueue_refused", None)
    assign(cto_rig, role)
    ok, reason, wid = app.execute(sql, args).fetchone()
    assert ok and reason == "queued" and wid
    assert app.execute("SELECT task_id,repository_id FROM work_item WHERE work_item_id=%s",
                       (wid,)).fetchone() == (None, REPO)
    work = claim(cto_rig)
    assert work[0] and work[2] == wid
    assert start(cto_rig, work[3])[0]


def test_scoped_implementation_from_real_planner_payload(cto_rig):
    app = cto_rig[0]
    task = {"task_id": TASK, "title": "test", "body": "bounded", "wave": "1", "priority": "P1"}
    payload, budget = _payload_for(task, PlanLimits(), ROUTE)
    activate(cto_rig)
    sql = "SELECT * FROM backlog_dispatch(%s,'planner',60,2,%s::jsonb,%s)"
    refused = app.execute(sql, (TASK, json.dumps(payload), budget)).fetchone()
    assert not refused[0] and refused[1] == "cto_assignment_required"
    # The implementation gate (0035) still applies before any CTO admission.
    assign(cto_rig, payload)
    with pytest.raises(Exception, match="delivery_team_requirements_not_ready"):
        app.execute(sql, (TASK, json.dumps(payload), budget))


@pytest.mark.parametrize(
    "task,payload",
    [
        (OTHER, {"kind": "agent_run", "backlog_task_id": TASK, "prompt": "bounded test"}),
        (OTHER, {"kind": "agent_run", "delivery_team": {"task_id": TASK}, "prompt": "bounded test"}),
        (TASK + " ", {"kind": "agent_run", "prompt": "bounded test"}),
        (None, {"kind": "agent_run", "backlog_task_id": "\t" + TASK, "prompt": "bounded test"}),
        (TASK.lower(), {"kind": "agent_run", "prompt": "bounded test"}),
        (TASK, {"kind": "service", "backlog_task_id": TASK}),
    ],
)
def test_scoped_identity_cannot_be_laundered_onto_legacy_path(cto_rig, task, payload):
    assign(cto_rig)
    activate(cto_rig)
    assert raw_enqueue(cto_rig, "laundered", payload, task) is None
    assert cto_rig[0].execute("SELECT count(*) FROM work_item").fetchone()[0] == 0
    assert audit_count(cto_rig, "assignment_required", "enqueue") == 1


def test_scoped_digest_mismatch_is_refused(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    assert enqueue(cto_rig, payload={**PAYLOAD, "prompt": "changed after approval"}) is None


def test_scoped_revoked_assignment_is_refused(cto_rig):
    aid = assign(cto_rig)
    cto_rig[1].execute("SELECT cto_revoke(%s,'operator revoked')", (aid,))
    activate(cto_rig)
    assert enqueue(cto_rig) is None


def test_scoped_stale_revision_is_refused_at_claim(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    enqueue(cto_rig)
    cto_rig[0].execute(
        "SELECT * FROM backlog_upsert_task(%s,'1','P1','OPEN','task','edited','bounded','repo-test')",
        (TASK,),
    )
    assert not claim(cto_rig)[0]
    assert audit_count(cto_rig, "stale_revision", "claim") >= 1


@pytest.mark.parametrize("tamper", [{"prompt": "tampered"}, {"backlog_task_id": OTHER}])
def test_bound_work_never_falls_back_to_legacy_claim(cto_rig, tamper):
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


# --- operator scope control -----------------------------------------------------


def test_scope_is_operator_only_requires_enrollment_and_is_audited(cto_rig, psycopg):
    app, operator, worker, admin = cto_rig
    _other_task(app)
    for conn in (app, worker):
        for sql in ("SELECT cto_scope_add('X','r')", "SELECT cto_scope_remove('X','r')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("INSERT INTO cto_scope(task_id,reference,added_by) VALUES('X','r','x')")
    assert not admin.execute("SELECT cto_scope_add(%s,'superuser')", (OTHER,)).fetchone()[0]
    # Not enrolled in the 0035 ledger: refused.
    assert not operator.execute("SELECT cto_scope_add(%s,'pilot')", (OTHER,)).fetchone()[0]
    assert audit_count(cto_rig, "not_enrolled", "scope_add") == 1
    app.execute("SELECT * FROM delivery_team_sync(100,%s::jsonb)", (json.dumps([OTHER]),))
    assert not operator.execute("SELECT cto_scope_add(%s,'  ')", (OTHER,)).fetchone()[0]
    assert operator.execute("SELECT cto_scope_add(%s,'pilot 2')", (OTHER,)).fetchone()[0]
    assert operator.execute("SELECT cto_scope_add(%s,'pilot 2 again')", (OTHER,)).fetchone()[0]
    assert operator.execute("SELECT cto_scope_remove(%s,'pilot 2 done')", (OTHER,)).fetchone()[0]
    assert app.execute(
        "SELECT action,task_id,reference,actor FROM cto_control_event WHERE task_id=%s ORDER BY event_id",
        (OTHER,),
    ).fetchall() == [
        ("scope_add", OTHER, "pilot 2", roles.OPERATOR_ROLE),
        ("scope_remove", OTHER, "pilot 2 done", roles.OPERATOR_ROLE),
    ]


def test_scope_remove_releases_unbound_work_but_bound_work_stays_enforced(cto_rig):
    aid = assign(cto_rig)
    activate(cto_rig)
    bound = enqueue(cto_rig, key="bound")
    assert cto_rig[1].execute("SELECT cto_scope_remove(%s,'pilot paused')", (TASK,)).fetchone()[0]
    free = enqueue(cto_rig, key="free", payload={**PAYLOAD, "prompt": "unassigned"})
    assert free
    cto_rig[1].execute("SELECT cto_revoke(%s,'revoked after scope removal')", (aid,))
    work = claim(cto_rig)
    assert work[0] and work[2] == free and work[2] != bound
    assert not claim(cto_rig)[0]


def test_scope_add_while_active_refuses_unbound_work(cto_rig):
    app, operator = cto_rig[0], cto_rig[1]
    _other_task(app, enroll=True)
    activate(cto_rig)
    assert raw_enqueue(cto_rig, "other-1", {**PAYLOAD, "backlog_task_id": OTHER}, OTHER)
    assert not operator.execute("SELECT cto_scope_add(%s,'too late')", (OTHER,)).fetchone()[0]
    assert audit_count(cto_rig, "unbound_scoped_work", "scope_add") == 1


# --- activation, deactivation, redrive --------------------------------------------


def test_activation_waits_for_unbound_scoped_work_and_redrive_cannot_revive_it(cto_rig):
    """Low case: scoped work queued before activation is refused, never stranded."""
    wid = enqueue(cto_rig, key="before-activation")
    assert not try_activate(cto_rig)
    assert audit_count(cto_rig, "unbound_scoped_work", "activate") == 1
    work = claim(cto_rig)  # disabled: legacy claim
    assert work[0] and work[2] == wid
    assert cto_rig[2].execute(
        "SELECT (queue_fail(%s,%s,'abandon before activation',false)).ok",
        (work[3], "disposable claim fixture"),
    ).fetchone()[0]
    activate(cto_rig)
    assert not cto_rig[0].execute("SELECT queue_redrive(%s,1)", (wid,)).fetchone()[0]
    assert audit_count(cto_rig, "assignment_required", "redrive") == 1
    assert cto_rig[0].execute("SELECT state FROM work_item WHERE work_item_id=%s", (wid,)).fetchone()[0] == "dead"


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
    # Reactivation refuses while that unassigned scoped work is outstanding.
    assert not try_activate(cto_rig)
    work = claim(cto_rig)
    assert work[0] and work[2] == wid
    assert start(cto_rig, work[3])[0]
    assert app.execute(
        "SELECT revoked_at FROM cto_assignment WHERE assignment_id=%s", (aid,)
    ).fetchone() == (None,)
    assert audit_count(cto_rig, "granted", "deactivate") == 1
    assert deactivate(cto_rig)  # idempotent while off, no second row
    assert app.execute(
        "SELECT count(*) FROM cto_control_event WHERE action IN ('activate','deactivate')"
    ).fetchone()[0] == 2
    for statement in (
        "UPDATE cto_control_event SET reference='rewritten'",
        "DELETE FROM cto_control_event",
        "TRUNCATE cto_control_event",
    ):
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            admin.execute(statement)


def test_reactivation_after_draining_restores_enforcement(cto_rig):
    assign(cto_rig)
    activate(cto_rig)
    assert deactivate(cto_rig)
    activate(cto_rig)
    assert enabled(cto_rig)
    assert enqueue(cto_rig, key="after", payload={**PAYLOAD, "prompt": "again"}) is None
    assert cto_rig[0].execute(
        "SELECT array_agg(action ORDER BY event_id) FROM cto_control_event WHERE task_id IS NULL"
    ).fetchone()[0] == ["activate", "deactivate", "activate"]


def test_switch_requires_operator_and_reason(cto_rig, psycopg):
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
    assert audit_count(cto_rig, "reason_required", "deactivate") == 1


def test_claim_token_shape_is_checked_before_scope(cto_rig):
    app = cto_rig[0]
    _other_task(app)
    activate(cto_rig)
    wid = raw_enqueue(cto_rig, "review", review_payload(OTHER), OTHER)
    denied = cto_rig[2].execute("SELECT * FROM queue_claim('execution',%s,300)", ("z" * 64,)).fetchone()
    assert not denied[0] and denied[1] == "bad_claim_token_hash"
    assert app.execute("SELECT attempt_count FROM work_item WHERE work_item_id=%s", (wid,)).fetchone()[0] == 0
    assert claim(cto_rig)[0]


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


def _downgrade(connect_as):
    with connect_as(roles.MIGRATOR_ROLE) as conn:
        reverted = migrations.downgrade(conn, target=36)
        roles.apply_table_grants(conn)
        return tuple(reverted)


def test_upgrade_from_unactivated_36_and_guarded_downgrade(admin_conn, connect_as):
    """Release path: #1110 at 36, never activated, then 0037 as a plain upgrade."""
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
        queued = raw_enqueue(rig, "queued-before-upgrade", review_payload(OTHER), OTHER, None)
        assert queued

        assert _migrate(connect_as) == (37,)
        assert not enabled(rig)
        assert app.execute("SELECT count(*) FROM cto_scope").fetchone()[0] == 0
        assert app.execute(
            "SELECT revoked_at FROM cto_assignment WHERE assignment_id=%s", (aid,)
        ).fetchone() == (None,)
        # A never-used 37 downgrades to exactly the 36 catalog, then re-upgrades.
        assert _downgrade(connect_as) == (37,)
        assert admin_conn.execute(CATALOG).fetchall() == baseline
        assert _migrate(connect_as) == (37,)

        work = claim(rig)  # disabled: identical legacy behaviour
        assert work[0] and work[2] == queued
        assert start(rig, work[3])[0]
        assert operator.execute("SELECT cto_scope_add(%s,'pilot')", (TASK,)).fetchone()[0]
        activate(rig)
        assert enqueue(rig, key="enrolled-assigned")
        assert enqueue(rig, key="enrolled-unassigned", payload={**PAYLOAD, "prompt": "x"}) is None
        assert raw_enqueue(rig, "review-other", review_payload(OTHER), OTHER, None)

        with pytest.raises(Exception, match="forward migration"):
            _downgrade(connect_as)
        assert deactivate(rig)
        # Switch history exists: downgrade still refuses rather than drop it.
        with pytest.raises(Exception, match="switch history"):
            _downgrade(connect_as)
        assert app.execute("SELECT count(*) FROM cto_control_event").fetchone()[0] == 3
    assert admin_conn.execute("SELECT max(version) FROM schema_migration").fetchone()[0] == 37
