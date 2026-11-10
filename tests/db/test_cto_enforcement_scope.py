"""0037: activated CTO enforcement applies to the operator's scope and is reversible.

PostgreSQL-backed like the 0036 suite: every test here is skipped without
AICC_TEST_PG_ADMIN_DSN. The shared `cto_rig` enrolls TASK and the operator puts
it in `cto_scope`; OTHER is never in scope. Payloads below are built by the
real producers (delivery_team.build_payload, planner._payload_for, the
review_merge cascade helpers) or copied field-for-field from review_merge.

Owner decision 2026-11-10: automatic PR-review jobs (`_cto_review_class`) are
out of scope even for a scoped task. The `review_world` fixture drives the
real review_merge producers and the real `cli._review_enqueue` writer against
this database; only GitHub is faked.
"""

from __future__ import annotations

import json
import re
import subprocess
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace

import pytest

from command_center.db import cli as db_cli
from command_center.db import migrations, roles
from command_center.db.work_queue_store import QueueAdmissionRefused, WorkQueueStore
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
    """F1 still holds for work under enforcement: a NULL repository can never
    match a binding. A PR-review job is no longer such work (it is admitted
    with or without a repository); a delivery-team review stage is."""
    app = cto_rig[0]
    role = delivery_team.build_payload(_candidate(app), ROUTE)
    assign(cto_rig, role)
    activate(cto_rig)
    assert raw_enqueue(cto_rig, "role:TASK", role, None, None) is None
    assert audit_count(cto_rig, "repository_unresolved", "enqueue") == 1
    assert app.execute("SELECT count(*) FROM work_item").fetchone()[0] == 0
    assert raw_enqueue(cto_rig, "review:TASK", review_payload(TASK), TASK, None)
    assert audit_count(cto_rig, "repository_unresolved", "enqueue") == 1


def test_scoped_review_with_repository_and_exact_assignment_is_admitted(cto_rig):
    """An assignment for a PR-review payload is not needed and is not consumed:
    the job is admitted unbound, so revoking that assignment cannot strand it."""
    payload = review_payload(TASK)
    aid = assign(cto_rig, payload)
    activate(cto_rig)
    wid = raw_enqueue(cto_rig, "review:TASK", payload, TASK)
    assert wid
    assert cto_rig[0].execute("SELECT count(*) FROM cto_work_binding").fetchone()[0] == 0
    cto_rig[1].execute("SELECT cto_revoke(%s,'not needed for a review job')", (aid,))
    work = claim(cto_rig)
    assert work[0] and work[2] == wid
    assert start(cto_rig, work[3])[0]


@pytest.mark.parametrize("kind", ["chunk", "retry", "verification"])
def test_scoped_review_variants_each_need_their_own_assignment(cto_rig, kind):
    """Before the owner decision each chunk, :retry:N and verification needed
    its own exact assignment. Now none does -- and the same variant turned
    into something that can write still needs one, exactly as before."""
    base = review_payload(TASK, chunk=0)
    activate(cto_rig)
    variant = {
        "chunk": review_payload(TASK, chunk=1),
        "retry": review_payload(TASK, chunk=0, cascade=review_merge._failover_cascade(
            base["cascade"], "claude")),
        "verification": verification_payload(TASK),
    }[kind]
    assert raw_enqueue(cto_rig, f"review:{kind}", variant, TASK)
    writing = {**variant, "task_type": "implementation"}
    assert raw_enqueue(cto_rig, f"write:{kind}", writing, TASK) is None
    assert audit_count(cto_rig, "assignment_required", "enqueue") == 1
    # With an assignment it passes CTO admission and reaches the next control,
    # the 0035 implementation gate.
    assign(cto_rig, writing)
    with pytest.raises(Exception, match="delivery_team_requirements_not_ready"):
        raw_enqueue(cto_rig, f"write:{kind}", writing, TASK)


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


# --- PR-review jobs of a scoped task (owner decision 2026-11-10) -------------------

BASE = "a" * 40
SMALL_DIFF = "diff --git a/x b/x\n+hi\n"
HUGE_DIFF = "diff --git a/x b/x\n+" + "я" * 60_000
FINDINGS = "x.py:1: missing authorization check\nVERDICT: REJECT\nHEAD_SHA: " + HEAD
RAW_TOKEN = "disposable claim fixture"


@pytest.fixture
def review_world(cto_rig, monkeypatch):
    """The real review producers and the real review queue writer on this
    database, as the app role. Only GitHub (`review_merge._gh`) is faked:
    `heads` maps PR number -> head sha, `diffs` maps head sha -> diff text."""
    app = cto_rig[0]
    monkeypatch.setenv("AICC_PLANNER_REPO_ROUTES", json.dumps({REPO: list(ROUTE)}))
    monkeypatch.setenv("AICC_GH_CACHE_TTL_SECONDS", "0")
    monkeypatch.delenv("AICC_EXECUTOR_PROFILE", raising=False)
    monkeypatch.setattr(review_merge, "_origin_owner_repo", lambda repo_path: ("x", REPO))
    heads, diffs = {}, {}

    def fake_gh(argv, repo):
        if argv[0] != "api" or len(argv) < 2:
            return subprocess.CompletedProcess(argv, 1, "", "?")
        compare = re.search(r"/compare/[0-9a-f]{40}\.\.\.([0-9a-f]{40})", argv[1])
        if compare is not None:
            return subprocess.CompletedProcess(argv, 0, diffs.get(compare.group(1), SMALL_DIFF), "")
        match = re.search(r"/pulls/(\d+)$", argv[1])
        if match is None:
            return subprocess.CompletedProcess(argv, 1, "", "?")
        number = int(match.group(1))
        body = {"state": "open", "base": {"sha": BASE, "repo": {"full_name": f"x/{REPO}"}},
                "head": {"sha": heads.get(number, HEAD)}, "changed_files": 1,
                "additions": 1, "deletions": 0}
        return subprocess.CompletedProcess(argv, 0, json.dumps(body), "")

    monkeypatch.setattr(review_merge, "_gh", fake_gh)

    def factory():
        return nullcontext(app)

    real_writer = db_cli._review_enqueue(WorkQueueStore(factory))
    sent = []

    def writer(queue, key, payload, task_id, max_attempts, repository_id=None):
        wid = real_writer(queue, key, payload, task_id, max_attempts, repository_id)
        sent.append(SimpleNamespace(key=key, payload=payload, task_id=task_id,
                                    repository_id=repository_id, work_item_id=wid))
        return wid

    def ready(task, number):
        """READY_TO_REVIEW with PR evidence, through the real state machine."""
        pr = f"https://github.com/x/{REPO}/pull/{number}"

        def rev():
            return app.execute("SELECT revision FROM backlog_task WHERE task_id=%s", (task,)).fetchone()[0]

        assert app.execute("SELECT ok FROM backlog_transition(%s,'IN_PROGRESS',%s)", (task, rev())).fetchone()[0]
        app.execute("SELECT backlog_record_evidence(%s,'pr',%s)", (task, pr))
        assert app.execute("SELECT ok FROM backlog_transition(%s,'READY_TO_REVIEW',%s)", (task, rev())).fetchone()[0]
        return pr

    return SimpleNamespace(factory=factory, writer=writer, sent=sent, heads=heads,
                           diffs=diffs, ready=ready)


def _produce_real_review_jobs(cto_rig, world, task=TASK, number=7):
    """Whole-PR chunks, a :retry:N with _failover_cascade, a finding
    verification and a pre-merge refresh -- each built and enqueued by the
    real review_merge function, for `task` at its PR."""
    world.diffs[HEAD] = HUGE_DIFF
    pr = world.ready(task, number)
    cfg = review_merge.ReviewConfig()
    report = review_merge.review_once(world.factory, world.writer, "/tmp")
    assert (task, pr) in report.reviewed, report
    chunks = [job for job in world.sent if "review_chunk" in job.payload]
    assert len(chunks) > 1 and len(chunks) == len(world.sent)

    # A chunk run that "succeeded" without a verdict: the next tick retries it.
    work = claim(cto_rig)
    assert work[0] and start(cto_rig, work[3])[0], work
    assert cto_rig[2].execute(
        "SELECT (queue_complete(%s,%s,%s::jsonb)).ok",
        (work[3], RAW_TOKEN, json.dumps({"executor": "claude", "status": "completed",
                                          "exit_code": 0, "result_text": "no verdict here"})),
    ).fetchone()[0]
    retry = review_merge.reconcile_review_once(world.factory, world.writer, "/tmp")
    assert retry.retried, retry
    retries = [job for job in world.sent if ":retry:" in job.key]
    assert len(retries) == 1 and retries[0].payload["cascade"][-1]["executor"] == "claude"

    snapshot = review_merge._PRSnapshot.create(HUGE_DIFF, BASE, HEAD)
    assert review_merge._verified_rejection_outcome(
        world.factory, world.writer, task, pr, snapshot, FINDINGS, cfg
    ) == ("WAIT", "verification_enqueued")
    assert world.sent[-1].payload["task_type"] == "verification_review"

    # The head moved: the refresh tick re-reviews the new exact head.
    world.heads[number] = "f" * 40
    before = len(world.sent)
    assert review_merge._enqueue_review_refresh(world.writer, "/tmp", task, pr, None, cfg) is None
    assert len(world.sent) == before + 1
    assert world.sent[-1].payload["review_head"]["head_sha"] == "f" * 40
    assert all(job.task_id == task and job.repository_id == REPO and job.work_item_id
               for job in world.sent)
    return world.sent


def _drain(cto_rig):
    """Claim and start everything claimable; returns the started work ids."""
    started = []
    while True:
        work = claim(cto_rig)
        if not work[0]:
            return started
        assert start(cto_rig, work[3])[0], work
        started.append(work[2])


def test_scoped_pr_review_jobs_pass_without_assignment(cto_rig, review_world):
    """Criterion 1: the real producers' whole/chunk, :retry:N, verification
    and refresh jobs of a scoped task are queued, claimed and started with
    enforcement active and no assignment at all."""
    app = cto_rig[0]
    activate(cto_rig)
    jobs = _produce_real_review_jobs(cto_rig, review_world)
    ids = {job.work_item_id for job in jobs}
    assert app.execute("SELECT count(*) FROM cto_assignment").fetchone()[0] == 0
    assert app.execute("SELECT count(*) FROM cto_work_binding").fetchone()[0] == 0
    # One chunk was already claimed, started and completed inside the helper.
    started = set(_drain(cto_rig))
    assert len(started) == len(ids) - 1 and started < ids
    assert audit_count(cto_rig, "review_class_exempt", "enqueue") == len(ids)
    for reason in ("assignment_required", "repository_unresolved", "assignment_missing"):
        assert audit_count(cto_rig, reason) == 0, reason


def test_scoped_pr_review_null_repository_and_redrive_pass(cto_rig, review_world):
    """Criterion 2: the same real payloads with repository_id NULL, and after
    queue_redrive of a dead-lettered one."""
    app, _, worker, _ = cto_rig
    activate(cto_rig)
    jobs = list(_produce_real_review_jobs(cto_rig, review_world))
    _drain(cto_rig)
    kinds = {
        "chunk": next(j for j in jobs if "review_chunk" in j.payload and ":retry:" not in j.key),
        "retry": next(j for j in jobs if ":retry:" in j.key),
        "verification": next(j for j in jobs if j.payload["task_type"] == "verification_review"),
        "refresh": jobs[-1],
    }
    for name, job in kinds.items():
        wid = raw_enqueue(cto_rig, f"null-repo:{name}", job.payload, TASK, None)
        assert wid, name
        work = claim(cto_rig)
        assert work[0] and work[2] == wid and start(cto_rig, work[3])[0], name
        assert worker.execute(
            "SELECT (queue_fail(%s,%s,'executor died',false)).ok", (work[3], RAW_TOKEN)
        ).fetchone()[0]
        assert app.execute("SELECT state FROM work_item WHERE work_item_id=%s", (wid,)).fetchone()[0] == "dead"
        assert app.execute("SELECT queue_redrive(%s,1)", (wid,)).fetchone()[0], name
        again = claim(cto_rig)
        assert again[0] and again[2] == wid and start(cto_rig, again[3])[0], name
        assert worker.execute(
            "SELECT (queue_complete(%s,%s,'{}'::jsonb)).ok", (again[3], RAW_TOKEN)
        ).fetchone()[0]
    assert audit_count(cto_rig, "repository_unresolved") == 0
    assert audit_count(cto_rig, "assignment_required") == 0
    assert app.execute("SELECT count(*) FROM cto_work_binding").fetchone()[0] == 0


def test_scoped_delivery_team_review_stage_still_needs_assignment(cto_rig):
    """Criterion 3: the delivery-team `independent_reviewer` stage is a
    different job (task_type `review`, a `delivery_team` block, a digest the
    operator knows in advance). It stays assigned."""
    app = cto_rig[0]
    candidate = _candidate(app)
    candidate = {**candidate, "stage": "independent_reviewer",
                 "context": {**(candidate.get("context") or {}), "source_sha": HEAD}}
    stage = delivery_team.build_payload(candidate, ROUTE)
    assert stage["delivery_team"]["stage"] == "independent_reviewer"
    activate(cto_rig)
    assert raw_enqueue(cto_rig, "stage:independent_reviewer", stage, None) is None
    assert audit_count(cto_rig, "assignment_required", "enqueue") == 1
    assert audit_count(cto_rig, "review_class_exempt") == 0
    assert app.execute("SELECT count(*) FROM work_item").fetchone()[0] == 0
    assign(cto_rig, stage)
    assert raw_enqueue(cto_rig, "stage:independent_reviewer", stage, None)
    assert app.execute("SELECT count(*) FROM cto_work_binding").fetchone()[0] == 1


def _without(payload, key):
    return {k: v for k, v in payload.items() if k != key}


LAUNDERING = {
    "task_type_implementation": lambda p: {**p, "task_type": "implementation"},
    "task_type_remediation": lambda p: {**p, "task_type": "remediation"},
    # Read-only for the worker, but not a PR-review job: it stays assigned.
    "task_type_review": lambda p: {**p, "task_type": "review",
                                   "cascade": [{**c, "task_type": "review"} for c in p["cascade"]]},
    "mutating_cascade_link": lambda p: {**p, "cascade": [
        *p["cascade"], {"executor": "codex", "task_type": "implementation"}]},
    "cascade_link_without_task_type": lambda p: {**p, "cascade": [
        *p["cascade"][:-1], _without(p["cascade"][-1], "task_type")]},
    "untrusted_false": lambda p: {**p, "untrusted": False},
    "no_review_head": lambda p: _without(p, "review_head"),
    "delivery_team_block": lambda p: {**p, "delivery_team": {"task_id": TASK}},
    "backlog_task_id": lambda p: {**p, "backlog_task_id": TASK},
    # Beyond the nine named shapes: every other field the rule reads.
    "delivery_team_source_sha": lambda p: {**p, "delivery_team_source_sha": HEAD},
    "untrusted_absent": lambda p: _without(p, "untrusted"),
    "untrusted_string": lambda p: {**p, "untrusted": "true"},
    "review_head_short_sha": lambda p: {**p, "review_head": {"pr_number": "7", "head_sha": "b" * 39}},
    "review_head_not_object": lambda p: {**p, "review_head": HEAD},
    "cascade_not_array": lambda p: {**p, "cascade": {"executor": "codex", "task_type": "independent_review"}},
    "cascade_null": lambda p: {**p, "cascade": None},
    "cascade_scalar_link": lambda p: {**p, "cascade": ["independent_review"]},
    "kind_simple_task": lambda p: {**p, "kind": "simple_task"},
    "task_type_absent": lambda p: _without(p, "task_type"),
}


@pytest.mark.parametrize("shape", sorted(LAUNDERING))
@pytest.mark.parametrize("base", ["review", "verification"])
def test_review_class_laundering_shapes_are_refused(cto_rig, shape, base):
    """Criterion 4: a producer cannot pass work that could write, or a
    delivery-team stage, off as a PR-review job of a scoped task."""
    honest = review_payload(TASK) if base == "review" else verification_payload(TASK)
    forged = LAUNDERING[shape](honest)
    assert forged != honest
    activate(cto_rig)
    assert not cto_rig[3].execute("SELECT _cto_review_class(%s::jsonb)", (json.dumps(forged),)).fetchone()[0]
    assert raw_enqueue(cto_rig, "forged", forged, TASK) is None
    assert cto_rig[0].execute("SELECT count(*) FROM work_item").fetchone()[0] == 0
    assert audit_count(cto_rig, "assignment_required", "enqueue") == 1
    assert audit_count(cto_rig, "review_class_exempt") == 0
    # The honest payload it was forged from is admitted.
    assert cto_rig[3].execute("SELECT _cto_review_class(%s::jsonb)", (json.dumps(honest),)).fetchone()[0]
    assert raw_enqueue(cto_rig, "honest", honest, TASK)


def test_review_class_rule_never_raises_on_arbitrary_json(cto_rig):
    admin = cto_rig[3]
    for value in ("null", "[]", "7", '"x"', '{"cascade":7}', '{"kind":"agent_run","cascade":"x"}'):
        assert admin.execute("SELECT _cto_review_class(%s::jsonb)", (value,)).fetchone()[0] is False
    assert admin.execute("SELECT _cto_review_class(NULL)").fetchone()[0] is False


def test_review_jobs_do_not_block_activation_or_scope_add(cto_rig):
    """Criterion 5: ready PR-review jobs are not unbound scoped work; ready
    work that can write still is."""
    app, operator = cto_rig[0], cto_rig[1]
    _other_task(app, enroll=True)
    for task in (TASK, OTHER):
        assert raw_enqueue(cto_rig, f"review:{task}", review_payload(task, chunk=0), task)
        assert raw_enqueue(cto_rig, f"verify:{task}", verification_payload(task), task, None)
    activate(cto_rig)
    assert operator.execute("SELECT cto_scope_add(%s,'pilot 2')", (OTHER,)).fetchone()[0]
    assert audit_count(cto_rig, "unbound_scoped_work") == 0
    assert len(_drain(cto_rig)) == 4

    # Writing work without a binding still blocks both levers.
    assert operator.execute("SELECT cto_scope_remove(%s,'again')", (OTHER,)).fetchone()[0]
    blocker = raw_enqueue(cto_rig, "write:other", {**PAYLOAD, "backlog_task_id": OTHER}, OTHER)
    assert blocker
    assert not operator.execute("SELECT cto_scope_add(%s,'too late')", (OTHER,)).fetchone()[0]
    assert deactivate(cto_rig)
    assert operator.execute("SELECT cto_scope_add(%s,'while off')", (OTHER,)).fetchone()[0]
    assert not try_activate(cto_rig)
    # Each refusal names the blocking work item for the operator.
    assert app.execute(
        "SELECT action,work_item_id FROM cto_assignment_event WHERE reason='unbound_scoped_work' ORDER BY event_id"
    ).fetchall() == [("scope_add", blocker), ("activate", blocker)]


def test_review_class_exemption_is_audited(cto_rig):
    """Criterion 6: each admission by the exemption leaves one audit row
    naming the work item; a task outside the scope leaves none."""
    app = cto_rig[0]
    _other_task(app)
    activate(cto_rig)
    scoped = [
        raw_enqueue(cto_rig, "review:chunk0", review_payload(TASK, chunk=0), TASK),
        raw_enqueue(cto_rig, "review:chunk1", review_payload(TASK, chunk=1), TASK, None),
        raw_enqueue(cto_rig, "verify", verification_payload(TASK), TASK),
    ]
    assert all(scoped)
    assert raw_enqueue(cto_rig, "review:other", review_payload(OTHER), OTHER)
    assert raw_enqueue(cto_rig, "review:none", review_payload("no task"), None)
    rows = app.execute(
        "SELECT action,actor,assignment_id,work_item_id FROM cto_assignment_event"
        " WHERE reason='review_class_exempt' ORDER BY event_id"
    ).fetchall()
    assert rows == [("enqueue", roles.APP_ROLE, None, wid) for wid in scoped]
    # An idempotent replay returns the same item and is not a new admission.
    assert raw_enqueue(cto_rig, "review:chunk0", review_payload(TASK, chunk=0), TASK) == scoped[0]
    assert audit_count(cto_rig, "review_class_exempt") == 3
    # With enforcement off nothing is exempted, so nothing is recorded.
    assert deactivate(cto_rig)
    assert raw_enqueue(cto_rig, "review:off", review_payload(TASK, chunk=2), TASK)
    assert audit_count(cto_rig, "review_class_exempt") == 3


# --- N1: one refused admission does not stop the review tick ------------------------


def test_refused_review_admission_does_not_stop_the_review_tick(cto_rig, review_world, monkeypatch, capsys):
    """Three READY_TO_REVIEW tasks; the database refuses the middle one's
    review item. The real `backlog-review` tick queues the first and the
    third, records the refusal as a skip and still runs its retry, verdict
    and watchdog steps."""
    app = cto_rig[0]
    first, last = "VOYN-CTO-A-FIRST", "VOYN-CTO-Z-LAST"
    for task in (first, last):
        assert app.execute(
            "SELECT * FROM backlog_upsert_task(%s,'1','P1','OPEN','task','t','bounded',%s)", (task, REPO)
        ).fetchone()[0]
    prs = {first: review_world.ready(first, 6), TASK: review_world.ready(TASK, 7),
           last: review_world.ready(last, 8)}
    assert first < TASK < last
    # The middle task's review key already names bound, in-scope work with
    # another payload, so the review item is a genuine idempotency_conflict.
    key = review_merge._review_key(TASK, prs[TASK], review_merge._PRSnapshot.create(SMALL_DIFF, BASE, HEAD))
    assign(cto_rig)
    activate(cto_rig)
    assert enqueue(cto_rig, key=key)

    with pytest.raises(QueueAdmissionRefused):
        review_world.writer("execution", key, review_payload(TASK), TASK, 2, REPO)

    @contextmanager
    def connection():
        yield app

    monkeypatch.setattr(db_cli, "load_config", lambda: SimpleNamespace(redacted=lambda: "test"))
    monkeypatch.setattr(db_cli.pool, "open_pool", lambda config: None)
    monkeypatch.setattr(db_cli.pool, "close_pool", lambda: None)
    monkeypatch.setattr(db_cli.pool, "connection", connection)
    steps = []
    for name in ("reconcile_review_once", "publish_review_verdicts"):
        real = getattr(review_merge, name)
        monkeypatch.setattr(
            review_merge, name,
            lambda *a, _real=real, _name=name, **k: (steps.append(_name), _real(*a, **k))[1],
        )

    assert db_cli.main(["backlog-review", "--repo-path", "/tmp"]) == 0
    out = capsys.readouterr().out
    assert f"REVIEW    {first} -> {prs[first]}" in out
    assert f"REVIEW    {last} -> {prs[last]}" in out
    assert f"SKIP      {TASK}: queue_admission_refused" in out
    assert steps == ["reconcile_review_once", "publish_review_verdicts"]
    assert "WATCHDOG  escalated=" in out
    queued = app.execute(
        "SELECT task_id FROM work_item WHERE idempotency_key LIKE 'review:%%'"
        " AND payload->>'task_type'='independent_review' ORDER BY task_id"
    ).fetchall()
    assert queued == [(first,), (last,)]
    assert audit_count(cto_rig, "idempotency_conflict", "enqueue") == 2
    assert app.execute(
        "SELECT count(*) FROM tick_skip_event WHERE tick_kind='review' AND task_id=%s"
        " AND reason='queue_admission_refused'", (TASK,)
    ).fetchone()[0] == 1
    # The next tick is not stopped either.
    assert db_cli.main(["backlog-review", "--repo-path", "/tmp"]) == 0


# --- N4 / N8: the operator can retire a row nothing else can remove -----------------


def retire(rig, work, reason="blocks activation"):
    return rig[1].execute("SELECT cto_work_retire(%s,%s)", (work, reason)).fetchone()[0]


def _state(rig, work):
    return rig[0].execute(
        "SELECT state,dead_reason FROM work_item WHERE work_item_id=%s", (work,)
    ).fetchone()


def test_operator_retires_delayed_work_that_blocks_scope_add_and_activation(cto_rig):
    """N4, first scenario: one row delayed for 60+ years, written by the app
    role, held cto_scope_add and then cto_activate in refusal forever."""
    app, operator = cto_rig[0], cto_rig[1]
    _other_task(app, enroll=True)
    activate(cto_rig)
    blocker = app.execute(
        "SELECT queue_enqueue('execution','parked',%s::jsonb,%s,%s,3,0,2000000000)",
        (json.dumps({**PAYLOAD, "backlog_task_id": OTHER}), OTHER, REPO),
    ).fetchone()[0]
    assert blocker
    assert not operator.execute("SELECT cto_scope_add(%s,'pilot 2')", (OTHER,)).fetchone()[0]
    assert not claim(cto_rig)[0]  # nothing can claim it away
    # The refusal audit names the row; the operator needs no queue access.
    named = operator.execute(
        "SELECT work_item_id FROM cto_assignment_event WHERE reason='unbound_scoped_work'"
        " ORDER BY event_id DESC LIMIT 1"
    ).fetchone()[0]
    assert named == blocker
    assert retire(cto_rig, named, "parked 60 years; blocks pilot 2")
    assert _state(cto_rig, blocker) == ("dead", "cto_operator_retired")
    assert operator.execute("SELECT cto_scope_add(%s,'pilot 2')", (OTHER,)).fetchone()[0]
    # The same row cannot come back while enforcement is active.
    assert not app.execute("SELECT queue_redrive(%s,1)", (blocker,)).fetchone()[0]
    assert app.execute(
        "SELECT action,work_item_id,reference,actor FROM cto_control_event WHERE action='work_retire'"
    ).fetchall() == [("work_retire", blocker, "parked 60 years; blocks pilot 2", roles.OPERATOR_ROLE)]
    assert audit_count(cto_rig, "granted", "retire") == 1
    assert app.execute(
        "SELECT event,outcome,reason,actor_role FROM work_event WHERE work_item_id=%s ORDER BY seq DESC LIMIT 1",
        (blocker,),
    ).fetchone() == ("retire", "granted", "cto_operator_retired", roles.OPERATOR_ROLE)


def test_operator_retires_unserved_taskless_work_atomically_with_activation(cto_rig):
    """N4, second scenario: a writing row without a task, in a queue nobody
    serves, held cto_activate in refusal. Retire and activate share one
    transaction, so no new blocking row can slip in between."""
    app, operator = cto_rig[0], cto_rig[1]
    writing = {"kind": "agent_run", "task_type": "implementation", "prompt": "write"}
    blocker = app.execute(
        "SELECT queue_enqueue('nobody-serves-this',%s,%s::jsonb)", ("orphan", json.dumps(writing))
    ).fetchone()[0]
    assert not try_activate(cto_rig)
    assert app.execute("SELECT queue_reap()").fetchone() is not None
    assert not try_activate(cto_rig)
    with operator.transaction():
        assert retire(cto_rig, blocker, "orphan in an unserved queue")
        assert try_activate(cto_rig)
    assert enabled(cto_rig)
    assert _state(cto_rig, blocker) == ("dead", "cto_operator_retired")


def test_retire_is_operator_only_audited_and_narrow(cto_rig, psycopg):
    app, operator, worker, admin = cto_rig
    _other_task(app, enroll=True)
    plain = "VOYN-CTO-NOT-ENROLLED"
    assert app.execute(
        "SELECT * FROM backlog_upsert_task(%s,'1','P1','OPEN','task','t','bounded',%s)", (plain, REPO)
    ).fetchone()[0]
    aid = assign(cto_rig)
    activate(cto_rig)
    live = enqueue(cto_rig, key="live")
    review = raw_enqueue(cto_rig, "review", review_payload(TASK), TASK)
    unenrolled = raw_enqueue(cto_rig, "plain", {**PAYLOAD, "backlog_task_id": plain}, plain)
    audit = raw_enqueue(cto_rig, "audit", audit_payload(), None, None)
    enrolled = raw_enqueue(cto_rig, "other", {**PAYLOAD, "backlog_task_id": OTHER}, OTHER)
    assert all((live, review, unenrolled, audit, enrolled))

    for conn in (app, worker):
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("SELECT cto_work_retire(%s,'forged')", (enrolled,))
    assert not admin.execute("SELECT cto_work_retire(%s,'superuser')", (enrolled,)).fetchone()[0]
    assert not retire(cto_rig, enrolled, "   ")
    assert not retire(cto_rig, "wrk_caller_supplied_text")
    # Live assigned work, PR-review jobs, read-only audits and work of a task
    # no enforcement rule could cover are not the operator's to kill here.
    for work in (live, review, unenrolled, audit):
        assert not retire(cto_rig, work), work
        assert _state(cto_rig, work)[0] == "ready"
    assert audit_count(cto_rig, "operator_required", "retire") == 1
    assert audit_count(cto_rig, "reason_required", "retire") == 1
    assert audit_count(cto_rig, "unknown_work_item", "retire") == 1
    assert audit_count(cto_rig, "not_retirable", "retire") == 4
    assert app.execute(
        "SELECT count(*) FROM cto_assignment_event WHERE action='retire' AND work_item_id='wrk_caller_supplied_text'"
    ).fetchone()[0] == 0

    # N8: a bound row whose assignment was revoked can be retired...
    operator.execute("SELECT cto_revoke(%s,'revoked')", (aid,))
    assert retire(cto_rig, live, "assignment revoked")
    assert _state(cto_rig, live) == ("dead", "cto_operator_retired")
    # ...and claimed work never: it is running or will be reaped.
    work = claim(cto_rig)
    assert work[0]
    assert not retire(cto_rig, work[2])
    assert audit_count(cto_rig, "not_ready", "retire") == 1
    assert retire(cto_rig, enrolled, "names an enrolled task")
    assert app.execute("SELECT count(*) FROM cto_control_event WHERE action='work_retire'").fetchone()[0] == 2
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        app.execute("INSERT INTO cto_control_event(action,work_item_id,reference,actor)"
                    " VALUES('work_retire','x','x','x')")


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
