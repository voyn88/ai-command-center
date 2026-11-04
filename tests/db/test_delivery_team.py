"""Native delivery-team authority on disposable PostgreSQL, under real grants.

No tests here fabricate stage PASS with direct table writes. Role outputs travel
through a real worker claim/complete and the control-plane ingest validator.
The parent harness must supply AICC_TEST_PG_ADMIN_DSN; skipped is not PASS.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from command_center.db import roles
from command_center.db.work_queue_store import ClaimedWork
from tests.db.test_backlog_planner import _task

pytest_plugins = ("tests.db.test_backlog_planner",)

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]
TASK = "VOYN-W0-TEAM-TEST"
SHA = "a" * 40
CRITERIA = [{"id": "C1", "description": "The original user outcome is observable"}]


def rows(factory, query, params=()):
    with factory() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return cur.fetchall()


def enroll(rig, task_id=TASK, **kwargs):
    factory, store, _queue = rig
    assert store.upsert_task(_task(task_id, repo="repo-one", **kwargs))[0]
    rows(factory, "SELECT * FROM delivery_team_sync(100)")


def candidate(factory, stage=None, task_id=TASK):
    found = [r[0] for r in rows(factory, "SELECT * FROM delivery_team_candidates(100)")]
    return next(
        c
        for c in found
        if c["task_id"] == task_id and (stage is None or c["stage"] == stage)
    )


def payload(c):
    value = {
        "kind": "agent_run",
        "v": 1,
        "task_type": "review",
        "untrusted": False,
        "cascade": [{"executor": "codex", "task_type": "review"}],
        "delivery_team": {
            k: c[k] for k in ("task_id", "revision", "stage", "input_digest")
        },
    }
    if c.get("context", {}).get("source_sha"):
        value["delivery_team_source_sha"] = c["context"]["source_sha"]
    return value


def dispatch(factory, c, body=None):
    return rows(
        factory,
        "SELECT * FROM delivery_team_dispatch(%s,%s,%s,%s,%s::jsonb,2)",
        (
            c["task_id"],
            c["revision"],
            c["stage"],
            c["input_digest"],
            json.dumps(body or payload(c)),
        ),
    )[0]


def result(c, *, sha=None, reference="source:original-requirement", environment=None):
    evidence = {
        "criterion_id": "C1",
        "reference": reference,
        "sha": sha,
        "verified": True,
    }
    if environment:
        evidence["environment"] = environment
    return {
        "schema": "delivery-team-review/v1",
        **payload(c)["delivery_team"],
        "verdict": "pass",
        "criteria": CRITERIA,
        "evidence": [evidence],
        "blockers": [],
        "summary": "Reviewed original requirement and cited evidence",
    }


def complete_stage(rig, c, output=None):
    factory, _store, queue = rig
    ok, reason, work_id = dispatch(factory, c)
    assert ok, reason
    work = queue.claim("execution")
    assert isinstance(work, ClaimedWork) and work.work_item_id == work_id
    assert queue.complete(
        work,
        {
            "delivery_team_result": output if output is not None else result(c),
            "observed_source_sha": c.get("context", {}).get("source_sha"),
        },
    )
    return rows(factory, "SELECT * FROM delivery_team_ingest(100)")


def preflight(rig):
    factory, _store, _queue = rig
    for stage in (
        "ba",
        "business_technologist",
        "architect",
        "qa_plan",
        "security",
        "ux",
        "cto",
    ):
        rows(factory, "SELECT * FROM delivery_team_sync(100)")
        c = candidate(factory, stage)
        assert complete_stage(rig, c) == [(TASK, stage, "pass")]
    assert rows(factory, "SELECT delivery_team_ready(%s,1)", (TASK,)) == [(True,)]


def test_enrollment_does_not_relabel_legacy_done_as_accepted(rig):  # noqa: F811
    factory, store, _queue = rig
    enroll(rig, status="DONE")
    assert candidate(factory)["stage"] == "ba"
    assert store.get_task(TASK)["status"] == "DONE"
    assert rows(
        factory,
        "SELECT delivery_team_ready(%s,1),delivery_team_accepted(%s,1)",
        (TASK, TASK),
    ) == [(False, False)]


def test_dispatch_is_atomic_idempotent_and_not_legacy_backlog_execution(rig):  # noqa: F811
    factory, store, _queue = rig
    enroll(rig)
    c = candidate(factory)
    first = dispatch(factory, c)
    second = dispatch(factory, c)
    assert first[0] and second == (True, "already_queued", first[2])
    assert rows(
        factory,
        "SELECT task_id,max_attempts FROM work_item WHERE work_item_id=%s",
        (first[2],),
    ) == [(None, 2)]
    assert store.get_task(TASK)["status"] == "OPEN"
    assert rows(factory, "SELECT count(*) FROM delivery_team_evidence") == [(0,)]


def test_atomic_global_capacity_under_concurrent_dispatch(rig):  # noqa: F811
    factory, _store, _queue = rig
    for suffix in ("A", "B", "C"):
        enroll(rig, TASK + suffix)
    candidates = [
        r[0] for r in rows(factory, "SELECT * FROM delivery_team_candidates(100)")
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        verdicts = list(pool.map(lambda c: dispatch(factory, c), candidates))
    assert sum(v[0] for v in verdicts) == 2
    assert [v[1] for v in verdicts if not v[0]] == ["capacity"]
    assert rows(factory, "SELECT count(*) FROM work_item WHERE state='ready'") == [(2,)]


def test_transaction_rollback_does_not_leave_half_dispatch(rig):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig)
    c = candidate(factory)
    with factory() as conn:
        with pytest.raises(RuntimeError), conn.transaction(), conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM delivery_team_dispatch(%s,1,'ba',%s,%s::jsonb,2)",
                (TASK, c["input_digest"], json.dumps(payload(c))),
            )
            assert cur.fetchone()[0]
            raise RuntimeError("caller rolled back")
    assert rows(factory, "SELECT count(*) FROM work_item") == [(0,)]
    assert candidate(factory)["stage"] == "ba"


@pytest.mark.parametrize(
    "mutation",
    [
        "empty_evidence",
        "wrong_revision",
        "wrong_digest",
        "extra_key",
        "empty_criteria",
        "string_verified",
        "pass_with_blocker",
    ],
)
def test_malformed_or_unproven_output_cannot_pass(rig, mutation):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig)
    c = candidate(factory)
    output = result(c)
    if mutation == "empty_evidence":
        output["evidence"] = []
    elif mutation == "wrong_revision":
        output["revision"] = "1"
    elif mutation == "wrong_digest":
        output["input_digest"] = "f" * 64
    elif mutation == "extra_key":
        output["approved"] = True
    elif mutation == "empty_criteria":
        output["criteria"] = []
    elif mutation == "string_verified":
        output["evidence"][0]["verified"] = "true"
    elif mutation == "pass_with_blocker":
        output["blockers"] = ["No original criterion"]
    complete_stage(rig, c, output)
    assert rows(
        factory,
        "SELECT status FROM delivery_team_stage WHERE task_id=%s AND stage='ba'",
        (TASK,),
    ) == [("blocked",)]
    assert rows(factory, "SELECT delivery_team_ready(%s,1)", (TASK,)) == [(False,)]
    # A real tick synchronizes before ingesting. The newly blocked BA changes
    # the auditor's incident input; its old digest must stay undispatchable
    # until the next tick synchronizes that input, rather than bypass the gate.
    assert rows(
        factory,
        "SELECT delivery_team_stage_current(%s,1,'auditor')",
        (TASK,),
    ) == [(False,)]
    assert rows(factory, "SELECT * FROM delivery_team_candidates(100)") == []
    auditor_digest = rows(
        factory,
        "SELECT input_digest FROM delivery_team_stage "
        "WHERE task_id=%s AND revision=1 AND stage='auditor'",
        (TASK,),
    )[0][0]
    stale_auditor = dict(c, stage="auditor", input_digest=auditor_digest)
    assert dispatch(factory, stale_auditor)[1] == "stale_digest"

    # The next tick must expose exactly the auditor, preserve the refusal,
    # and not ingest the already recorded result for a second time.
    rows(factory, "SELECT * FROM delivery_team_sync(100)")
    assert rows(factory, "SELECT * FROM delivery_team_ingest(100)") == []
    assert {
        r[0]["stage"]
        for r in rows(factory, "SELECT * FROM delivery_team_candidates(100)")
    } == {"auditor"}
    auditor = candidate(factory, "auditor")
    assert auditor["input_digest"] != auditor_digest
    assert [i["stage"] for i in auditor["context"]["incidents"]] == ["ba"]
    assert rows(
        factory,
        "SELECT status FROM delivery_team_stage WHERE task_id=%s AND stage='ba'",
        (TASK,),
    ) == [("blocked",)]
    assert rows(factory, "SELECT count(*) FROM delivery_team_evidence") == [(1,)]
    assert dispatch(factory, auditor)[0]


def test_stale_requirements_refuse_dispatch_and_completed_output(rig):  # noqa: F811
    factory, store, queue = rig
    enroll(rig)
    c = candidate(factory)
    assert dispatch(factory, c)[0]
    work = queue.claim("execution")
    assert isinstance(work, ClaimedWork)
    assert store.upsert_task(
        _task(TASK, repo="repo-one", body="Changed business outcome")
    )[0]
    assert dispatch(factory, c)[1] == "stale_revision"
    assert queue.complete(work, {"delivery_team_result": result(c)})
    assert rows(factory, "SELECT * FROM delivery_team_ingest(100)") == [
        (TASK, "ba", "stale_input")
    ]
    rows(factory, "SELECT * FROM delivery_team_sync(100)")
    assert candidate(factory)["revision"] == 2
    assert rows(factory, "SELECT delivery_team_ready(%s,2)", (TASK,)) == [(False,)]


def test_unchanged_requirements_survive_status_revision_but_changed_body_does_not(rig):  # noqa: F811
    factory, store, _queue = rig
    enroll(rig)
    preflight(rig)
    assert store.transition(TASK, "IN_PROGRESS", 1)[0]
    rows(factory, "SELECT * FROM delivery_team_sync(100)")
    assert rows(factory, "SELECT delivery_team_ready(%s,2)", (TASK,)) == [(True,)]
    assert rows(factory, "SELECT count(*) FROM delivery_team_evidence") == [(7,)]
    assert store.upsert_task(_task(TASK, repo="repo-one", body="Different outcome"))[0]
    rows(factory, "SELECT * FROM delivery_team_sync(100)")
    assert rows(factory, "SELECT delivery_team_ready(%s,3)", (TASK,)) == [(False,)]


def test_native_mutating_dispatch_gate_cannot_be_bypassed_and_readonly_is_allowed(
    rig, psycopg
):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig)
    with pytest.raises(
        psycopg.errors.CheckViolation, match="delivery_team_requirements_not_ready"
    ):
        rows(
            factory,
            "SELECT queue_enqueue('execution','bad',%s::jsonb,%s)",
            (json.dumps({"kind": "agent_run", "task_type": "implementation"}), TASK),
        )
    assert rows(factory, "SELECT count(*) FROM work_item") == [(0,)]
    assert rows(
        factory,
        "SELECT queue_enqueue('execution','review',%s::jsonb,%s)",
        (json.dumps({"kind": "agent_run", "task_type": "review"}), TASK),
    )[0][0]


def test_retry_budget_is_bounded_and_ingest_is_append_only(rig, psycopg):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig)
    c = candidate(factory)
    output = result(c)
    output["verdict"] = "blocked"
    output["blockers"] = ["Missing owner decision"]
    for attempt in range(2):
        complete_stage(rig, c, output)
        assert rows(factory, "SELECT * FROM delivery_team_ingest(100)") == []
        retried = rows(
            factory,
            "SELECT delivery_team_retry(%s,1,'ba',%s)",
            (TASK, c["input_digest"]),
        )[0][0]
        assert retried is (attempt == 0)
    assert rows(factory, "SELECT count(*) FROM delivery_team_evidence") == [(2,)]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        rows(factory, "UPDATE delivery_team_evidence SET verdict='pass'")


def test_worker_cannot_operate_team_control_plane(rig, psycopg):  # noqa: F811
    factory, _store, queue = rig
    enroll(rig)
    for query in (
        "SELECT delivery_team_sync(1)",
        "SELECT delivery_team_ready('x',1)",
        "SELECT * FROM delivery_team_stage",
        "UPDATE delivery_team_stage SET status='passed'",
    ):
        with (
            queue._connection() as conn,
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            conn.cursor() as cur,
        ):
            cur.execute(query)
    assert rows(
        factory,
        "SELECT has_table_privilege(%s,'delivery_team_stage','UPDATE')",
        (roles.APP_ROLE,),
    ) == [(False,)]


ACCEPTANCE_REF = "https://checks.example/run/1#sha256=" + "c" * 64
ACCEPTANCE_SQL = "SELECT delivery_team_record_acceptance(%s,1,%s,%s,%s,%s,%s)"


def record_acceptance(operator, factory):
    observed = rows(factory, "SELECT clock_timestamp()")[0][0]
    assert rows(operator, ACCEPTANCE_SQL, (TASK, SHA, "C1", "business-check-1", ACCEPTANCE_REF, observed)) == [(True,)]
    return observed


def test_production_acceptance_needs_trusted_matching_receipt_and_original_criteria(
    rig, connect_as, psycopg,
):  # noqa: F811
    factory, store, queue = rig
    def operator():
        return connect_as(roles.OPERATOR_ROLE)

    enroll(rig)
    preflight(rig)
    assert not rows(factory, "SELECT * FROM delivery_team_candidates(100)")
    assert store.record_evidence(TASK, "sha", SHA)[0]
    assert store.record_evidence(TASK, "pr", "https://github.com/example/repo/pull/1")[
        0
    ]
    for stage in ("independent_reviewer", "devops"):
        rows(factory, "SELECT * FROM delivery_team_sync(100)")
        c = candidate(factory, stage)
        assert complete_stage(rig, c, result(c, sha=SHA)) == [(TASK, stage, "pass")]
    rows(factory, "SELECT * FROM delivery_team_sync(100)")
    assert not rows(factory, "SELECT * FROM delivery_team_candidates(100)")
    receipt_sql = "SELECT delivery_team_record_release(%s,1,%s,%s,%s::jsonb,%s)"
    for untrusted in (factory, queue._connection):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rows(untrusted, receipt_sql, (TASK, SHA, "production", '["C1"]', "receipt:1"))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rows(untrusted, "SELECT delivery_team_release_scope(%s,1)", (TASK,))
    assert rows(operator, "SELECT delivery_team_release_scope(%s,1)", (TASK,)) == [
        ({"repo": "repo-one", "source_sha": SHA, "criteria": ["C1"]},)
    ]
    assert rows(operator, "SELECT delivery_team_release_scope(%s,2)", (TASK,)) == [(None,)]
    assert rows(
        operator, receipt_sql, (TASK, "b" * 40, "production", '["C1"]', "receipt:1")
    ) == [(False,)]
    assert rows(
        operator, receipt_sql, (TASK, SHA, "staging", '["C1"]', "receipt:1")
    ) == [(False,)]
    assert rows(
        operator, receipt_sql, (TASK, SHA, "production", '["C2"]', "receipt:1")
    ) == [(False,)]
    assert rows(
        operator, receipt_sql, (TASK, SHA, "production", '["C1"]', "receipt:1")
    ) == [(True,)]
    rows(factory, "SELECT * FROM delivery_team_sync(100)")
    c = candidate(factory, "production_qa")
    first_receipt = c["context"]["release_receipt"]
    assert rows(
        operator, receipt_sql, (TASK, SHA, "production", '["C1"]', "receipt:later-observation")
    ) == [(True,)]
    rows(factory, "SELECT * FROM delivery_team_sync(100)")
    assert candidate(factory, "production_qa") == c
    assert rows(factory, "SELECT count(*) FROM delivery_team_release_receipt") == [(1,)]
    # A deployment/health receipt is never sufficient business acceptance.
    assert complete_stage(rig, c, result(c, sha=SHA, reference="receipt:1", environment="production")) == [
        (TASK, "production_qa", "unproven_production_criteria")
    ]
    assert rows(factory, "SELECT delivery_team_accepted(%s,1)", (TASK,)) == [(False,)]
    record_acceptance(operator, factory)
    rows(factory, "SELECT * FROM delivery_team_sync(100)")
    c = candidate(factory, "production_qa")
    wrong = result(c, sha=SHA, reference=ACCEPTANCE_REF, environment="production")
    wrong["criteria"] = [{"id": "C1", "description": "Silently weakened criterion"}]
    assert complete_stage(rig, c, wrong) == [
        (TASK, "production_qa", "criterion_contract_mismatch")
    ]
    assert rows(factory, "SELECT delivery_team_accepted(%s,1)", (TASK,)) == [(False,)]
    assert rows(
        factory,
        "SELECT delivery_team_retry(%s,1,'production_qa',%s)",
        (TASK, c["input_digest"]),
    ) == [(True,)]
    assert complete_stage(
        rig, c, result(c, sha=SHA, reference=ACCEPTANCE_REF, environment="production")
    ) == [(TASK, "production_qa", "pass")]
    assert rows(factory, "SELECT delivery_team_accepted(%s,1)", (TASK,)) == [(True,)]
    assert rows(
        operator, receipt_sql, (TASK, SHA, "production", '["C1"]', "receipt:post-acceptance")
    ) == [(True,)]
    rows(factory, "SELECT * FROM delivery_team_sync(100)")
    assert rows(factory, "SELECT to_jsonb(r) FROM delivery_team_release_receipt r") == [(first_receipt,)]
    assert rows(factory, "SELECT delivery_team_accepted(%s,1)", (TASK,)) == [(True,)]
    assert store.record_evidence(TASK, "sha", "b" * 40)[0]
    assert rows(factory, "SELECT delivery_team_accepted(%s,1)", (TASK,)) == [(False,)]


def test_scoped_pilot_enrolls_only_selected_task(rig):  # noqa: F811
    factory, store, _queue = rig
    assert store.upsert_task(_task(TASK, repo="repo-one"))[0]
    assert store.upsert_task(_task(TASK + "-OTHER", repo="repo-one"))[0]
    rows(
        factory,
        "SELECT * FROM delivery_team_sync(100,%s::jsonb)",
        (json.dumps([TASK]),),
    )
    assert rows(factory, "SELECT DISTINCT task_id FROM delivery_team_stage") == [
        (TASK,)
    ]
    assert (
        rows(factory, "SELECT * FROM delivery_team_candidates(100,'[]'::jsonb)") == []
    )
    assert (
        len(
            rows(
                factory,
                "SELECT * FROM delivery_team_candidates(100,%s::jsonb)",
                (json.dumps([TASK]),),
            )
        )
        == 1
    )


@pytest.mark.parametrize(
    "unsafe",
    [
        "mutating_type",
        "mutating_cascade",
        "paid_executor",
        "backlog_id",
        "wrong_metadata",
    ],
)
def test_unsafe_role_payload_cannot_reach_queue(rig, unsafe):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig)
    c = candidate(factory)
    body = payload(c)
    if unsafe == "mutating_type":
        body["task_type"] = "implementation"
    elif unsafe == "mutating_cascade":
        body["cascade"][0]["task_type"] = "implementation"
    elif unsafe == "paid_executor":
        body["cascade"][0]["executor"] = "unapproved-provider"
    elif unsafe == "backlog_id":
        body["backlog_task_id"] = TASK
    elif unsafe == "wrong_metadata":
        body["delivery_team"]["input_digest"] = "0" * 64
    assert dispatch(factory, c, body)[1] == "unsafe_payload"
    assert rows(factory, "SELECT count(*) FROM work_item") == [(0,)]


INCIDENT_SQL = "SELECT delivery_team_record_incident(%s,%s,%s,%s,%s,%s,%s,%s::jsonb)"
CORRECTION = "VOYN-W0-TEAM-CORRECTION"


def incident_args(task_id=TASK, revision=1, run_ref="https://ci.example/runs/17"):
    from command_center.orchestrator.delivery_team import incident_key

    signature = "CI failed: проверка"
    evidence = {"kind": "ci_failure", "reference": run_ref,
                "observed_at": "2026-11-04T12:00:00+00:00", "detail": "Assertion failed"}
    return (task_id, revision, incident_key("repo-one", run_ref, SHA, signature),
            "repo-one", run_ref, SHA, signature, json.dumps(evidence))


def sync_task(factory):
    return rows(factory, "SELECT * FROM delivery_team_sync(100,%s::jsonb)", (json.dumps([TASK]),))


def rca_result(c):
    value = result(c, sha=SHA)
    value["rca"] = [{
        "incident_key": c["context"]["incidents"][0]["incident_key"],
        "facts": ["The recorded CI run failed"], "hypotheses": [],
        "root_cause": "Fixture uses an obsolete contract",
        "missed_control": "Contract fixture was not updated",
        "actions": [{"task_id": CORRECTION, "owner": "fixture-owner",
                     "action": "Update fixture and verify the original invariant",
                     "effectiveness_criterion_id": "C1"}],
    }]
    return value


def test_real_incident_is_bound_idempotent_append_only_and_worker_cannot_inject(rig, psycopg):  # noqa: F811
    factory, _store, queue = rig
    enroll(rig)
    args = incident_args()
    assert rows(factory, INCIDENT_SQL, args) == [(True,)]
    assert rows(factory, INCIDENT_SQL, args) == [(True,)]
    assert rows(factory, "SELECT count(*) FROM delivery_team_incident") == [(1,)]
    for index, replacement in ((1, 2), (2, "b" * 64), (3, "foreign-repo"), (5, "b" * 40)):
        wrong = list(args)
        wrong[index] = replacement
        assert rows(factory, INCIDENT_SQL, wrong) == [(False,)]
    changed_evidence = list(args)
    changed_evidence[-1] = json.dumps({**json.loads(args[-1]), "detail": "Changed observation"})
    assert rows(factory, INCIDENT_SQL, changed_evidence) == [(False,)]
    for query, params in ((INCIDENT_SQL, args), ("SELECT * FROM delivery_team_incident", ())):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rows(queue._connection, query, params)
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        rows(factory, "UPDATE delivery_team_incident SET signature='rewritten'")
    sync_task(factory)
    c = candidate(factory, "auditor")
    assert c["context"]["incidents"][0]["incident_key"] == args[2]
    assert c["context"]["incidents"][0]["evidence"] == json.loads(args[-1])


def test_generic_auditor_pass_cannot_close_real_incident(rig):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig)
    assert rows(factory, INCIDENT_SQL, incident_args()) == [(True,)]
    sync_task(factory)
    c = candidate(factory, "auditor")
    assert complete_stage(rig, c) == [(TASK, "auditor", "invalid_schema")]
    assert rows(factory, "SELECT status FROM delivery_team_stage WHERE task_id=%s AND stage='auditor'", (TASK,)) == [("blocked",)]


def test_cto_rca_requires_unchanged_audit_and_recorded_corrective_evidence(rig, connect_as):  # noqa: F811
    factory, store, _queue = rig
    enroll(rig)
    assert store.upsert_task(_task(CORRECTION, repo="repo-one"))[0]
    assert rows(factory, INCIDENT_SQL, incident_args()) == [(True,)]
    sync_task(factory)
    audit = candidate(factory, "auditor")
    output = rca_result(audit)
    assert store.record_remediation(CORRECTION, TASK, "https://github.com/example/repo/pull/1", SHA)[0]
    assert rows(lambda: connect_as(roles.OPERATOR_ROLE),
                "SELECT delivery_team_assign_correction(%s,1,%s,%s::jsonb,%s)",
                (TASK, output["rca"][0]["incident_key"], json.dumps(output["rca"][0]["actions"][0]),
                 "https://control.example/assignments/1")) == [(True,)]
    sync_task(factory)
    audit = candidate(factory, "auditor")
    output = rca_result(audit)
    assert complete_stage(rig, audit, output) == [(TASK, "auditor", "pass")]
    sync_task(factory)
    cto = candidate(factory, "cto_rca")
    closure = result(cto, sha=SHA, reference="https://ci.example/correction/18")
    closure["rca"] = output["rca"]
    assert complete_stage(rig, cto, closure) == [(TASK, "cto_rca", "corrective_task_not_done")]
    assert store.upsert_task(_task(CORRECTION, repo="repo-one", status="DONE"))[0]
    assert store.record_evidence(CORRECTION, "sha", SHA)[0]
    # Corrective state is also digest-bound; fresh status/SHA requires a fresh
    # CTO assessment, and a model claim still cannot replace recorded CI proof.
    assert rows(factory, "SELECT delivery_team_stage_current(%s,1,'cto_rca')", (TASK,)) == [(False,)]
    sync_task(factory)
    cto = candidate(factory, "cto_rca")
    closure = result(cto, sha=SHA, reference="https://ci.example/correction/18")
    closure["rca"] = output["rca"]
    assert complete_stage(rig, cto, closure) == [(TASK, "cto_rca", "unproven_corrective_effectiveness")]
    assert rows(factory, "SELECT status FROM delivery_team_stage WHERE task_id=%s AND stage='cto_rca'", (TASK,)) == [("blocked",)]


def test_cto_rca_pass_has_same_actions_and_independent_correction_evidence(rig, connect_as):  # noqa: F811
    factory, store, _queue = rig
    enroll(rig)
    assert store.upsert_task(_task(CORRECTION, repo="repo-one", status="DONE"))[0]
    assert store.record_evidence(CORRECTION, "sha", SHA)[0]
    reference = "https://ci.example/correction/18#sha256=" + "d"*64
    assert store.record_evidence(CORRECTION, "ci", reference)[0]
    assert rows(factory, INCIDENT_SQL, incident_args()) == [(True,)]
    sync_task(factory)
    audit = candidate(factory, "auditor")
    output = rca_result(audit)
    assert store.record_remediation(CORRECTION, TASK, "https://github.com/example/repo/pull/1", SHA)[0]
    assert rows(lambda: connect_as(roles.OPERATOR_ROLE),
                "SELECT delivery_team_assign_correction(%s,1,%s,%s::jsonb,%s)",
                (TASK, output["rca"][0]["incident_key"], json.dumps(output["rca"][0]["actions"][0]),
                 "https://control.example/assignments/1")) == [(True,)]
    sync_task(factory)
    audit = candidate(factory, "auditor")
    output = rca_result(audit)
    assert complete_stage(rig, audit, output) == [(TASK, "auditor", "pass")]
    sync_task(factory)
    cto = candidate(factory, "cto_rca")
    closure = result(cto, sha=SHA, reference=reference)
    closure["rca"] = json.loads(json.dumps(output["rca"]))
    closure["rca"][0]["actions"][0]["owner"] = "silently-reassigned"
    assert complete_stage(rig, cto, closure) == [(TASK, "cto_rca", "unassigned_corrective_action")]
    assert rows(factory, "SELECT delivery_team_retry(%s,1,'cto_rca',%s)", (TASK, cto["input_digest"])) == [(True,)]
    closure["rca"] = output["rca"]
    assert complete_stage(rig, cto, closure) == [(TASK, "cto_rca", "unproven_corrective_effectiveness")]
    assert rows(lambda: connect_as(roles.OPERATOR_ROLE),
                "SELECT delivery_team_record_corrective_check(%s,1,%s,%s,1,%s,'C1','effectiveness-check',%s,clock_timestamp())",
                (TASK, output["rca"][0]["incident_key"], CORRECTION, SHA, reference)) == [(True,)]
    sync_task(factory)
    cto = candidate(factory, "cto_rca")
    closure = result(cto, sha=SHA, reference=reference)
    closure["rca"] = output["rca"]
    assert complete_stage(rig, cto, closure) == [(TASK, "cto_rca", "pass")]
    # A new correction revision cannot reuse an older effectiveness observation,
    # even if it remains DONE with the same source SHA.
    changed = _task(CORRECTION, repo="repo-one", status="DONE",
                    body="Changed corrective scope requires new effectiveness check")
    assert store.upsert_task(changed)[0]
    sync_task(factory)
    cto = candidate(factory, "cto_rca")
    stale = result(cto, sha=SHA, reference=reference)
    stale["rca"] = output["rca"]
    assert complete_stage(rig, cto, stale) == [(TASK, "cto_rca", "unproven_corrective_effectiveness")]
    # A new actual incident invalidates prior audit/closure; it is not silently covered.
    later = list(incident_args())
    from command_center.orchestrator.delivery_team import incident_key
    later[4] = "https://ci.example/runs/19"
    later[2] = incident_key(later[3], later[4], later[5], later[6])
    later[-1] = json.dumps({**json.loads(later[-1]), "reference": later[4]})
    assert rows(factory, INCIDENT_SQL, later) == [(True,)]
    assert rows(factory, "SELECT delivery_team_stage_current(%s,1,'cto_rca')", (TASK,)) == [(False,)]
    sync_task(factory)
    assert candidate(factory, "auditor")["stage"] == "auditor"


def test_real_incident_survives_status_revision_and_replay_is_not_duplicate(rig):  # noqa: F811
    factory, store, _queue = rig
    enroll(rig)
    args = incident_args()
    assert rows(factory, INCIDENT_SQL, args) == [(True,)]
    assert store.transition(TASK, "IN_PROGRESS", 1)[0]
    replay = list(args)
    replay[1] = 2
    assert rows(factory, INCIDENT_SQL, replay) == [(True,)]
    sync_task(factory)
    auditor = candidate(factory, "auditor")
    assert auditor["revision"] == 2
    assert len(auditor["context"]["incidents"]) == 1
    assert auditor["context"]["incidents"][0]["recorded_revision"] == 1
    assert rows(factory, "SELECT count(*) FROM delivery_team_incident") == [(1,)]


POST_CODE_STAGES = ("independent_reviewer", "devops", "production_qa")


def prepare_post_code_candidate(rig, connect_as, target_stage):
    """Reach the requested stage through the same grants and ledger as a tick."""
    factory, store, _queue = rig
    enroll(rig)
    preflight(rig)
    assert store.record_evidence(TASK, "sha", SHA)[0]
    assert store.record_evidence(TASK, "pr", "https://github.com/example/repo/pull/1")[0]
    for stage in POST_CODE_STAGES:
        if stage == "production_qa":
            assert rows(
                lambda: connect_as(roles.OPERATOR_ROLE),
                "SELECT delivery_team_record_release(%s,1,%s,'production',%s::jsonb,%s)",
                (TASK, SHA, '["C1"]', "receipt:source-pin"),
            ) == [(True,)]
            record_acceptance(lambda: connect_as(roles.OPERATOR_ROLE), factory)
        rows(factory, "SELECT * FROM delivery_team_sync(100)")
        c = candidate(factory, stage)
        if stage == target_stage:
            return c
        assert complete_stage(rig, c, result(c, sha=SHA)) == [(TASK, stage, "pass")]
    raise AssertionError(f"Unknown post-code stage: {target_stage}")


def pinned_result(c):
    if c["stage"] == "production_qa":
        return result(c, sha=SHA, reference=ACCEPTANCE_REF, environment="production")
    return result(c, sha=SHA)


@pytest.mark.parametrize("stage", POST_CODE_STAGES)
@pytest.mark.parametrize("bad_pin", [None, "b" * 40], ids=["missing", "wrong"])
def test_post_code_dispatch_requires_exact_source_pin(rig, connect_as, stage, bad_pin):  # noqa: F811
    factory, _store, _queue = rig
    c = prepare_post_code_candidate(rig, connect_as, stage)
    body = payload(c)
    if bad_pin is None:
        del body["delivery_team_source_sha"]
    else:
        body["delivery_team_source_sha"] = bad_pin
    queue_before = rows(factory, "SELECT count(*) FROM work_item")
    assert dispatch(factory, c, body) == (False, "source_sha_mismatch", None)
    assert rows(factory, "SELECT count(*) FROM work_item") == queue_before
    assert rows(
        factory,
        "SELECT status,dispatch_count,work_item_id FROM delivery_team_stage "
        "WHERE task_id=%s AND revision=1 AND stage=%s",
        (TASK, stage),
    ) == [("pending", 0, None)]
    # Refusal must leave the valid, exact-pin assignment usable.
    assert complete_stage(rig, c, pinned_result(c)) == [(TASK, stage, "pass")]
    assert rows(factory, "SELECT * FROM delivery_team_ingest(100)") == []


@pytest.mark.parametrize("stage", POST_CODE_STAGES)
@pytest.mark.parametrize("observed", [None, "b" * 40], ids=["missing", "wrong"])
def test_post_code_agent_claim_cannot_replace_worker_observed_sha(
    rig, connect_as, stage, observed,
):  # noqa: F811
    factory, _store, queue = rig
    c = prepare_post_code_candidate(rig, connect_as, stage)
    ok, reason, work_id = dispatch(factory, c)
    assert ok, reason
    work = queue.claim("execution")
    assert isinstance(work, ClaimedWork) and work.work_item_id == work_id
    # Model output claims the right SHA and otherwise satisfies all contracts.
    body = {"delivery_team_result": pinned_result(c)}
    if observed is not None:
        body["observed_source_sha"] = observed
    assert queue.complete(work, body)
    assert rows(factory, "SELECT * FROM delivery_team_ingest(100)") == [
        (TASK, stage, "observed_source_sha_mismatch")
    ]
    assert rows(
        factory,
        "SELECT status,reason FROM delivery_team_stage WHERE task_id=%s AND revision=1 AND stage=%s",
        (TASK, stage),
    ) == [("blocked", "observed_source_sha_mismatch")]
    assert rows(factory, "SELECT delivery_team_accepted(%s,1)", (TASK,)) == [(False,)]
    # Re-ingestion cannot turn the invalid original evidence into approval.
    assert rows(factory, "SELECT * FROM delivery_team_ingest(100)") == []
    assert rows(
        factory,
        "SELECT verdict FROM delivery_team_evidence WHERE work_item_id=%s",
        (work_id,),
    ) == [("invalid",)]
    assert rows(
        factory,
        "SELECT delivery_team_retry(%s,1,%s,%s)",
        (TASK, stage, c["input_digest"]),
    ) == [(True,)]
    assert complete_stage(rig, c, pinned_result(c)) == [(TASK, stage, "pass")]
    assert rows(
        factory,
        "SELECT verdict FROM delivery_team_evidence WHERE work_item_id=%s",
        (work_id,),
    ) == [("invalid",)]


def test_acceptance_attestation_is_operator_only_scoped_and_immutable(rig, connect_as, psycopg):  # noqa: F811
    factory, _store, queue = rig
    c = prepare_post_code_candidate(rig, connect_as, "production_qa")
    def operator():
        return connect_as(roles.OPERATOR_ROLE)
    observed = c["context"]["acceptance_checks"][0]["observed_at"]
    args = (TASK, SHA, "C1", "business-check-1", ACCEPTANCE_REF, observed)
    for untrusted in (factory, queue._connection):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rows(untrusted, ACCEPTANCE_SQL, args)
    assert rows(operator, ACCEPTANCE_SQL, args) == [(True,)]
    for index, replacement in ((0, TASK+"-OTHER"), (1, "b"*40), (2, "unknown"),
                               (3, "another-check"), (4, "receipt:source-pin"),
                               (5, "2000-01-01T00:00:00Z"), (5, "2999-01-01T00:00:00Z")):
        wrong = list(args)
        wrong[index] = replacement
        assert rows(operator, ACCEPTANCE_SQL, wrong) == [(False,)]
    assert rows(factory, "SELECT count(*) FROM delivery_team_acceptance_check") == [(1,)]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        rows(factory, "UPDATE delivery_team_acceptance_check SET check_id='forged'")
    forged = result(c, sha=SHA, reference="https://forged.example/check#sha256="+"d"*64, environment="production")
    assert complete_stage(rig, c, forged) == [(TASK, "production_qa", "unproven_production_criteria")]
    assert rows(factory, "SELECT delivery_team_accepted(%s,1)", (TASK,)) == [(False,)]


@pytest.mark.parametrize("linked", [False, True])
def test_auditor_cannot_use_unrelated_task_or_invent_owner(rig, linked):  # noqa: F811
    factory, store, _queue = rig
    enroll(rig)
    assert store.upsert_task(_task(CORRECTION, repo="repo-one", status="DONE"))[0]
    assert store.record_evidence(CORRECTION, "sha", SHA)[0]
    assert store.record_evidence(CORRECTION, "ci", "https://ci.example/green")[0]
    if linked:
        assert store.record_remediation(CORRECTION, TASK, "https://github.com/example/repo/pull/1", SHA)[0]
    assert rows(factory, INCIDENT_SQL, incident_args()) == [(True,)]
    sync_task(factory)
    audit = candidate(factory, "auditor")
    assert complete_stage(rig, audit, rca_result(audit)) == [
        # The fixture parent is P0: the CTO policy leaves that to the operator.
        (TASK, "auditor", "unassigned_corrective_action:operator_confirmation_required"
         if linked else "unlinked_corrective_task")
    ]


def test_reobserved_incident_retains_first_observation(rig):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig)
    original = incident_args()
    assert rows(factory, INCIDENT_SQL, original) == [(True,)]
    later = list(original)
    later[-1] = json.dumps({**json.loads(original[-1]), "observed_at": "2026-11-04T15:00:00Z"})
    assert rows(factory, INCIDENT_SQL, later) == [(True,)]
    assert rows(factory, "SELECT evidence FROM delivery_team_incident") == [(json.loads(original[-1]),)]


def test_gate_failure_incident_survives_retry_and_success(rig):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig)
    c = candidate(factory, "ba")
    output = result(c)
    output["verdict"] = "blocked"
    output["blockers"] = ["Original source not available"]
    complete_stage(rig, c, output)
    sync_task(factory)
    incident = candidate(factory, "auditor")["context"]["incidents"]
    assert len(incident) == 1
    assert rows(factory, "SELECT delivery_team_retry(%s,1,'ba',%s)", (TASK, c["input_digest"])) == [(True,)]
    sync_task(factory)
    assert candidate(factory, "auditor")["context"]["incidents"] == incident
    c = candidate(factory, "ba")
    assert complete_stage(rig, c) == [(TASK, "ba", "pass")]
    sync_task(factory)
    assert candidate(factory, "auditor")["context"]["incidents"] == incident


def test_acceptance_before_receipt_observation_is_rejected_in_long_transaction(rig, connect_as):  # noqa: F811
    factory, store, _queue = rig
    enroll(rig)
    preflight(rig)
    assert store.record_evidence(TASK, "sha", SHA)[0]
    with connect_as(roles.OPERATOR_ROLE) as conn, conn.cursor() as cur:
        cur.execute("SELECT clock_timestamp()")
        before_receipt = cur.fetchone()[0]
        cur.execute("SELECT delivery_team_record_release(%s,1,%s,'production','[\"C1\"]'::jsonb,'receipt:timing')", (TASK, SHA))
        assert cur.fetchone() == (True,)
        cur.execute(ACCEPTANCE_SQL, (TASK, SHA, "C1", "business-check-1", ACCEPTANCE_REF, before_receipt))
        assert cur.fetchone() == (False,)


def test_one_correction_can_cover_two_related_incidents(rig, connect_as, psycopg):  # noqa: F811
    factory, store, queue = rig
    enroll(rig)
    assert store.upsert_task(_task(CORRECTION, repo="repo-one"))[0]
    assert store.record_remediation(CORRECTION, TASK, "https://github.com/example/repo/pull/1", SHA)[0]
    first = incident_args()
    second = incident_args(run_ref="https://ci.example/runs/second")
    for args in (first, second):
        assert rows(factory, INCIDENT_SQL, args) == [(True,)]
    sync_task(factory)
    c = candidate(factory, "auditor")
    value = rca_result(c)
    value["rca"] = [dict(value["rca"][0], incident_key=i["incident_key"]) for i in c["context"]["incidents"]]
    assign_sql = "SELECT delivery_team_assign_correction(%s,1,%s,%s::jsonb,%s)"
    for item in value["rca"]:
        args = (TASK, item["incident_key"], json.dumps(item["actions"][0]), "https://control.example/assignment/1")
        for untrusted in (factory, queue._connection):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                rows(untrusted, assign_sql, args)
        assert rows(lambda: connect_as(roles.OPERATOR_ROLE), assign_sql, args) == [(True,)]
    sync_task(factory)
    c = candidate(factory, "auditor")
    value.update({k:c[k] for k in ("revision","input_digest")})
    assert complete_stage(rig, c, value) == [(TASK, "auditor", "pass")]


# --- 0036: CTO corrective-assignment policy, due dates, optional roles ------

OTHER = "VOYN-W0-TEAM-OTHER"
ASSIGNMENTS = ("SELECT task_id,assignment->>'owner',recorded_by,source_work_item_id IS NOT NULL,"
               "due_at-recorded_at FROM delivery_team_corrective_assignment ORDER BY task_id")


def policy_rig(rig, priority="P2", incidents=1):
    factory, store, _queue = rig
    enroll(rig, priority=priority)
    assert store.upsert_task(_task(CORRECTION, repo="repo-one"))[0]
    assert store.record_remediation(CORRECTION, TASK, "https://github.com/example/repo/pull/1", SHA)[0]
    for n in range(incidents):
        assert rows(factory, INCIDENT_SQL, incident_args(run_ref=f"https://ci.example/runs/{17 + n}")) == [(True,)]
    sync_task(factory)
    return candidate(factory, "auditor")


def team_rca(c, owner="developer"):
    value = rca_result(c)
    value["rca"][0]["actions"][0]["owner"] = owner
    return value


def test_cto_policy_assigns_canonical_role_with_due_date_and_keeps_audit_current(rig):  # noqa: F811
    from datetime import timedelta
    factory, _store, _queue = rig
    audit = policy_rig(rig)
    assert complete_stage(rig, audit, team_rca(audit)) == [(TASK, "auditor", "pass")]
    assert rows(factory, ASSIGNMENTS) == [
        (CORRECTION, "developer", "policy:delivery-team-cto/v1", True, timedelta(days=5))]
    # The assignment is the audit's output, not its input: the audit stays current.
    sync_task(factory)
    assert rows(factory, "SELECT delivery_team_stage_current(%s,1,'auditor')", (TASK,)) == [(True,)]
    cto = candidate(factory, "cto_rca")
    assert [a["recorded_by"] for a in cto["context"]["corrective_assignments"]] == ["policy:delivery-team-cto/v1"]
    # CTO-RCA closure still needs the independent effectiveness check.
    closure = result(cto, sha=SHA, reference="https://ci.example/correction/18")
    closure["rca"] = team_rca(audit)["rca"]
    assert complete_stage(rig, cto, closure) == [(TASK, "cto_rca", "corrective_task_not_done")]


@pytest.mark.parametrize("owner", ["fixture-owner", "auditor", "CTO"])
def test_cto_policy_refuses_owner_outside_team_roles(rig, owner):  # noqa: F811
    factory, _store, _queue = rig
    audit = policy_rig(rig)
    assert complete_stage(rig, audit, team_rca(audit, owner)) == [
        (TASK, "auditor", "unassigned_corrective_action:owner_not_team_role")]
    assert rows(factory, "SELECT count(*) FROM delivery_team_corrective_assignment") == [(0,)]


def test_cto_policy_batch_is_all_or_nothing(rig):  # noqa: F811
    factory, store, _queue = rig
    audit = policy_rig(rig)
    assert store.upsert_task(_task(OTHER, repo="repo-one"))[0]
    value = team_rca(audit)
    value["rca"][0]["actions"].append(dict(value["rca"][0]["actions"][0], task_id=OTHER))
    assert complete_stage(rig, audit, value) == [
        (TASK, "auditor", "unassigned_corrective_action:unlinked_corrective_task")]
    assert rows(factory, "SELECT count(*) FROM delivery_team_corrective_assignment") == [(0,)]


def test_cto_policy_rolls_back_when_revalidation_fails(rig):  # noqa: F811
    factory, _store, _queue = rig
    audit = policy_rig(rig, incidents=2)
    value = team_rca(audit)  # covers only the first of two incidents
    assert complete_stage(rig, audit, value) == [(TASK, "auditor", "incomplete_incident_coverage")]
    assert rows(factory, "SELECT count(*) FROM delivery_team_corrective_assignment") == [(0,)]
    assert rows(factory, "SELECT status FROM delivery_team_stage WHERE task_id=%s AND stage='auditor'",
                (TASK,)) == [("blocked",)]


def test_cto_policy_never_overrides_operator_assignment(rig, connect_as):  # noqa: F811
    from datetime import timedelta
    factory, _store, _queue = rig
    audit = policy_rig(rig)
    action = team_rca(audit, "developer")["rca"][0]["actions"][0]
    assert rows(lambda: connect_as(roles.OPERATOR_ROLE),
                "SELECT delivery_team_assign_correction(%s,1,%s,%s::jsonb,%s)",
                (TASK, audit["context"]["incidents"][0]["incident_key"], json.dumps(action),
                 "https://control.example/assignments/1")) == [(True,)]
    sync_task(factory)
    audit = candidate(factory, "auditor")
    assert complete_stage(rig, audit, team_rca(audit, "devops")) == [
        (TASK, "auditor", "unassigned_corrective_action:assignment_conflict")]
    owner = rows(factory, ASSIGNMENTS)
    assert [(r[1], r[2] == "policy:delivery-team-cto/v1", r[4]) for r in owner] == [
        ("developer", False, timedelta(days=5))]


def test_cto_policy_is_not_an_app_or_worker_entry_point(rig, psycopg):  # noqa: F811
    factory, _store, queue = rig
    for conn in (factory, queue._connection):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            rows(conn, "SELECT _delivery_team_cto_assign(%s,1,'{}'::jsonb,'x')", (TASK,))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        rows(factory, "INSERT INTO delivery_team_due_policy VALUES ('P3','1 hour')")


def run_until(rig, stop, architect=None):
    factory, _store, _queue = rig
    for stage in ("ba", "business_technologist", "architect", "qa_plan"):
        rows(factory, "SELECT * FROM delivery_team_sync(100)")
        c = candidate(factory, stage)
        output = result(c)
        if stage == "architect" and architect is not None:
            output["role_applicability"] = architect
        verdict = complete_stage(rig, c, output)
        if stage == stop:
            return verdict
        assert verdict == [(TASK, stage, "pass")]
    rows(factory, "SELECT * FROM delivery_team_sync(100)")
    return [r[0]["stage"] for r in rows(factory, "SELECT * FROM delivery_team_candidates(100)")]


NOT_APPLICABLE = {"required": False, "reason": "Internal read-only report without users or secrets"}


def test_architect_waiver_skips_optional_roles_for_low_risk_task(rig):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig, priority="P2")
    assert run_until(rig, None, {"security": NOT_APPLICABLE, "ux": NOT_APPLICABLE}) == ["cto"]
    assert rows(factory, "SELECT delivery_team_stage_waived(%s,1,'security'),"
                "delivery_team_stage_waived(%s,1,'ux'),delivery_team_ready(%s,1)", (TASK, TASK, TASK)) == [
        (True, True, False)]
    assert complete_stage(rig, candidate(factory, "cto")) == [(TASK, "cto", "pass")]
    assert rows(factory, "SELECT delivery_team_ready(%s,1)", (TASK,)) == [(True,)]


def test_waiver_is_per_role_and_absent_means_required(rig):  # noqa: F811
    enroll(rig, priority="P2")
    assert run_until(rig, None, {"security": NOT_APPLICABLE}) == ["ux"]


@pytest.mark.parametrize("priority", ["P0", "P1"])
def test_waiver_is_refused_for_high_priority(rig, priority):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig, priority=priority)
    assert run_until(rig, None, {"security": NOT_APPLICABLE, "ux": NOT_APPLICABLE}) == ["security"]
    assert rows(factory, "SELECT delivery_team_ready(%s,1)", (TASK,)) == [(False,)]


@pytest.mark.parametrize("value", [
    {}, [], {"legal": NOT_APPLICABLE}, {"security": {"required": False}},
    {"security": {"required": "no", "reason": "x"}}, {"security": {"required": False, "reason": " "}},
    {"security": dict(NOT_APPLICABLE, extra=True)},
])
def test_invalid_role_applicability_blocks_architect(rig, value):  # noqa: F811
    enroll(rig, priority="P2")
    assert run_until(rig, "architect", value) == [(TASK, "architect", "invalid_role_applicability")]


def test_role_applicability_is_architect_only(rig):  # noqa: F811
    factory, _store, _queue = rig
    enroll(rig, priority="P2")
    c = candidate(factory, "ba")
    output = dict(result(c), role_applicability={"security": NOT_APPLICABLE})
    assert complete_stage(rig, c, output) == [(TASK, "ba", "invalid_schema")]


def test_owner_read_model_runs_under_app_grants(rig, monkeypatch):  # noqa: F811
    from contextlib import contextmanager
    from command_center.api import delivery_team_routes as team_routes
    factory, _store, _queue = rig
    audit = policy_rig(rig)
    assert complete_stage(rig, audit, team_rca(audit)) == [(TASK, "auditor", "pass")]

    @contextmanager
    def app_connection():
        with factory() as conn:
            yield conn
    monkeypatch.setattr(team_routes.pool, "connection", app_connection)
    data = team_routes.read_team()
    assert data["available"] is True
    task = next(t for t in data["tasks"] if t["id"] == TASK)
    assert [(c["task_id"], c["owner"], c["assigned_by"], c["overdue"]) for c in task["corrections"]] == [
        (CORRECTION, "developer", "cto_policy", False)]
    assert task["corrections"][0]["due_at"]


def overdue_rig(rig, test_dsn, psycopg, sla="1 millisecond"):
    import time
    with psycopg.connect(test_dsn, autocommit=True) as admin:
        admin.execute("UPDATE delivery_team_due_policy SET sla=%s::interval WHERE priority='P2'", (sla,))
    audit = policy_rig(rig)
    assert complete_stage(rig, audit, team_rca(audit)) == [(TASK, "auditor", "pass")]
    time.sleep(0.01)
    return audit


def test_overdue_correction_escalates_once_to_cto_backlog_task(rig, test_dsn, psycopg):  # noqa: F811
    from command_center.orchestrator.delivery_overdue import escalate_overdue, escalation_task_id
    factory, store, _queue = rig
    audit = overdue_rig(rig, test_dsn, psycopg)
    incident = audit["context"]["incidents"][0]["incident_key"]
    expected = escalation_task_id(TASK, incident, CORRECTION)
    report = escalate_overdue(factory)
    assert report.escalated == [(CORRECTION, expected)] and report.refused == []
    filed = store.get_task(expected)
    assert filed["status"] == "OPEN" and filed["priority"] == "P2" and filed["repo"] == "repo-one"
    body = rows(factory, "SELECT body FROM backlog_task WHERE task_id=%s", (expected,))[0][0]
    assert "escalated to the CTO" in body and "developer" in body
    # Exactly once: a later tick neither re-lists nor re-files it.
    assert escalate_overdue(factory).escalated == []
    assert rows(factory, "SELECT count(*) FROM delivery_team_overdue_escalation") == [(1,)]
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        rows(factory, "DELETE FROM delivery_team_overdue_escalation")


def test_assignment_within_due_date_is_not_escalated(rig, test_dsn, psycopg):  # noqa: F811
    from command_center.orchestrator.delivery_overdue import escalate_overdue
    factory, _store, _queue = rig
    audit = overdue_rig(rig, test_dsn, psycopg, sla="5 days")
    assert escalate_overdue(factory).escalated == []
    incident = audit["context"]["incidents"][0]["incident_key"]
    # The claim re-derives the condition; a caller cannot escalate early.
    assert rows(factory, "SELECT delivery_team_claim_overdue(%s,%s,%s,%s)",
                (TASK, incident, CORRECTION, "VOYN-EARLY")) == [(False,)]


def test_recorded_effectiveness_check_stops_escalation(rig, test_dsn, psycopg, connect_as):  # noqa: F811
    from command_center.orchestrator.delivery_overdue import escalate_overdue
    factory, store, _queue = rig
    audit = overdue_rig(rig, test_dsn, psycopg)
    assert store.upsert_task(_task(CORRECTION, repo="repo-one", status="DONE"))[0]
    assert store.record_evidence(CORRECTION, "sha", SHA)[0]
    reference = "https://ci.example/correction/18#sha256=" + "d" * 64
    assert rows(lambda: connect_as(roles.OPERATOR_ROLE),
                "SELECT delivery_team_record_corrective_check(%s,1,%s,%s,%s,%s,'C1','effectiveness-check',%s,clock_timestamp())",
                (TASK, audit["context"]["incidents"][0]["incident_key"], CORRECTION,
                 store.get_task(CORRECTION)["revision"], SHA, reference)) == [(True,)]
    assert escalate_overdue(factory).escalated == []


def test_refused_escalation_task_rolls_back_the_claim(rig, test_dsn, psycopg, monkeypatch):  # noqa: F811
    from command_center.db.backlog_store import BacklogStore
    from command_center.orchestrator.delivery_overdue import escalate_overdue
    factory, _store, _queue = rig
    overdue_rig(rig, test_dsn, psycopg)
    monkeypatch.setattr(BacklogStore, "upsert_task", lambda self, task: (False, "refused_for_test", False))
    assert escalate_overdue(factory).refused == [(CORRECTION, "refused_for_test")]
    assert rows(factory, "SELECT count(*) FROM delivery_team_overdue_escalation") == [(0,)]
    monkeypatch.undo()
    assert len(escalate_overdue(factory).escalated) == 1


def test_team_tick_escalates_overdue_unless_stopped(rig, test_dsn, psycopg, monkeypatch):  # noqa: F811
    from command_center.orchestrator import delivery_team as team
    factory, _store, _queue = rig
    overdue_rig(rig, test_dsn, psycopg)
    monkeypatch.setenv("AICC_DELIVERY_TEAM_ENABLED", "1")
    monkeypatch.setenv("AICC_DELIVERY_TEAM_TASK_IDS", json.dumps([TASK]))
    monkeypatch.setenv("AICC_DELIVERY_TEAM_STOP", "1")
    assert team.tick(factory).escalated == []
    monkeypatch.delenv("AICC_DELIVERY_TEAM_STOP")
    monkeypatch.setattr("command_center.orchestrator.planner.repo_route", lambda repo: None)
    assert [task for task, _ in team.tick(factory).escalated] == [CORRECTION]
