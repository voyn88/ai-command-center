"""BO-S2: the dispatch protocol and the planner tick, on real PostgreSQL.

The atomic act under the real ``aicc_app`` grants, the wave gate under the
approved semantics (a later numeric wave yields only while the earliest
unfinished numeric wave still has a dispatchable candidate), and the whole
plan_once composition end to end — dispatch through the store, execution
through a real worker-role claim, lane release on the terminal state.

Skipped wholesale unless ``AICC_TEST_PG_ADMIN_DSN`` is set — see ``conftest``.
"""

from __future__ import annotations

import json
import secrets

import pytest

from command_center.db import roles
from command_center.db.backlog_parser import ParsedTask
from command_center.db.backlog_store import BacklogStore
from command_center.db.work_queue_store import ClaimedWork, WorkQueueStore
from command_center.orchestrator.planner import PlanLimits, _payload_for, plan_once

pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]


def _as_role(dsn: str, role: str, password: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=password)
    return make_conninfo(**params)


def _provision(admin_conn, psycopg, test_dsn, role_passwords) -> None:
    from command_center.db import migrations

    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]),
        autocommit=True,
    ) as conn:
        migrations.upgrade(conn)
        roles.apply_table_grants(conn)


@pytest.fixture
def rig(admin_conn, psycopg, test_dsn, role_passwords):
    """(app_factory, backlog_store, worker_queue_store) under real grants."""
    from contextlib import contextmanager

    from psycopg import sql

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    name = f"aicc_wh_plan_{secrets.token_hex(4)}"
    password = secrets.token_urlsafe(24)
    with admin_conn.cursor() as cur:
        for statement in roles.render_worker_host_role(name):
            cur.execute(statement)
        cur.execute(
            sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                sql.Identifier(name), sql.Literal(password)
            )
        )
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    worker_dsn = _as_role(test_dsn, name, password)

    def factory_for(dsn):
        @contextmanager
        def factory():
            with psycopg.connect(dsn, autocommit=True) as conn:
                yield conn

        return factory

    app_factory = factory_for(app_dsn)
    try:
        yield (
            app_factory,
            BacklogStore(app_factory),
            WorkQueueStore(factory_for(worker_dsn)),
        )
    finally:
        with admin_conn.cursor() as cur:
            try:
                cur.execute(
                    sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name))
                )
            except Exception:  # noqa: BLE001 — cleanup must not mask a failure
                admin_conn.rollback()


@pytest.fixture(autouse=True)
def _test_repo_routes(monkeypatch, request):
    """Every synthetic repo-* used by this suite gets a route: the planner now
    refuses unrouted repos (the first live tick's lesson), and these tests are
    about dispatch mechanics, not the route table. The route tests below opt
    out by overriding the variable themselves."""
    import json

    repos = ["repo-d2","repo-ga","repo-gb","repo-gc","repo-in","repo-nm",
             "repo-one","repo-p1","repo-p3","repo-pk","repo-shared","repo-tt"]
    monkeypatch.setenv(
        "AICC_PLANNER_REPO_ROUTES",
        json.dumps({r: ["AICC", f"/srv/{r}"] for r in repos}),
    )


def _task(task_id: str, **overrides) -> ParsedTask:
    values = dict(
        task_id=task_id,
        wave="0",
        priority="P0",
        status="OPEN",
        kind="task",
        title=task_id.lower(),
        body="do the thing",
        repo=f"repo-{task_id[-2:]}",
        line_no=1,
    )
    values.update(overrides)
    return ParsedTask(**values)


def test_dispatch_payload_carries_the_specific_task_id() -> None:
    """VOYN-W0-AICC-PUBLISH-BRANCH-COLLISION: the publish branch is
    `backlog/<backlog_task_id>` (publish.py, via handlers.py). Before this
    field existed the payload only carried `project_id` -- shared by every
    task in one repo -- so every dispatch for the same repo published to the
    SAME branch and a later force-push erased an earlier task's still-open
    work. Pinned here so a future refactor cannot silently drop the field
    the payload's shape review would not otherwise catch (dict access, not a
    typed schema at this layer)."""
    task = {
        "task_id": "VOYN-W0-SPECIFIC-TASK",
        "wave": "0",
        "priority": "P0",
        "title": "t",
        "body": "b",
    }
    payload, _budget = _payload_for(task, PlanLimits(), ("AICC", "/srv/repo"))
    assert payload["backlog_task_id"] == "VOYN-W0-SPECIFIC-TASK"


def _dispatch(app_factory, task_id, planner="planner-t", wip=4, payload=None):
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM backlog_dispatch(%s, %s, 3600, %s, %s::jsonb, 3)",
                (task_id, planner, wip, json.dumps(payload or {"kind": "agent_run"})),
            )
            return cur.fetchone()


def test_dispatch_is_one_atomic_act(rig) -> None:
    """Lease + enqueue + IN_PROGRESS + audit together; a refusal leaves NO
    trace of any step."""
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-AT"))[0]
    ok, _reason, work_item_id, revision = _dispatch(app_factory, "VOYN-W0-AT")
    assert ok and work_item_id.startswith("wki") and revision == 2
    task = store.get_task("VOYN-W0-AT")
    assert task["status"] == "IN_PROGRESS"
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM work_item_public WHERE task_id = %s", ("VOYN-W0-AT",)
            )
            assert cur.fetchall() == [("ready",)]
            cur.execute(
                "SELECT count(*) FROM backlog_writer_lease "
                "WHERE authority = %s AND owner = %s",
                ("repo:" + task["repo"], "planner-t"),
            )
            assert cur.fetchone()[0] == 1

    # A refusal (already IN_PROGRESS) mutates nothing further.
    ok, reason, *_ = _dispatch(app_factory, "VOYN-W0-AT")
    assert not ok and reason == "not_eligible"


def test_refusals_leave_no_lease_and_no_work_item(rig) -> None:
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-D1", repo="repo-shared"))[0]
    assert store.upsert_task(_task("VOYN-W0-D2", repo="repo-d2"))[0]
    assert store.add_dependency("VOYN-W0-D2", "VOYN-W0-D1")[0]

    ok, reason, *_ = _dispatch(app_factory, "VOYN-W0-D2")
    assert not ok and reason == "dependencies_unsatisfied"
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM work_item_public")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM backlog_writer_lease")
            assert cur.fetchone()[0] == 0


def test_one_writer_per_repository_across_planners(rig) -> None:
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-R1", repo="repo-one"))[0]
    assert store.upsert_task(_task("VOYN-W0-R2", repo="repo-one"))[0]
    assert _dispatch(app_factory, "VOYN-W0-R1", planner="planner-a")[0]
    ok, reason, *_ = _dispatch(app_factory, "VOYN-W0-R2", planner="planner-b")
    assert not ok and reason == "repo_busy"
    # The SAME planner may take a second task in its held repo? No: the lease
    # renews for the holder, so the dispatch proceeds — one WRITER, not one
    # task, is the invariant; WIP is the task cap.
    assert _dispatch(app_factory, "VOYN-W0-R2", planner="planner-a")[0]


def test_wip_limit_is_enforced_in_the_database(rig) -> None:
    app_factory, store, _worker = rig
    for i in range(3):
        assert store.upsert_task(_task(f"VOYN-W0-W{i}", repo=f"repo-w{i}"))[0]
    assert _dispatch(app_factory, "VOYN-W0-W0", wip=2)[0]
    assert _dispatch(app_factory, "VOYN-W0-W1", wip=2)[0]
    ok, reason, *_ = _dispatch(app_factory, "VOYN-W0-W2", wip=2)
    assert not ok and reason == "wip_exhausted"


def test_the_wave_gate_yields_exactly_when_the_earlier_wave_is_spent(rig) -> None:
    """Approved decision 1, both directions: refused while the earliest
    numeric wave has a dispatchable candidate; admitted the moment it has
    none (each remaining task blocked by deps, busy repo, or no repo).
    Named lanes bypass throughout."""
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-GA", repo="repo-ga"))[0]
    assert store.upsert_task(_task("VOYN-W1-GB", wave="1", repo="repo-gb"))[0]
    assert store.upsert_task(_task("VOYN-COM-GC", wave="COM", repo="repo-gc"))[0]

    ok, reason, *_ = _dispatch(app_factory, "VOYN-W1-GB")
    assert not ok and reason == "earlier_wave_has_eligible_work"
    assert _dispatch(app_factory, "VOYN-COM-GC")[0], "named lanes are always parallel"

    assert _dispatch(app_factory, "VOYN-W0-GA")[0]
    ok, reason, *_ = _dispatch(app_factory, "VOYN-W1-GB")
    assert ok, f"wave 0 spent, wave 1 must be admitted (got {reason})"


def test_planner_tick_end_to_end_with_a_real_worker(rig) -> None:
    """plan_once dispatches by wave order, reports the wave gate, skips the
    repo-less; a real worker-role claim executes and dead-letters; the next
    tick releases the lane."""
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-P1", repo="repo-p1"))[0]
    assert store.upsert_task(_task("VOYN-W0-P2", priority="P1", repo=None))[0]
    assert store.upsert_task(_task("VOYN-W1-P3", wave="1", repo="repo-p3"))[0]

    limits = PlanLimits(planner="planner-e2e", wip_limit=2, max_dispatches_per_tick=2)
    report = plan_once(app_factory, limits)
    assert [t for t, _ in report.dispatched] == ["VOYN-W0-P1", "VOYN-W1-P3"], (
        "wave 0 spent itself on P1, so wave 1 rides the SAME tick — the "
        "approved non-blockade semantics"
    )
    assert report.undispatchable == [("VOYN-W0-P2", "no_repo")]
    assert report.skipped_by_wave_gate == []

    # The queue delivers to a real worker; a non-retryable failure (budget 1
    # link -> attempts land on the same route) dead-letters the item.
    claimed = worker.claim("execution", visibility_seconds=60)
    assert isinstance(claimed, ClaimedWork)
    assert claimed.payload["kind"] == "agent_run"
    assert claimed.payload["cascade"], "the planner must route through the cascade"
    assert worker.fail(claimed, reason="synthetic failure", retryable=False)

    report2 = plan_once(app_factory, limits)
    assert ("VOYN-W0-P1", "returned_to_pool") in report2.ingested
    # A finding, not a loss (BO-S3): the freed task re-enters the pool and
    # the SAME tick redispatches it as a fresh epoch — new revision, new
    # idempotency key, new work item, fresh cascade budget.
    assert "VOYN-W0-P1" in [t for t, _ in report2.dispatched]
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM work_item_public WHERE task_id = %s",
                ("VOYN-W0-P1",),
            )
            assert cur.fetchone()[0] == 2, "a fresh dispatch epoch, not a re-run"


def test_two_planner_ticks_cannot_run_concurrently(rig) -> None:
    app_factory, _store, _worker = rig
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ok FROM backlog_lease_acquire('planner:global', 'other-host', 60)"
            )
            assert cur.fetchone()[0]
    report = plan_once(app_factory, PlanLimits(planner="planner-late"))
    assert report.planner_busy and report.dispatched == []


# -- result ingest (BO-S3) ----------------------------------------------------


def _complete_latest(app_factory, worker, task_id, result):
    claimed = worker.claim("execution", visibility_seconds=60)
    assert isinstance(claimed, ClaimedWork), claimed
    assert claimed.payload.get("project_id", task_id)  # sanity
    assert worker.complete(claimed, result)
    return claimed


def test_ingest_succeeded_records_evidence_and_moves_to_review(rig) -> None:
    """One act: evidence from the persisted result (never from a claim) +
    IN_PROGRESS -> READY_TO_REVIEW through the existing machine + lane
    freed. The recorded pr/sha are exactly what the DONE gate then accepts."""
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-IN", repo="repo-in"))[0]
    assert _dispatch(app_factory, "VOYN-W0-IN")[0]
    _complete_latest(
        app_factory,
        worker,
        "VOYN-W0-IN",
        {
            "status": "completed",
            "pr_url": "https://github.com/o/r/pull/7",
            "head_sha": "feedface",
        },
    )

    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
            rows = cur.fetchall()
    assert [(r[0], r[2]) for r in rows] == [("VOYN-W0-IN", "ready_to_review")]

    task = store.get_task("VOYN-W0-IN")
    assert task["status"] == "READY_TO_REVIEW"
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT kind, value FROM backlog_evidence WHERE task_id = %s ORDER BY kind",
                ("VOYN-W0-IN",),
            )
            assert cur.fetchall() == [
                ("pr", "https://github.com/o/r/pull/7"),
                ("sha", "feedface"),
            ]
            cur.execute(
                "SELECT count(*) FROM backlog_writer_lease WHERE authority = %s",
                ("repo:repo-in",),
            )
            assert cur.fetchone()[0] == 0

    # The external merge fact then closes through the EXISTING gate.
    ok, reason, _ = store.transition("VOYN-W0-IN", "DONE", task["revision"])
    assert ok, reason


def test_ingest_without_machine_outcome_still_reviews_but_done_holds(rig) -> None:
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-NM", repo="repo-nm"))[0]
    assert _dispatch(app_factory, "VOYN-W0-NM")[0]
    _complete_latest(app_factory, worker, "VOYN-W0-NM", {"status": "completed"})
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
            cur.fetchall()
    task = store.get_task("VOYN-W0-NM")
    assert task["status"] == "READY_TO_REVIEW"
    ok, reason, _ = store.transition("VOYN-W0-NM", "DONE", task["revision"])
    assert not ok and reason.startswith("missing_evidence")


def test_ingest_queue_succeeded_but_task_failed_returns_to_pool_not_review(rig) -> None:
    """The queue's `succeeded` means only "this attempt is terminal, do not
    redeliver it" (worker/handlers.py: redelivering an already-executed
    mutating run would re-apply its side effects) -- it is NOT a claim that
    the agent's own run succeeded. A `worker.complete()` result whose
    payload says `status: "failed"` (e.g. exit_code=1, an API/credit error)
    must take the same cascade-exhaustion path as a `dead` work item, not
    reach READY_TO_REVIEW with no pr/sha and no path back. Reproduces the
    live 2026-08-20 incident (4 tasks stuck exactly this way)."""
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-QF", repo="repo-qf"))[0]
    assert _dispatch(app_factory, "VOYN-W0-QF")[0]
    _complete_latest(
        app_factory,
        worker,
        "VOYN-W0-QF",
        {"status": "failed", "exit_code": 1, "result_text": "Credit balance is too low"},
    )

    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
            rows = cur.fetchall()
    assert [(r[0], r[2]) for r in rows] == [("VOYN-W0-QF", "returned_to_pool")]

    task = store.get_task("VOYN-W0-QF")
    assert task["status"] == "OPEN"
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM backlog_evidence WHERE task_id = %s",
                ("VOYN-W0-QF",),
            )
            assert cur.fetchone()[0] == 0


def test_migration_0009_is_reversible_without_residue(pg_connection_factory) -> None:
    """Live up->down->up on the exact function body: down restores 0007's
    (pre-fix) definition, up reapplies 0009's fix -- pinned so a future
    no-op down (CREATE FUNCTION, not OR REPLACE) breaks the second up
    loudly instead of leaving stale behaviour undetected."""
    from command_center.db import migrations

    with pg_connection_factory() as conn:
        migrations.upgrade(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_ingest_results'"
            )
            assert "v_task_status" in cur.fetchone()[0]
        migrations.downgrade(conn, target=8)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_ingest_results'"
            )
            assert "v_task_status" not in cur.fetchone()[0]
        migrations.upgrade(conn)  # must not raise 'already exists'
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_ingest_results'"
            )
            assert "v_task_status" in cur.fetchone()[0]


def test_second_cascade_exhaustion_parks_for_the_owner(rig) -> None:
    """First exhaustion: a finding — back to OPEN, fresh epoch. Second:
    DEFER_TO_USER — two full budgets failing is a human's decision point,
    and the OPEN<->dead pump would otherwise burn the fleet on one task."""
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-PK", repo="repo-pk"))[0]

    for round_no, expected in ((1, "OPEN"), (2, "DEFER_TO_USER")):
        assert _dispatch(app_factory, "VOYN-W0-PK")[0], f"round {round_no}"
        claimed = worker.claim("execution", visibility_seconds=60)
        assert isinstance(claimed, ClaimedWork)
        assert worker.fail(claimed, reason=f"round {round_no}", retryable=False)
        with app_factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
                rows = cur.fetchall()
        assert len(rows) == 1
        assert store.get_task("VOYN-W0-PK")["status"] == expected, rows
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reason FROM backlog_event WHERE task_id = %s "
                "AND event = 'return_to_pool' AND outcome = 'granted' ORDER BY event_id",
                ("VOYN-W0-PK",),
            )
            reasons = [r[0] for r in cur.fetchall()]
    assert len(reasons) == 2 and all(r.startswith("cascade_exhausted") for r in reasons)


def test_return_to_pool_refuses_outside_in_progress(rig) -> None:
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-RG"))[0]
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ok, reason FROM backlog_return_to_pool(%s, %s)",
                ("VOYN-W0-RG", "manual"),
            )
            assert cur.fetchone() == (False, "not_in_progress")


def test_the_tick_lease_uses_its_own_ttl_not_the_repo_horizon(rig) -> None:
    """PLANNER-LEASE-TTL, pinned (acceptance 7b: the mutation
    planner_lease_ttl_seconds -> lease_ttl_seconds in the planner:global
    acquire survived every other test). Interception at the SQL seam: the
    global acquire must carry the TICK ttl, and dispatch must carry the RUN
    ttl — two deliberately different numbers in one plan."""
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-TT", repo="repo-tt"))[0]

    calls: list[tuple[str, tuple]] = []

    from contextlib import contextmanager

    @contextmanager
    def recording_factory():
        with app_factory() as conn:
            class RecordingCursor:
                def __init__(self, cur):
                    self._cur = cur

                def execute(self, sql, params=()):
                    calls.append((sql, tuple(params)))
                    return self._cur.execute(sql, params)

                def __getattr__(self, name):
                    return getattr(self._cur, name)

                def __enter__(self):
                    self._cur.__enter__()
                    return self

                def __exit__(self, *exc):
                    return self._cur.__exit__(*exc)

            class RecordingConn:
                def cursor(self):
                    return RecordingCursor(conn.cursor())

                def __getattr__(self, name):
                    return getattr(conn, name)

            yield RecordingConn()

    limits = PlanLimits(
        planner="planner-ttl", lease_ttl_seconds=7200, planner_lease_ttl_seconds=123
    )
    report = plan_once(recording_factory, limits)
    assert [t for t, _ in report.dispatched] == ["VOYN-W0-TT"]

    acquires = [p for s, p in calls if "backlog_lease_acquire" in s]
    assert ("planner:global", "planner-ttl", 123) in acquires, acquires
    dispatches = [p for s, p in calls if "backlog_dispatch" in s]
    assert len(dispatches) == 1 and dispatches[0][2] == 7200, dispatches


def test_repo_routes_translate_the_backlog_vocabulary(monkeypatch) -> None:
    """The first live tick died three honest deaths: the payload carried the
    backlog's repo string where the worker's validate_repository demands a
    canonical PROJECT_IDS member plus its configured path. The route table
    is the translation, and an unrouted repo must never dispatch."""
    from command_center.orchestrator.planner import repo_route

    monkeypatch.delenv("AICC_PLANNER_REPO_ROUTES", raising=False)
    assert repo_route("ai-command-center") == (
        "AICC", "/home/voynadmin/Projects/ai-command-center"
    )
    assert repo_route("aios")[0] == "AIOS"
    assert repo_route("nowhere/unknown") is None
    monkeypatch.setenv("AICC_PLANNER_REPO_ROUTES", '{"x": ["AIOS", "/p"]}')
    assert repo_route("x") == ("AIOS", "/p")
    assert repo_route("aios") is None  # override replaces, not merges
    # Fail closed must be probed on a key the DEFAULTS would answer —
    # otherwise "closed" and "fell through to defaults" are identical
    # (review mutant (б) survived on exactly that blindness).
    monkeypatch.setenv("AICC_PLANNER_REPO_ROUTES", "{broken json")
    assert repo_route("aios") is None
    for bad in ('{"x": "AICC"}', '{"x": {"a": 1}}', '{"x": ["AICC", 7]}',
                '{"x": ["AICC", ""]}', '[1, 2]'):
        monkeypatch.setenv("AICC_PLANNER_REPO_ROUTES", bad)
        assert repo_route("x") is None and repo_route("aios") is None, bad


def test_an_unrouted_repo_is_reported_not_dead_lettered(rig, monkeypatch) -> None:
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-RR", repo="repo-without-route"))[0]
    monkeypatch.delenv("AICC_PLANNER_REPO_ROUTES", raising=False)
    report = plan_once(app_factory, PlanLimits(planner="router-test"))
    assert ("VOYN-W0-RR", "unknown_repo_route") in report.undispatchable
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_item WHERE task_id = %s", ("VOYN-W0-RR",))
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# The refusal audit must outlive the refusal (VOYN-W0-AICC-AUDIT-ROLLBACK-CLASS)
# ---------------------------------------------------------------------------
# The backlog layer's half of the class. The queue layer states and measures
# the rule in 0002, identity restates it in 0003, and these two functions were
# where it was disobeyed: each turned a RETURNED verdict back into an
# exception, and the exception rolled back the audit row the verdict's own
# function had just written. The class guard over the deployed schema lives in
# `test_refusal_audit_survives.py`; what is pinned here is the behaviour.


def _events(app_factory, task_id):
    """Read on a FRESH connection: committed, not merely visible to the
    transaction that wrote them."""
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT event, outcome, reason FROM backlog_event "
            "WHERE task_id = %s ORDER BY event_id",
            (task_id,),
        )
        return cur.fetchall()


def test_a_wedged_gate_row_is_refused_per_row_and_keeps_its_audit(rig) -> None:
    """One poisoned row must cost one row, not the tick and not the record.

    A `kind = 'gate'` record parked in IN_PROGRESS is reachable through the
    importer -- `backlog_upsert_task` may set any status directly, the CHECK
    constraints allow the combination, and the ingest loop selects every
    IN_PROGRESS task with a terminal work item without filtering on kind.
    `backlog_transition` then refuses it with `gate_is_control_record`.
    Before 0010 that refusal was raised: the tick died at this row, the
    healthy task's ingest died with it, the dispatch phase never ran, and
    every audit row of the whole attempt -- including the one naming the
    reason -- was rolled back. Tick after tick, identically, because the
    poisoned row is still there on the next pass.
    """
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-GA", repo="repo-ga"))[0]
    assert _dispatch(app_factory, "VOYN-W0-GA")[0]
    _complete_latest(
        app_factory,
        worker,
        "VOYN-W0-GA",
        {"status": "completed", "pr_url": "https://x/pull/1", "head_sha": "abc123"},
    )

    assert store.upsert_task(_task("VOYN-W0-GB", kind="gate", status="IN_PROGRESS", repo="repo-gb"))[0]
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT queue_enqueue('execution', %s, '{}'::jsonb, %s, %s, 3)",
            ("gate-item", "VOYN-W0-GB", "repo-gb"),
        )
        # The lane this row occupies, held by the planner about to refuse it.
        # Without it the "lane freed" assertion below would pass on a
        # repository that never had a lease.
        cur.execute(
            "SELECT ok FROM backlog_lease_acquire('repo:repo-gb', 'planner-t', 3600)"
        )
        assert cur.fetchone()[0]
    # A payload that reaches the TRANSITION arm (0009 requires status
    # `completed` before it will try READY_TO_REVIEW); `backlog_transition`
    # is the auditing callee whose refusal must survive.
    _complete_latest(
        app_factory,
        worker,
        "VOYN-W0-GB",
        {"status": "completed", "pr_url": "https://x/pull/2", "head_sha": "def456"},
    )

    with app_factory() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
        rows = {r[0]: (r[2], r[3]) for r in cur.fetchall()}

    # The healthy task is ingested in the SAME tick that refuses the gate.
    assert rows["VOYN-W0-GA"][0] == "ready_to_review"
    assert store.get_task("VOYN-W0-GA")["status"] == "READY_TO_REVIEW"

    action, detail = rows["VOYN-W0-GB"]
    assert action == "ingest_refused"
    assert detail["refused"] == "gate_is_control_record"
    assert detail["at"] == "transition_refused"
    assert store.get_task("VOYN-W0-GB")["status"] == "IN_PROGRESS"

    assert ("transition", "rejected", "gate_is_control_record") in _events(
        app_factory, "VOYN-W0-GB"
    ), "the callee's denial audit did not survive the caller's refusal"
    assert ("ingest", "rejected", "transition_refused") in _events(
        app_factory, "VOYN-W0-GB"
    )

    # A refused ingest advances nothing, so it must record nothing either:
    # the evidence write moved BEHIND the transition when the abort that used
    # to discard it went away.
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM backlog_evidence WHERE task_id = %s", ("VOYN-W0-GB",)
        )
        assert cur.fetchone()[0] == 0, "a refused ingest left evidence behind"
        # And the lane is FREED rather than wedged for every other task in
        # that repository: the work item is terminal either way, so nothing
        # is running there, and holding the lease because one row cannot be
        # advanced is the same tick-wide damage narrowed to one repo.
        cur.execute(
            "SELECT count(*) FROM backlog_writer_lease WHERE authority = %s",
            ("repo:repo-gb",),
        )
        assert cur.fetchone()[0] == 0, "a refused ingest wedged the lane"
        cur.execute(
            "SELECT detail FROM backlog_event WHERE task_id = %s "
            "AND event = 'ingest' AND outcome = 'rejected'",
            ("VOYN-W0-GB",),
        )
        assert cur.fetchone()[0]["lease_released"] is True


def test_a_dispatch_that_cannot_transition_refuses_as_data_and_leaves_nothing(
    rig, admin_conn
) -> None:
    """The dispatch layer, by fault injection.

    `backlog_dispatch` re-checks OPEN under the row lock, so its transition
    can only refuse if that invariant is broken by a future edit -- which is
    exactly the case the old `RAISE EXCEPTION` was written for, and exactly
    the case in which an operator has nothing but the audit to read. Injected
    rather than left unproven: a defence-in-depth branch that no test can
    reach is a branch a refactor deletes for free.

    Fail-closed is asserted alongside it -- no work item, no lease, status
    unchanged -- because that is what the reordering (transition BEFORE
    enqueue, lease released by a compensating call) buys instead of the abort
    that used to provide it.
    """
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-GC", repo="repo-gc"))[0]
    with admin_conn.cursor() as cur:
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION backlog_transition(
                p_task_id text, p_to_status text, p_expected_revision bigint
            ) RETURNS backlog_verdict
                LANGUAGE plpgsql VOLATILE SECURITY DEFINER
                SET search_path = pg_catalog, public AS $$
            DECLARE v backlog_verdict;
            BEGIN
                PERFORM _backlog_audit(p_task_id, 'transition', 'rejected',
                                       'injected_refusal');
                v.ok := false; v.reason := 'injected_refusal';
                RETURN v;
            END
            $$;
            """
        )

    ok, reason, work_item_id, _revision = _dispatch(app_factory, "VOYN-W0-GC")
    assert not ok and reason == "transition_refused: injected_refusal"
    assert work_item_id is None
    assert store.get_task("VOYN-W0-GC")["status"] == "OPEN"

    with app_factory() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_item WHERE task_id = %s", ("VOYN-W0-GC",))
        assert cur.fetchone()[0] == 0, "a refused dispatch left a work item"
        cur.execute(
            "SELECT count(*) FROM backlog_writer_lease WHERE authority = %s",
            ("repo:repo-gc",),
        )
        assert cur.fetchone()[0] == 0, "a refused dispatch left the lane held"

    events = _events(app_factory, "VOYN-W0-GC")
    assert ("transition", "rejected", "injected_refusal") in events, (
        "the callee's denial audit did not survive the caller's refusal"
    )
    assert ("dispatch", "rejected", "transition_refused") in events


def test_a_dispatch_refusal_does_not_release_a_lease_it_did_not_take(
    rig, admin_conn
) -> None:
    """The compensation must not become a second defect.

    Two tasks in ONE repository: the first is dispatched and holds the lane.
    `backlog_lease_acquire` succeeds for the second as well -- a lease that is
    already ours is renewed, not refused -- so if the second's transition then
    refuses, an unconditional release would hand the FIRST task's repository
    to another writer while its run is still in flight: the two-writer outcome
    the lease exists to prevent, introduced by the fix for a lease leak. The
    release is therefore conditional on this call being what acquired it.
    """
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-D3", repo="repo-shared"))[0]
    assert store.upsert_task(_task("VOYN-W0-D4", repo="repo-shared"))[0]
    assert _dispatch(app_factory, "VOYN-W0-D3", planner="planner-s")[0]

    with admin_conn.cursor() as cur:
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION backlog_transition(
                p_task_id text, p_to_status text, p_expected_revision bigint
            ) RETURNS backlog_verdict
                LANGUAGE plpgsql VOLATILE SECURITY DEFINER
                SET search_path = pg_catalog, public AS $$
            DECLARE v backlog_verdict;
            BEGIN
                v.ok := false; v.reason := 'injected_refusal';
                RETURN v;
            END
            $$;
            """
        )
    ok, reason, *_ = _dispatch(app_factory, "VOYN-W0-D4", planner="planner-s")
    assert not ok and reason == "transition_refused: injected_refusal"

    with app_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT owner FROM backlog_writer_lease WHERE authority = %s",
            ("repo:repo-shared",),
        )
        assert cur.fetchall() == [("planner-s",)], (
            "the refusal released the lease the FIRST dispatch is still using"
        )


def test_a_wedged_row_costs_one_task_and_the_tick_still_dispatches(rig) -> None:
    """The whole-tick damage, at the composition layer.

    The unit of the old defect was not one refusal but one TICK.
    `backlog_ingest_results` loops over every in-flight task, so the
    exception that a single unadvanceable row produced took the healthy
    tasks' ingest with it, and because ingest runs FIRST in `plan_once`, the
    dispatch phase never ran either -- for as long as the poisoned row sat
    there. Assert the composition, not just the function: the wedged row is
    reported by name, the healthy task is still ingested, and the tick still
    dispatches.
    """
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-GA", repo="repo-ga"))[0]
    assert store.upsert_task(_task("VOYN-W0-GB", kind="gate", status="IN_PROGRESS", repo="repo-gb"))[0]

    limits = PlanLimits(planner="planner-t", wip_limit=4, max_dispatches_per_tick=4)
    assert [t for t, _ in plan_once(app_factory, limits).dispatched] == ["VOYN-W0-GA"]
    _complete_latest(
        app_factory,
        worker,
        "VOYN-W0-GA",
        {"status": "completed", "pr_url": "https://x/pull/1", "head_sha": "abc123"},
    )
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT queue_enqueue('execution', %s, '{}'::jsonb, %s, %s, 3)",
            ("gate-item", "VOYN-W0-GB", "repo-gb"),
        )
    _complete_latest(
        app_factory,
        worker,
        "VOYN-W0-GB",
        {"status": "completed", "pr_url": "https://x/pull/2", "head_sha": "def456"},
    )

    # A third task, OPEN, dispatchable only if the tick gets past ingest.
    assert store.upsert_task(_task("VOYN-W0-TT", repo="repo-tt"))[0]

    report = plan_once(app_factory, limits)
    assert report.ingested == [("VOYN-W0-GA", "ready_to_review")]
    assert report.ingest_refused == [("VOYN-W0-GB", "gate_is_control_record")], (
        "the unadvanceable row must be named in the report, not buried in "
        "`ingested` and not thrown as an exception"
    )
    assert [t for t, _ in report.dispatched] == ["VOYN-W0-TT"], (
        "the dispatch phase did not run after a refused ingest"
    )

    # And it is stable: a second tick sees the same wedged row, reports it
    # again, and still does the rest of its work.
    assert plan_once(app_factory, limits).ingest_refused == [
        ("VOYN-W0-GB", "gate_is_control_record")
    ]
