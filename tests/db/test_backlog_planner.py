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

    repos = ["repo-d2","repo-pipe","repo-ga","repo-gb","repo-gc","repo-in","repo-nm",
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


def _dispatch(
    app_factory, task_id, planner="planner-t", wip=4, payload=None, max_attempts=3
):
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM backlog_dispatch(%s, %s, 3600, %s, %s::jsonb, %s)",
                (
                    task_id,
                    planner,
                    wip,
                    json.dumps(payload or {"kind": "agent_run"}),
                    max_attempts,
                ),
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


def test_ingest_a_clean_run_with_no_pr_returns_to_pool_not_review(rig) -> None:
    """VOYN-W0-AICC-INGEST-REQUIRES-REAL-PR-NOT-JUST-COMPLETED. Proven live
    2026-08-21: the agent process exited cleanly (`status: completed`) but
    `publish_run`'s own `git push` failed underneath it (a stale writer
    lease), so `pr_url` never arrived. `status='completed'` alone used to be
    enough to reach READY_TO_REVIEW -- but review_once/publish_review_
    verdicts/merge_once all `JOIN backlog_evidence ON kind = 'pr'`, so a
    task with no `pr` evidence reaches READY_TO_REVIEW and then sits there
    invisibly forever, never even reviewed, let alone blocked at a later
    DONE gate. A clean run that never got published is exactly as exhausted
    an attempt as a failed one and belongs on the same cascade-exhaustion
    path."""
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-NM", repo="repo-nm"))[0]
    assert _dispatch(app_factory, "VOYN-W0-NM")[0]
    _complete_latest(app_factory, worker, "VOYN-W0-NM", {"status": "completed"})
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
            rows = cur.fetchall()
    assert [(r[0], r[2]) for r in rows] == [("VOYN-W0-NM", "returned_to_pool")]
    task = store.get_task("VOYN-W0-NM")
    assert task["status"] == "OPEN"
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM backlog_evidence WHERE task_id = %s",
                ("VOYN-W0-NM",),
            )
            assert cur.fetchone()[0] == 0


def test_repeated_no_pr_publish_failure_stays_operational(rig) -> None:
    """A second technical publish failure is still an operations retry, not
    an owner decision.  Migration 0012 deliberately exempts no_pr_published
    from the two-epoch DEFER_TO_USER circuit breaker."""
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-N2", repo="repo-nm"))[0]

    for round_no in (1, 2):
        assert _dispatch(app_factory, "VOYN-W0-N2")[0], f"round {round_no}"
        _complete_latest(
            app_factory,
            worker,
            "VOYN-W0-N2",
            {"status": "completed"},
        )
        with app_factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
                rows = cur.fetchall()
        assert [(r[0], r[2]) for r in rows] == [
            ("VOYN-W0-N2", "returned_to_pool")
        ]
        assert store.get_task("VOYN-W0-N2")["status"] == "OPEN", rows


def test_repeated_guarded_publish_prep_failure_stays_operational(rig) -> None:
    """VOYN-W0-AICC-DEFER-RESUME-COVER-PUBLISH-PREP: a guarded publish
    preparation failure (worker/handlers.py's WorkspaceVerificationError,
    e.g. `agent_worktree_clean` finding `uncommitted_changes`) reaches the
    dead-letter path wrapped in queue_fail's own `max_attempts_exhausted:`
    label (0002_queue_claim.sql), which reaches backlog_return_to_pool as
    `cascade_exhausted: max_attempts_exhausted: guarded publish preparation
    failed at agent_worktree_clean: uncommitted_changes: ...`. Migration
    0012's allowlist did not name this shape, so a SECOND occurrence used to
    park the task in DEFER_TO_USER as if it were an owner decision even
    though it is exactly the same kind of operational retry no_pr_published
    already gets exempted from. 0017 fixes this by recognizing any
    `max_attempts_exhausted:` cascade cause as technical."""
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-GP", repo="repo-nm"))[0]

    reason = (
        "guarded publish preparation failed at agent_worktree_clean: "
        "uncommitted_changes: M some_file.py"
    )
    for round_no in (1, 2):
        assert _dispatch(app_factory, "VOYN-W0-GP", max_attempts=1)[0], f"round {round_no}"
        claimed = worker.claim("execution", visibility_seconds=60)
        assert isinstance(claimed, ClaimedWork)
        assert worker.fail(claimed, reason=reason, retryable=True)
        with app_factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
                rows = cur.fetchall()
        assert [(r[0], r[2]) for r in rows] == [("VOYN-W0-GP", "returned_to_pool")]
        assert store.get_task("VOYN-W0-GP")["status"] == "OPEN", rows

    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reason FROM backlog_event WHERE task_id = %s "
                "AND event = 'return_to_pool' AND outcome = 'granted' ORDER BY event_id",
                ("VOYN-W0-GP",),
            )
            reasons = [r[0] for r in cur.fetchall()]
    assert len(reasons) == 2
    assert all(r.startswith("cascade_exhausted: max_attempts_exhausted:") for r in reasons)


def test_ingest_a_clean_run_with_sha_but_no_pr_still_returns_to_pool(rig) -> None:
    """The exact live shape of the 2026-08-21 incident: status='completed'
    AND a real head_sha (the agent reported its own HEAD_SHA trailer), but
    publish still failed so pr_url is null. sha alone is not evidence a
    review can act on -- return_to_pool, same as no evidence at all."""
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-SO", repo="repo-so"))[0]
    assert _dispatch(app_factory, "VOYN-W0-SO")[0]
    _complete_latest(
        app_factory, worker, "VOYN-W0-SO", {"status": "completed", "head_sha": "cafef00d"}
    )
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
            rows = cur.fetchall()
    assert [(r[0], r[2]) for r in rows] == [("VOYN-W0-SO", "returned_to_pool")]
    task = store.get_task("VOYN-W0-SO")
    assert task["status"] == "OPEN"


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


def test_migration_0017_is_reversible_without_residue(pg_connection_factory) -> None:
    """Live up->down->up pins the broadened technical-park classifier to
    migration 0017. Downgrading to 0016 must restore 0012's narrower
    allowlist (no `max_attempts_exhausted:` catch-all), and the second
    upgrade must reapply it."""
    from command_center.db import migrations

    with pg_connection_factory() as conn:
        migrations.upgrade(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_return_to_pool'"
            )
            assert "max_attempts_exhausted" in cur.fetchone()[0]
        migrations.downgrade(conn, target=16)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_return_to_pool'"
            )
            assert "max_attempts_exhausted" not in cur.fetchone()[0]
        migrations.upgrade(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_return_to_pool'"
            )
            assert "max_attempts_exhausted" in cur.fetchone()[0]


def test_migration_0012_is_reversible_without_residue(pg_connection_factory) -> None:
    """Live up->down->up pins the technical-failure policy to migration
    0012.  Downgrading to 0011 must restore the owner-defer definition, and
    the second upgrade must reapply the operational retry classification."""
    from command_center.db import migrations

    with pg_connection_factory() as conn:
        migrations.upgrade(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_return_to_pool'"
            )
            assert "v_technical" in cur.fetchone()[0]
        migrations.downgrade(conn, target=11)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_return_to_pool'"
            )
            assert "v_technical" not in cur.fetchone()[0]
        migrations.upgrade(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_return_to_pool'"
            )
            assert "v_technical" in cur.fetchone()[0]


def test_migration_0011_is_reversible_without_residue(pg_connection_factory) -> None:
    """Live up->down->up on the exact function body, same pin as 0009's own
    test: down restores 0009's (pr-not-required) definition, up reapplies
    0011's fix -- so a future no-op down (CREATE FUNCTION, not OR REPLACE)
    breaks the second up loudly instead of leaving stale behaviour
    undetected."""
    from command_center.db import migrations

    with pg_connection_factory() as conn:
        migrations.upgrade(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_ingest_results'"
            )
            assert "no_pr_published" in cur.fetchone()[0]
        migrations.downgrade(conn, target=10)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_ingest_results'"
            )
            assert "no_pr_published" not in cur.fetchone()[0]
        migrations.upgrade(conn)  # must not raise 'already exists'
        with conn.cursor() as cur:
            cur.execute(
                "SELECT prosrc FROM pg_proc WHERE proname = 'backlog_ingest_results'"
            )
            assert "no_pr_published" in cur.fetchone()[0]


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


# --- VOYN-W0-AICC-DEFER-AUTO-RESUME (0014): the machine exit from parks -----


def _park_technically(app_factory, store, worker, task_id) -> None:
    """Two cascade exhaustions through the real machine: the first returns to
    OPEN (a finding), the second parks in DEFER_TO_USER -- exactly the
    technical park 0014 exists to drain."""
    assert store.upsert_task(_task(task_id))[0]
    for expected in ("OPEN", "DEFER_TO_USER"):
        assert _dispatch(app_factory, task_id)[0]
        claimed = worker.claim("execution", visibility_seconds=60)
        assert isinstance(claimed, ClaimedWork)
        assert worker.fail(claimed, reason="boom", retryable=False)
        with app_factory() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
        assert store.get_task(task_id)["status"] == expected


def _repark(app_factory, store, task_id, reason) -> None:
    """OPEN -> IN_PROGRESS -> DEFER_TO_USER again, via the real
    return_to_pool (prior granted returns >= 1, non-allowlisted reason)."""
    assert _dispatch(app_factory, task_id)[0]
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ok FROM backlog_return_to_pool(%s, %s)", (task_id, reason))
            assert cur.fetchone()[0]
    assert store.get_task(task_id)["status"] == "DEFER_TO_USER"


def test_resume_deferred_returns_a_technical_park_to_open(rig) -> None:
    app_factory, store, worker = rig
    _park_technically(app_factory, store, worker, "VOYN-W0-RS")

    ok, reason, revision = store.resume_deferred("VOYN-W0-RS")
    assert ok and reason == "OPEN" and revision is not None
    assert store.get_task("VOYN-W0-RS")["status"] == "OPEN"

    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reason, detail FROM backlog_event WHERE task_id = %s "
                "AND event = 'resume_deferred' AND outcome = 'granted'",
                ("VOYN-W0-RS",),
            )
            rows = cur.fetchall()
    assert len(rows) == 1
    # The grant records the ORIGINAL park reason -- "why did this come back"
    # stays answerable from the audit alone.
    assert rows[0][0].startswith("cascade_exhausted")
    assert rows[0][1]["prior_resumes"] == 0


def test_resume_deferred_refuses_an_owner_decision_park(rig) -> None:
    """A park whose latest parking reason is NOT a technical cascade
    exhaustion stays parked: an owner decision is never auto-lifted."""
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-RO"))[0]
    owner_reason = "owner must choose the product direction"
    # First return targets OPEN (prior=0); the second, with a prior granted
    # return on record and a non-technical reason, parks.
    assert _dispatch(app_factory, "VOYN-W0-RO")[0]
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ok FROM backlog_return_to_pool(%s, %s)", ("VOYN-W0-RO", owner_reason))
            assert cur.fetchone()[0]
    _repark(app_factory, store, "VOYN-W0-RO", owner_reason)

    ok, reason, _revision = store.resume_deferred("VOYN-W0-RO")
    assert (ok, reason) == (False, "owner_decision_park")
    assert store.get_task("VOYN-W0-RO")["status"] == "DEFER_TO_USER"


def test_resume_deferred_refuses_a_park_without_machine_evidence(rig) -> None:
    """Imported or hand-upserted DEFER_TO_USER rows have no machine park
    event: provenance unknown, treated as an owner decision. Fail closed."""
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-RN", status="DEFER_TO_USER"))[0]
    ok, reason, _revision = store.resume_deferred("VOYN-W0-RN")
    assert (ok, reason) == (False, "no_machine_park_evidence")
    assert store.get_task("VOYN-W0-RN")["status"] == "DEFER_TO_USER"


def test_resume_deferred_budget_is_bounded(rig) -> None:
    """Three granted resumes are the budget; the fourth attempt refuses --
    a task that re-parks every time it runs is a fact for the owner, not
    fuel for an infinite resume/exhaust loop."""
    app_factory, store, worker = rig
    _park_technically(app_factory, store, worker, "VOYN-W0-RB")

    for round_no in range(3):
        ok, reason, _rev = store.resume_deferred("VOYN-W0-RB")
        assert ok, (round_no, reason)
        _repark(app_factory, store, "VOYN-W0-RB", "cascade_exhausted: synthetic re-park")

    ok, reason, _rev = store.resume_deferred("VOYN-W0-RB")
    assert (ok, reason) == (False, "resume_budget_exhausted")
    assert store.get_task("VOYN-W0-RB")["status"] == "DEFER_TO_USER"


def test_resume_deferred_refuses_everything_else(rig) -> None:
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-RX"))[0]  # OPEN
    assert store.resume_deferred("VOYN-W0-RX")[:2] == (False, "not_deferred")
    assert store.upsert_task(_task("VOYN-W0-RG2", kind="gate", status="DEFER_TO_USER"))[0]
    assert store.resume_deferred("VOYN-W0-RG2")[:2] == (False, "gate_is_control_record")
    assert store.resume_deferred("VOYN-W0-NOPE")[:2] == (False, "unknown_task")


def test_plan_once_reconciles_technical_parks_without_audit_spam(rig) -> None:
    """The planner tick resumes eligible technical parks (bounded) and never
    even ATTEMPTS ineligible ones -- an owner park must not accrete a
    rejected `resume_deferred` audit row on every 5-minute tick."""
    app_factory, store, worker = rig
    _park_technically(app_factory, store, worker, "VOYN-W0-RP")
    assert store.upsert_task(_task("VOYN-W0-RQ", status="DEFER_TO_USER"))[0]

    report = plan_once(app_factory, PlanLimits(wip_limit=4))
    assert not report.planner_busy
    resumed_ids = [task_id for task_id, _reason in report.resumed]
    assert "VOYN-W0-RP" in resumed_ids
    assert "VOYN-W0-RQ" not in resumed_ids
    assert store.get_task("VOYN-W0-RQ")["status"] == "DEFER_TO_USER"
    assert store.get_task("VOYN-W0-RP")["status"] in ("OPEN", "IN_PROGRESS")

    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM backlog_event WHERE task_id = %s "
                "AND event = 'resume_deferred'",
                ("VOYN-W0-RQ",),
            )
            assert cur.fetchone()[0] == 0

    # A zero cap disables the reconcile entirely.
    _park_technically(app_factory, store, worker, "VOYN-W0-RZ")
    report = plan_once(app_factory, PlanLimits(wip_limit=4, max_resumes_per_tick=0))
    assert report.resumed == []
    assert store.get_task("VOYN-W0-RZ")["status"] == "DEFER_TO_USER"


# --- review_backlog_limit: dispatch-only backpressure, not a whole-tick gate


def _ready_to_review_with_pr(app_factory, store, task_id, pr_url) -> None:
    """A READY_TO_REVIEW task carrying `pr` evidence -- what
    `review_backlog_limit` counts."""
    assert store.upsert_task(_task(task_id, repo="repo-d2", status="OPEN"))[0]
    with app_factory() as conn:
        with conn.cursor() as cur:
            def _rev():
                cur.execute(
                    "SELECT revision FROM backlog_task WHERE task_id=%s", (task_id,)
                )
                return cur.fetchone()[0]
            cur.execute(
                "SELECT ok FROM backlog_transition(%s,'IN_PROGRESS',%s)",
                (task_id, _rev()),
            )
            cur.execute(
                "SELECT backlog_record_evidence(%s,'pr',%s)", (task_id, pr_url)
            )
            cur.execute(
                "SELECT ok FROM backlog_transition(%s,'READY_TO_REVIEW',%s)",
                (task_id, _rev()),
            )


def test_review_backlog_fence_pauses_dispatch_but_not_resume_reconcile(rig) -> None:
    """VOYN-W0-AICC-PR-WINDOW-RECONCILER-REM: an earlier version of this
    fence was a blanket `return report` placed ABOVE the DEFER_TO_USER
    resume reconcile, so a full review backlog silently froze parked-task
    recovery too -- a control whose blast radius (the whole rest of the
    tick) was wider than its stated purpose (gate new dispatch). This pins
    that ingest and resume both still happen when the fence trips, and only
    the dispatch loop is skipped."""
    app_factory, store, worker = rig
    _ready_to_review_with_pr(
        app_factory, store, "VOYN-W0-BL1", "https://github.com/x/repo-d2/pull/101"
    )
    _ready_to_review_with_pr(
        app_factory, store, "VOYN-W0-BL2", "https://github.com/x/repo-d2/pull/102"
    )
    _park_technically(app_factory, store, worker, "VOYN-W0-BL3")
    assert store.upsert_task(_task("VOYN-W0-BL4", repo="repo-d2"))[0]  # OPEN
    # A lane is busy, so the fence is real backpressure here (with every lane
    # idle the tick would dispatch anyway -- see
    # test_idle_lanes_dispatch_through_the_review_backlog_fence).
    assert store.upsert_task(_task("VOYN-W0-TT", repo="repo-tt"))[0]
    assert _dispatch(app_factory, "VOYN-W0-TT")[0]

    report = plan_once(app_factory, PlanLimits(wip_limit=4, review_backlog_limit=2))

    assert report.review_window_full == 2
    assert report.idle_trickle is False
    assert report.dispatched == []
    # BL4, plus BL3 once the resume reconcile above returned it to OPEN in
    # this same tick: both are functional candidates the fence held.
    assert report.fenced >= 1
    assert "VOYN-W0-BL3" in [task_id for task_id, _reason in report.resumed]
    assert store.get_task("VOYN-W0-BL3")["status"] in ("OPEN", "IN_PROGRESS")
    # The candidate loop never ran at all -- the OPEN task the fence was
    # supposed to hold back stays exactly where it was, not merely un-
    # dispatched-but-examined.
    assert store.get_task("VOYN-W0-BL4")["status"] == "OPEN"


def test_review_backlog_limit_zero_disables_the_fence(rig) -> None:
    """0 disables, matching `max_resumes_per_tick`'s convention on this same
    dataclass -- not silently coerced to a threshold of 1."""
    app_factory, store, _worker = rig
    _ready_to_review_with_pr(
        app_factory, store, "VOYN-W0-BL5", "https://github.com/x/repo-d2/pull/103"
    )
    assert store.upsert_task(_task("VOYN-W0-BL6", repo="repo-d2"))[0]

    report = plan_once(app_factory, PlanLimits(wip_limit=4, review_backlog_limit=0))

    assert report.review_window_full is None
    assert "VOYN-W0-BL6" in [task_id for task_id, _work_item in report.dispatched]


def test_resume_deferred_refuses_stale_park_evidence(rig) -> None:
    """Independent review of PR #401 at 2bc73ac: a task technically parked,
    later resumed, and then hand-upserted BACK into DEFER_TO_USER (an owner
    decision with no return_to_pool event) still carries its old technical
    park event -- which must NOT reopen it. Any granted mutating event after
    the park event supersedes the evidence: fail closed."""
    app_factory, store, worker = rig
    _park_technically(app_factory, store, worker, "VOYN-W0-RSS")

    ok, reason, _rev = store.resume_deferred("VOYN-W0-RSS")
    assert ok and reason == "OPEN"

    # The owner hand-parks it again -- via upsert, the only path that sets
    # DEFER_TO_USER without a return_to_pool event.
    assert store.upsert_task(_task("VOYN-W0-RSS", status="DEFER_TO_USER"))[0]

    ok, reason, _rev = store.resume_deferred("VOYN-W0-RSS")
    assert (ok, reason) == (False, "superseded_park_evidence")
    assert store.get_task("VOYN-W0-RSS")["status"] == "DEFER_TO_USER"

    # And the planner filter mirrors the gate: the task is never attempted,
    # so the refusal above stays the ONLY superseded audit row.
    report = plan_once(app_factory, PlanLimits(wip_limit=4))
    assert "VOYN-W0-RSS" not in [task_id for task_id, _ in report.resumed]
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM backlog_event WHERE task_id = %s "
                "AND event = 'resume_deferred' AND outcome = 'rejected' "
                "AND reason = 'superseded_park_evidence'",
                ("VOYN-W0-RSS",),
            )
            assert cur.fetchone()[0] == 1


def test_resume_budget_is_a_window_not_a_lifetime_score(
    rig, admin_conn
) -> None:
    """0017 (VOYN-W0-AICC-DEFER-AUTO-RESUME-REM): three granted resumes
    OLDER than the 48h window must not exhaust the budget — a fixed
    pipeline reclaims its old parks; three recent ones still refuse.

    Grants are seeded through the real machine (park -> resume cycles),
    never by raw INSERT: no role holds INSERT on backlog tables by design
    (0005), and a grant fabricated after the park would trip the unchanged
    superseded_park_evidence check anyway (independent review of 29d2152,
    findings 1-2). Only created_at is backdated, via the admin connection —
    the one property 0017's window reads."""
    app_factory, store, worker = rig
    task = "VOYN-W0-RSW"

    # Three real park->resume cycles: the first park needs the fresh-task
    # double exhaustion, every later one goes straight to DEFER via the
    # repark path (prior granted returns >= 1 — live-confirmed by the
    # independent verification of 4af6832 on real PostgreSQL). Each
    # grant's event_id precedes the next park, so superseded_park_evidence
    # never trips.
    _park_technically(app_factory, store, worker, task)
    ok, reason, _ = store.resume_deferred(task)
    assert ok and reason == "OPEN"
    for _ in range(2):
        _repark(app_factory, store, task, "cascade_exhausted: again")
        ok, reason, _ = store.resume_deferred(task)
        assert ok and reason == "OPEN"
    _repark(app_factory, store, task, "cascade_exhausted: again")

    # Lifetime budget is now spent (3 grants). Prove the OLD behaviour is
    # gone by aging those grants out of the window.
    with admin_conn.cursor() as cur:
        cur.execute(
            "UPDATE backlog_event SET created_at = now() - interval '3 days' "
            "WHERE task_id = %s AND event = 'resume_deferred' "
            "AND outcome = 'granted'",
            (task,),
        )
        assert cur.rowcount == 3
    admin_conn.commit()

    ok, reason, _ = store.resume_deferred(task)
    assert ok and reason == "OPEN", (
        "stale resume history must not bury the task forever"
    )

    # Three RECENT grants (the one above plus two more cycles) refuse the
    # fourth — the window still stops a park that re-arms itself.
    for _ in range(2):
        _repark(app_factory, store, task, "cascade_exhausted: again")
        ok, reason, _ = store.resume_deferred(task)
        assert ok and reason == "OPEN"
    _repark(app_factory, store, task, "cascade_exhausted: again")

    ok, reason, _ = store.resume_deferred(task)
    assert not ok and reason == "resume_budget_exhausted"


# ---------------------------------------------------------------------------
# VOYN-W0-AICC-NO-RECOVERY-PATH-STUCK-READY-TO-REVIEW (0018): a sanctioned
# recovery path for a task stuck in READY_TO_REVIEW with no `pr` evidence --
# invisible to both backlog_transition (no READY_TO_REVIEW -> OPEN move) and
# backlog_return_to_pool (IN_PROGRESS only).
# ---------------------------------------------------------------------------


def _stick_in_review_without_pr_evidence(app_factory, store, task_id) -> None:
    """Reproduce the stuck state directly through the machine: dispatch to
    IN_PROGRESS, then transition straight to READY_TO_REVIEW with no
    evidence recorded at all -- the exact shape 0011 stopped `backlog_
    ingest_results` from producing, and the shape any future bug in a
    different corner of the same pipeline could still produce. `backlog_
    transition`'s READY_TO_REVIEW move itself carries no evidence
    requirement (only the DONE move does), so this is a legitimate machine
    path, not a raw INSERT bypassing it."""
    assert _dispatch(app_factory, task_id)[0]
    task = store.get_task(task_id)
    ok, reason, _rev = store.transition(task_id, "READY_TO_REVIEW", task["revision"])
    assert ok, reason
    assert store.get_task(task_id)["status"] == "READY_TO_REVIEW"


def test_recover_stuck_ready_to_review_returns_evidence_free_task_to_open(rig) -> None:
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-SK", repo="repo-sk"))[0]
    _stick_in_review_without_pr_evidence(app_factory, store, "VOYN-W0-SK")

    ok, reason, revision = store.recover_stuck_ready_to_review("VOYN-W0-SK")
    assert ok and reason == "OPEN" and revision is not None
    assert store.get_task("VOYN-W0-SK")["status"] == "OPEN"

    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reason, detail FROM backlog_event WHERE task_id = %s "
                "AND event = 'recover_stuck_ready_to_review' AND outcome = 'granted'",
                ("VOYN-W0-SK",),
            )
            rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "no_pr_evidence"
    assert rows[0][1] == {"from": "READY_TO_REVIEW", "to": "OPEN"}

    # OPEN means a fresh dispatch is possible again.
    assert _dispatch(app_factory, "VOYN-W0-SK")[0]


def test_recover_stuck_ready_to_review_refuses_a_task_with_pr_evidence(rig) -> None:
    """A READY_TO_REVIEW task that DOES carry `pr` evidence is genuinely
    reviewable: this recovery path must leave it alone for the real
    review/merge machinery."""
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-SP", repo="repo-sp"))[0]
    assert _dispatch(app_factory, "VOYN-W0-SP")[0]
    _complete_latest(
        app_factory,
        worker,
        "VOYN-W0-SP",
        {"status": "completed", "pr_url": "https://github.com/o/r/pull/9",
         "head_sha": "deadbeef"},
    )
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
    assert store.get_task("VOYN-W0-SP")["status"] == "READY_TO_REVIEW"

    ok, reason, _rev = store.recover_stuck_ready_to_review("VOYN-W0-SP")
    assert (ok, reason) == (False, "has_pr_evidence")
    assert store.get_task("VOYN-W0-SP")["status"] == "READY_TO_REVIEW"


def test_recover_stuck_ready_to_review_refuses_everything_else(rig) -> None:
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-SX"))[0]  # OPEN
    assert store.recover_stuck_ready_to_review("VOYN-W0-SX")[:2] == (
        False, "not_ready_to_review",
    )
    assert store.upsert_task(
        _task("VOYN-W0-SG", kind="gate", status="READY_TO_REVIEW")
    )[0]
    assert store.recover_stuck_ready_to_review("VOYN-W0-SG")[:2] == (
        False, "gate_is_control_record",
    )
    assert store.recover_stuck_ready_to_review("VOYN-W0-NOPE")[:2] == (
        False, "unknown_task",
    )


def _mark_ready_to_review_with_pr(app_factory, task_id: str) -> None:
    with app_factory() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM backlog_record_evidence(%s, 'pr', %s)",
            (task_id, f"https://github.com/voyn88/x/pull/{secrets.randbelow(10**6)}"),
        )


def test_pipeline_class_tasks_pass_the_review_backlog_fence(rig, admin_conn) -> None:
    """VOYN-W0-AICC-PLANNER-PIPELINE-CLASS-PRIORITY-AND-WINDOW-PAUSE: with the
    review backlog at the limit and a lane busy, a functional candidate is
    held (backpressure) while a pipeline-class candidate is dispatched --
    the fix for the backlog must never wait behind the backlog."""
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-RV", status="READY_TO_REVIEW", repo="repo-rv"))[0]
    _mark_ready_to_review_with_pr(app_factory, "VOYN-W0-RV")
    assert store.upsert_task(_task("VOYN-W0-TT", repo="repo-tt"))[0]  # keeps a lane busy
    assert _dispatch(app_factory, "VOYN-W0-TT")[0]
    assert store.upsert_task(_task("VOYN-W0-P1", repo="repo-p1"))[0]  # functional
    assert store.upsert_task(_task("VOYN-W0-P3", repo="repo-p3"))[0]  # pipeline
    with admin_conn.cursor() as cur:
        cur.execute(
            "UPDATE backlog_task SET task_class = 'pipeline' WHERE task_id = %s",
            ("VOYN-W0-P3",),
        )
        admin_conn.commit()
        cur.execute("SELECT task_id FROM backlog_eligible")
        order = [row[0] for row in cur.fetchall()]
    assert order.index("VOYN-W0-P3") < order.index("VOYN-W0-P1"), (
        "same wave and priority: the pipeline task is offered first"
    )

    limits = PlanLimits(planner="planner-fence", review_backlog_limit=1)
    report = plan_once(app_factory, limits)
    assert report.review_window_full == 1
    assert report.idle_trickle is False
    assert [t for t, _ in report.dispatched] == ["VOYN-W0-P3"]
    assert report.pipeline_bypass == ["VOYN-W0-P3"]
    assert report.fenced == 1


def test_idle_lanes_dispatch_through_the_review_backlog_fence(rig) -> None:
    """The fence is backpressure for busy lanes, not a reason to idle the
    fleet: with no execution work item ready or claimed, the tick dispatches
    its ordinary bounded batch even though the backlog is at the limit."""
    app_factory, store, _worker = rig
    assert store.upsert_task(_task("VOYN-W0-RV", status="READY_TO_REVIEW", repo="repo-rv"))[0]
    _mark_ready_to_review_with_pr(app_factory, "VOYN-W0-RV")
    assert store.upsert_task(_task("VOYN-W0-P1", repo="repo-p1"))[0]

    limits = PlanLimits(planner="planner-idle", review_backlog_limit=1)
    report = plan_once(app_factory, limits)
    assert report.review_window_full == 1
    assert report.idle_trickle is True
    assert [t for t, _ in report.dispatched] == ["VOYN-W0-P1"]
    assert report.fenced == 0


def _set_pipeline(app_factory, task_id: str) -> None:
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT backlog_set_task_class(%s, 'pipeline')", (task_id,))


def _return_non_technical(app_factory, task_id: str) -> str:
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT reason FROM backlog_return_to_pool(%s, %s)",
                (task_id, "cascade_exhausted: agent gave up (too large)"),
            )
            return cur.fetchone()[0]


def test_pipeline_task_returned_twice_is_split_not_parked(rig) -> None:
    """0021: a pipeline-class task returned twice without a technical cause
    stays OPEN with split_requested (the planner then dispatches a
    decomposition run); the fourth return still parks. A functional task
    keeps the original second-return park."""
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-PIPE", repo="repo-pipe"))[0]
    _set_pipeline(app_factory, "VOYN-W0-PIPE")
    assert _dispatch(app_factory, "VOYN-W0-PIPE")[0]
    assert _return_non_technical(app_factory, "VOYN-W0-PIPE") == "OPEN"
    assert _dispatch(app_factory, "VOYN-W0-PIPE")[0]
    assert _return_non_technical(app_factory, "VOYN-W0-PIPE") == "OPEN"
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT (detail->>'split_requested')::boolean FROM backlog_event "
                "WHERE task_id = %s AND event = 'return_to_pool' AND outcome = 'granted' "
                "ORDER BY event_id DESC LIMIT 1",
                ("VOYN-W0-PIPE",),
            )
            assert cur.fetchone()[0] is True
    from command_center.orchestrator.planner import Planner, _split_requested

    assert _split_requested(Planner(app_factory)._rows, "VOYN-W0-PIPE") is True
    # The PLANNER's next dispatch of this task is a decomposition run: the
    # payload carries the split instructions and the SPLIT_TASKS_JSON
    # trailer contract, not an ordinary implementation prompt (review of
    # fc167cf7: the earlier assertion only checked the flag, so a planner
    # that ignored it during planning still passed).
    from command_center.orchestrator.planner import _SPLIT_INSTRUCTIONS, PlanLimits

    plan = Planner(app_factory).plan_once(PlanLimits(planner="planner-t"))
    assert "VOYN-W0-PIPE" in plan.split_dispatched, plan
    with app_factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT payload FROM work_item WHERE task_id = %s ORDER BY created_at DESC LIMIT 1",
            ("VOYN-W0-PIPE",),
        )
        payload = cur.fetchone()[0]
    assert "SPLIT_TASKS_JSON" in payload["prompt"]
    assert _SPLIT_INSTRUCTIONS.strip()[:40] in payload["prompt"]
    # Third and fourth returns: still open once more, then parked.
    assert _return_non_technical(app_factory, "VOYN-W0-PIPE") == "OPEN"
    assert _dispatch(app_factory, "VOYN-W0-PIPE")[0]
    assert _return_non_technical(app_factory, "VOYN-W0-PIPE") == "DEFER_TO_USER"
    # Functional control: second return parks as before.
    assert store.upsert_task(_task("VOYN-W0-FUNC", repo="repo-func"))[0]
    assert _dispatch(app_factory, "VOYN-W0-FUNC")[0]
    assert _return_non_technical(app_factory, "VOYN-W0-FUNC") == "OPEN"
    assert _dispatch(app_factory, "VOYN-W0-FUNC")[0]
    assert _return_non_technical(app_factory, "VOYN-W0-FUNC") == "DEFER_TO_USER"


def test_split_trailer_creates_bounded_children_and_closes_the_parent(rig) -> None:
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-BIG", repo="repo-big", priority="P1"))[0]
    _set_pipeline(app_factory, "VOYN-W0-BIG")
    assert _dispatch(app_factory, "VOYN-W0-BIG")[0]
    trailer = (
        "I decomposed the task.\n"
        'SPLIT_TASKS_JSON: [{"suffix": "s1-gate", "title": "Gate the CI workflows", '
        '"body": "Add the job-level guard and the concurrency suffix; tests in policy file."}, '
        '{"suffix": "S2-RECONCILER", "title": "Order accepted PRs first", '
        '"body": "Accepted-but-unmerged PRs enter the window first; unit tests.", "priority": "P0"}]'
    )
    _complete_latest(
        app_factory, worker, "VOYN-W0-BIG",
        {"status": "completed", "result_text": trailer},
    )
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
            rows = cur.fetchall()
    assert [(r[0], r[2]) for r in rows] == [("VOYN-W0-BIG", "split")]
    assert store.get_task("VOYN-W0-BIG")["status"] == "SPLIT"
    c1 = store.get_task("VOYN-W0-BIG-S1-GATE")
    c2 = store.get_task("VOYN-W0-BIG-S2-RECONCILER")
    assert c1["status"] == "OPEN" and c1["priority"] == "P1" and c1["repo"] == "repo-big"
    assert c2["priority"] == "P0"
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT depends_on_task_id FROM backlog_dependency WHERE task_id = %s ORDER BY 1",
                ("VOYN-W0-BIG",),
            )
            assert [r[0] for r in cur.fetchall()] == [
                "VOYN-W0-BIG-S1-GATE", "VOYN-W0-BIG-S2-RECONCILER"
            ]
            cur.execute(
                "SELECT task_class FROM backlog_task WHERE task_id = %s", ("VOYN-W0-BIG-S1-GATE",)
            )
            assert cur.fetchone()[0] == "pipeline"
            # Children are eligible; the parent is not (SPLIT).
            cur.execute("SELECT task_id FROM backlog_eligible WHERE task_id LIKE 'VOYN-W0-BIG%'")
            assert sorted(r[0] for r in cur.fetchall()) == [
                "VOYN-W0-BIG-S1-GATE", "VOYN-W0-BIG-S2-RECONCILER"
            ]


def test_malformed_split_trailer_creates_nothing_and_returns_to_pool(rig) -> None:
    app_factory, store, worker = rig
    assert store.upsert_task(_task("VOYN-W0-BAD", repo="repo-bad"))[0]
    _set_pipeline(app_factory, "VOYN-W0-BAD")
    assert _dispatch(app_factory, "VOYN-W0-BAD")[0]
    _complete_latest(
        app_factory, worker, "VOYN-W0-BAD",
        {"status": "completed",
         "result_text": 'SPLIT_TASKS_JSON: [{"suffix": "bad suffix!", "title": "x", "body": "y"}]'},
    )
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM backlog_ingest_results(%s)", ("planner-t",))
            rows = cur.fetchall()
            cur.execute("SELECT count(*) FROM backlog_task WHERE task_id LIKE 'VOYN-W0-BAD-%'")
            assert cur.fetchone()[0] == 0
    assert rows[0][2] in ("returned_to_pool", "parked_for_owner")
    assert store.get_task("VOYN-W0-BAD")["status"] in ("OPEN", "DEFER_TO_USER")


def test_dispatch_smoke_reads_with_dispatch_privileges(rig) -> None:
    app_factory, _store, _worker = rig
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT backlog_dispatch_smoke()")
            assert cur.fetchone()[0] is True


def test_open_monitor_findings_become_pipeline_tasks_once(rig) -> None:
    from command_center.orchestrator.planner import PlanLimits, Planner

    app_factory, store, worker = rig
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT monitor_record_finding(%s, %s, %s::jsonb)",
                ("worker-01:infra", "active_workers:2<4", '{"active_workers": 2}'),
            )
            # Idempotent while open: the same (source, failure) is one row.
            cur.execute(
                "SELECT monitor_record_finding(%s, %s, %s::jsonb)",
                ("worker-01:infra", "active_workers:2<4", '{"active_workers": 2}'),
            )
            cur.execute("SELECT count(*) FROM monitor_finding WHERE state = 'open'")
            assert cur.fetchone()[0] == 1
    report = Planner(app_factory).plan_once(PlanLimits(planner="planner-t"))
    from command_center.orchestrator.planner import _monitor_task_id

    monitor_id = _monitor_task_id("worker-01:infra", "active_workers:2<4")
    assert monitor_id.startswith("VOYN-MON-WORKER-01-INFRA-ACTIVE-WORKERS-2-4-")
    assert report.monitor_tasks == [(monitor_id, "active_workers:2<4")]
    task = store.get_task(monitor_id)
    assert task["status"] == "OPEN" and task["priority"] == "P1"
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT task_class FROM backlog_task WHERE task_id = %s", (task["task_id"],))
            assert cur.fetchone()[0] == "pipeline"
    # A second tick does not create a twin; clearing then re-recording re-uses the id.
    report2 = Planner(app_factory).plan_once(PlanLimits(planner="planner-t"))
    assert report2.monitor_tasks == []
    with app_factory() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT monitor_clear_finding(%s)", ("worker-01:infra",))
            assert cur.fetchone()[0] == 1
