"""backlog_triage (0008): the missing seam out of UNTRIAGED. On live PG,
under real app grants — the four decisions, the DONE-needs-evidence gate,
the not-untriaged refusal, and the audit trail."""

from __future__ import annotations


from tests.db.test_backlog_planner import _test_repo_routes, rig  # noqa: F401


def _untriaged(store, factory, task_id):
    from tests.db.test_backlog_planner import _task
    assert store.upsert_task(_task(task_id, status="UNTRIAGED"))[0]


def _triage(factory, task_id, decision, detail=None):
    with factory() as c, c.cursor() as cur:
        cur.execute("SELECT ok, reason FROM backlog_triage(%s, %s, %s)",
                    (task_id, decision, detail))
        return cur.fetchone()


def _status(factory, task_id):
    with factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        return cur.fetchone()[0]


def test_accept_opens_the_finding(rig):  # noqa: F811
    f, store, _ = rig
    _untriaged(store, f, "VOYN-W0-T1")
    ok, reason = _triage(f, "VOYN-W0-T1", "accept")
    assert ok and reason == "OPEN"
    assert _status(f, "VOYN-W0-T1") == "OPEN"


def test_refine_needs_refinement(rig):  # noqa: F811
    f, store, _ = rig
    _untriaged(store, f, "VOYN-W0-T2")
    ok, reason = _triage(f, "VOYN-W0-T2", "refine")
    assert ok and reason == "NEEDS_REFINEMENT"


def test_duplicate_is_decided_with_traceable_detail(rig):  # noqa: F811
    f, store, _ = rig
    _untriaged(store, f, "VOYN-W0-T3")
    ok, reason = _triage(f, "VOYN-W0-T3", "duplicate", "superseded by VOYN-W0-T1")
    assert ok and reason == "DECIDED"
    with f() as c, c.cursor() as cur:
        cur.execute("SELECT detail FROM backlog_event WHERE task_id=%s AND event='triage' "
                    "AND outcome='granted'", ("VOYN-W0-T3",))
        assert "superseded" in str(cur.fetchone()[0])


def test_done_requires_pr_and_sha_evidence(rig):  # noqa: F811
    f, store, _ = rig
    _untriaged(store, f, "VOYN-W0-T4")
    # bare 'done' is refused — a finding cannot claim delivery without receipts
    ok, reason = _triage(f, "VOYN-W0-T4", "done")
    assert not ok and "done_needs_evidence" in reason
    assert _status(f, "VOYN-W0-T4") == "UNTRIAGED"
    # with receipts, it closes
    with f() as c, c.cursor() as cur:
        cur.execute("SELECT backlog_record_evidence(%s,'pr',%s)", ("VOYN-W0-T4", "https://x/pull/1"))
        cur.execute("SELECT backlog_record_evidence(%s,'sha',%s)", ("VOYN-W0-T4", "a"*40))
        c.commit()
    ok, reason = _triage(f, "VOYN-W0-T4", "done")
    assert ok and reason == "DONE"


def test_triage_only_from_untriaged(rig):  # noqa: F811
    f, store, _ = rig
    from tests.db.test_backlog_planner import _task
    assert store.upsert_task(_task("VOYN-W0-T5", status="OPEN"))[0]
    ok, reason = _triage(f, "VOYN-W0-T5", "accept")
    assert not ok and "not_untriaged" in reason


def test_unknown_decision_is_refused_as_data(rig):  # noqa: F811
    f, store, _ = rig
    _untriaged(store, f, "VOYN-W0-T6")
    ok, reason = _triage(f, "VOYN-W0-T6", "nonsense")
    assert not ok and "unknown_decision" in reason
    assert _status(f, "VOYN-W0-T6") == "UNTRIAGED"


def test_concurrent_triage_of_one_finding_applies_once(rig):  # noqa: F811
    """The only serializer of two simultaneous triages of one finding is the
    row lock (backlog_triage has no optimistic revision gate, unlike
    transition/dispatch). Removing FOR UPDATE lets both read UNTRIAGED and
    both apply — this pins the lock: exactly one 'granted', the loser sees
    not_untriaged."""
    import threading

    f, store, _ = rig
    _untriaged(store, f, "VOYN-W0-TC")
    results = []
    barrier = threading.Barrier(2)

    def worker(decision):
        with f() as c, c.cursor() as cur:
            barrier.wait()
            cur.execute("SELECT ok, reason FROM backlog_triage(%s, %s, %s)",
                        ("VOYN-W0-TC", decision, None))
            results.append(cur.fetchone())

    ts = [threading.Thread(target=worker, args=(d,)) for d in ("accept", "duplicate")]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    granted = [r for r in results if r[0]]
    refused = [r for r in results if not r[0]]
    assert len(granted) == 1, results
    assert len(refused) == 1 and "not_untriaged" in refused[0][1], results


def test_migration_0008_is_reversible_without_residue(pg_connection_factory):
    """Live up->down->up: down drops the function, up recreates it with no
    'already exists' residue (CREATE FUNCTION, not CREATE OR REPLACE — a
    no-op down would break the second up, which nothing else catches)."""
    from command_center.db import migrations

    with pg_connection_factory() as conn:
        migrations.upgrade(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_proc WHERE proname='backlog_triage'")
            assert cur.fetchone()[0] == 1
        migrations.downgrade(conn, target=7)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_proc WHERE proname='backlog_triage'")
            assert cur.fetchone()[0] == 0
        migrations.upgrade(conn)  # must not raise 'already exists'
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_proc WHERE proname='backlog_triage'")
            assert cur.fetchone()[0] == 1
