"""Real PostgreSQL regression coverage for atomic coordinator queue writes."""
from concurrent.futures import ThreadPoolExecutor

from command_center.db import migrations, roles


def _prepare(conn):
    roles.apply_bootstrap(conn)
    migrations.upgrade(conn)


def _enqueue(conn, snapshot="snapshot", fail=False):
    return conn.execute(
        "SELECT * FROM coordinator_enqueue('test', %s, 'blocker', %s, "
        "'execution', jsonb_build_object('fail', %s))",
        (snapshot, "coord:test:" + snapshot, fail),
    ).fetchone()


def test_enqueue_failure_retries_same_snapshot_with_cooldown_and_cap(admin_conn):
    _prepare(admin_conn)
    admin_conn.execute("""
        CREATE FUNCTION coordination_test_fail() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.payload->>'fail' = 'true' THEN
                RAISE EXCEPTION 'simulated queue failure';
            END IF;
            RETURN NEW;
        END $$;
        CREATE TRIGGER coordination_test_fail BEFORE INSERT ON work_item
        FOR EACH ROW EXECUTE FUNCTION coordination_test_fail();
    """)
    assert _enqueue(admin_conn, fail=True)[1].startswith("enqueue_failed:")
    assert _enqueue(admin_conn) == (None, "cas_begin_refused")  # cooldown
    admin_conn.execute("UPDATE coordinator_state SET cooldown_until = now() - interval '1 second'")
    item, error = _enqueue(admin_conn)
    assert item and error is None
    assert admin_conn.execute("SELECT count(*) FROM work_item").fetchone()[0] == 1
    assert not admin_conn.execute(
        "SELECT coordinator_cas_clear_inflight('test', 'coord:test:snapshot')"
    ).fetchone()[0]  # never reset an actual queued/running item

    # A distinct snapshot has its own bounded pre-queue retry budget.
    admin_conn.execute("DELETE FROM coordinator_state")
    for _ in range(3):
        assert _enqueue(admin_conn, "bad", True)[1].startswith("enqueue_failed:")
        admin_conn.execute("UPDATE coordinator_state SET cooldown_until = now() - interval '1 second'")
    assert _enqueue(admin_conn, "bad") == (None, "cas_begin_refused")
    assert _enqueue(admin_conn, "changed")[0]


def test_transaction_rollback_never_leaves_claim_without_queue(admin_conn):
    _prepare(admin_conn)
    try:
        with admin_conn.transaction():
            assert _enqueue(admin_conn)[0]
            raise RuntimeError("simulate crash before commit")
    except RuntimeError:
        pass
    assert admin_conn.execute("SELECT count(*) FROM coordinator_state").fetchone()[0] == 0
    assert admin_conn.execute("SELECT count(*) FROM work_item").fetchone()[0] == 0
    assert _enqueue(admin_conn)[0]


def test_concurrent_ticks_commit_only_one_action(admin_conn, psycopg, test_dsn):
    _prepare(admin_conn)

    def competing_tick():
        with psycopg.connect(test_dsn, autocommit=True) as other:
            return _enqueue(other)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with admin_conn.transaction():
            item, error = _enqueue(admin_conn)
            assert item and error is None
            future = executor.submit(competing_tick)
            # The second tick can only see the committed claim+queue pair.
        assert future.result(timeout=10) == (None, "cas_begin_refused")
    assert admin_conn.execute("SELECT count(*) FROM work_item").fetchone()[0] == 1


def test_missing_item_recovery_is_bounded_and_reuses_key(admin_conn):
    _prepare(admin_conn)
    assert admin_conn.execute(
        "SELECT coordinator_cas_begin('test','snapshot','blocker','coord:test:snapshot')"
    ).fetchone()[0]
    assert admin_conn.execute(
        "SELECT coordinator_cas_clear_inflight('test','coord:test:snapshot')"
    ).fetchone()[0]
    admin_conn.execute("UPDATE coordinator_state SET cooldown_until = now() - interval '1 second'")
    assert _enqueue(admin_conn)[0]


def test_app_role_can_enqueue_and_worker_can_parse_payload(admin_conn, connect_as):
    from contextlib import contextmanager

    from command_center.orchestrator.coordination import (
        CoordinationLimits,
        coordinate_once,
    )
    from command_center.worker.payloads import AgentRunRequest, parse_agent_run

    _prepare(admin_conn)
    roles.apply_table_grants(admin_conn)

    @contextmanager
    def factory():
        with connect_as(roles.APP_ROLE) as conn:
            yield conn

    report = coordinate_once(
        factory, CoordinationLimits(repository_path="/srv/test-repo"),
        environ={"AICC_COORDINATOR_V2": "1"},
        load_candidates=lambda: [{"task_id": "VOYN-A", "wave": "1", "title": "a"}],
    )
    assert report.enqueued
    payload, task_id, attempts = admin_conn.execute(
        "SELECT payload, task_id, max_attempts FROM work_item WHERE work_item_id = %s",
        (report.enqueued,),
    ).fetchone()
    assert isinstance(parse_agent_run(payload), AgentRunRequest)
    assert payload["cascade"][0]["capability"] == "model_only"
    assert task_id == "VOYN-AICC-COORD-V2" and attempts == 1
    again = coordinate_once(
        factory, CoordinationLimits(repository_path="/srv/test-repo"),
        environ={"AICC_COORDINATOR_V2": "1"},
        load_candidates=lambda: [{"task_id": "VOYN-A", "wave": "1", "title": "a"}],
    )
    assert again.skipped == "inflight_pending"


def test_planner_disabled_and_stopped_skip_advisory_read(admin_conn, connect_as, monkeypatch):
    import json
    from contextlib import contextmanager

    from command_center.db.backlog_parser import ParsedTask
    from command_center.db.backlog_store import BacklogStore
    from command_center.orchestrator import planner

    roles.apply_bootstrap(admin_conn)
    with connect_as(roles.MIGRATOR_ROLE) as migrator:
        migrations.upgrade(migrator)
        roles.apply_table_grants(migrator)

    @contextmanager
    def factory():
        with connect_as(roles.APP_ROLE) as conn:
            yield conn

    store = BacklogStore(factory)
    for task_id in ("VOYN-A", "VOYN-B"):
        assert store.upsert_task(ParsedTask(
            task_id=task_id, wave="0", priority="P0", status="OPEN", kind="task",
            title=task_id, body="test", repo="test-repo", line_no=1,
        ))[0]
    monkeypatch.setenv("AICC_PLANNER_REPO_ROUTES", json.dumps({"test-repo": ["AICC", "/srv/test-repo"]}))
    reads = []
    monkeypatch.setattr(planner, "load_advisory_order", lambda _factory: reads.append(True) or ["VOYN-B", "VOYN-A"])
    monkeypatch.delenv("AICC_COORDINATOR_V2", raising=False)
    monkeypatch.delenv("AICC_COORDINATOR_V2_STOP", raising=False)
    # A zero dispatch budget still runs the actual candidate-ordering path.
    planner.plan_once(factory, planner.PlanLimits(max_dispatches_per_tick=0))
    assert reads == []
    monkeypatch.setenv("AICC_COORDINATOR_V2", "1")
    monkeypatch.setenv("AICC_COORDINATOR_V2_STOP", "1")
    report = planner.plan_once(factory, planner.PlanLimits(max_dispatches_per_tick=1))
    assert reads == []
    assert [task for task, _ in report.dispatched] == ["VOYN-A"]
