"""backlog_mark_duplicate (0018): the missing OPEN -> DECIDED seam for a
duplicate found by the bge-m3 dedup scan (VOYN-W0-AICC-BGE-M3-DEDUP-SCAN),
after triage. On live PG, under real app grants.

Also pins the fix for PR #785's rejection: existence of p_task_id must be
confirmed BEFORE anything is audited, because backlog_event.task_id is
FK-constrained against backlog_task. The rejected PR's canonical_required
and self_duplicate checks audited p_task_id before confirming the task
existed, so an unknown task_id crashed with a foreign-key violation instead
of returning a verdict — exactly what
test_unknown_task_with_self_duplicate_does_not_crash and
test_unknown_task_with_missing_canonical_does_not_crash exercise here.
"""

from __future__ import annotations

from tests.db.test_backlog_planner import _test_repo_routes, rig  # noqa: F401


def _open(store, task_id, **overrides):
    from tests.db.test_backlog_planner import _task
    assert store.upsert_task(_task(task_id, status="OPEN", **overrides))[0]


def _mark_duplicate(factory, task_id, canonical_task_id, detail=None):
    with factory() as c, c.cursor() as cur:
        cur.execute("SELECT ok, reason FROM backlog_mark_duplicate(%s, %s, %s)",
                    (task_id, canonical_task_id, detail))
        return cur.fetchone()


def _status(factory, task_id):
    with factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        return cur.fetchone()[0]


def _duplicate_of(factory, task_id):
    with factory() as c, c.cursor() as cur:
        cur.execute("SELECT duplicate_of FROM backlog_task WHERE task_id=%s", (task_id,))
        return cur.fetchone()[0]


def test_open_becomes_decided_with_canonical_recorded(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-D1")
    _open(store, "VOYN-W0-D2")
    ok, reason = _mark_duplicate(f, "VOYN-W0-D2", "VOYN-W0-D1", "same finding, filed twice")
    assert ok and reason == "DECIDED"
    assert _status(f, "VOYN-W0-D2") == "DECIDED"
    assert _duplicate_of(f, "VOYN-W0-D2") == "VOYN-W0-D1"
    with f() as c, c.cursor() as cur:
        cur.execute("SELECT detail FROM backlog_event WHERE task_id=%s AND event='mark_duplicate' "
                    "AND outcome='granted'", ("VOYN-W0-D2",))
        detail = cur.fetchone()[0]
        assert detail["canonical"] == "VOYN-W0-D1"
        assert "same finding" in detail["detail"]


def test_canonical_is_required(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-D3")
    ok, reason = _mark_duplicate(f, "VOYN-W0-D3", None)
    assert not ok and reason == "canonical_required"
    assert _status(f, "VOYN-W0-D3") == "OPEN"


def test_self_duplicate_is_refused(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-D4")
    ok, reason = _mark_duplicate(f, "VOYN-W0-D4", "VOYN-W0-D4")
    assert not ok and reason == "self_duplicate"
    assert _status(f, "VOYN-W0-D4") == "OPEN"


def test_unknown_canonical_is_refused(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-D5")
    ok, reason = _mark_duplicate(f, "VOYN-W0-D5", "VOYN-W0-NO-SUCH-TASK")
    assert not ok and reason == "unknown_canonical"
    assert _status(f, "VOYN-W0-D5") == "OPEN"


def test_unknown_task_is_refused_with_a_valid_canonical(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-D6")
    ok, reason = _mark_duplicate(f, "VOYN-W0-NO-SUCH-TASK", "VOYN-W0-D6")
    assert not ok and reason == "unknown_task"


def test_unknown_task_with_self_duplicate_does_not_crash(rig):  # noqa: F811
    """PR #785's rejection: self_duplicate audited p_task_id before existence
    was confirmed, so this call raised a foreign-key violation instead of
    returning a verdict. Pins that existence is checked first."""
    f, store, _ = rig
    ok, reason = _mark_duplicate(f, "VOYN-W0-NO-SUCH-TASK", "VOYN-W0-NO-SUCH-TASK")
    assert not ok and reason == "unknown_task"


def test_unknown_task_with_missing_canonical_does_not_crash(rig):  # noqa: F811
    """PR #785's rejection: canonical_required audited p_task_id before
    existence was confirmed, so this call raised a foreign-key violation
    instead of returning a verdict. Pins that existence is checked first."""
    f, store, _ = rig
    ok, reason = _mark_duplicate(f, "VOYN-W0-NO-SUCH-TASK", None)
    assert not ok and reason == "unknown_task"


def test_mark_duplicate_only_from_open(rig):  # noqa: F811
    f, store, _ = rig
    from tests.db.test_backlog_planner import _task
    assert store.upsert_task(_task("VOYN-W0-D7", status="UNTRIAGED"))[0]
    assert store.upsert_task(_task("VOYN-W0-D8", status="OPEN"))[0]
    ok, reason = _mark_duplicate(f, "VOYN-W0-D7", "VOYN-W0-D8")
    assert not ok and "not_open" in reason
    assert _status(f, "VOYN-W0-D7") == "UNTRIAGED"


def test_mark_duplicate_is_not_reapplied(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-D9")
    _open(store, "VOYN-W0-D10")
    _open(store, "VOYN-W0-D11")
    ok, reason = _mark_duplicate(f, "VOYN-W0-D9", "VOYN-W0-D10")
    assert ok and reason == "DECIDED"
    ok, reason = _mark_duplicate(f, "VOYN-W0-D9", "VOYN-W0-D11")
    assert not ok and "not_open" in reason
    assert _duplicate_of(f, "VOYN-W0-D9") == "VOYN-W0-D10"


def test_migration_0018_is_reversible_without_residue(pg_connection_factory):
    """Live up->down->up: down drops the function and column, up recreates
    them with no 'already exists' residue (CREATE FUNCTION, not CREATE OR
    REPLACE — a no-op down would break the second up, which nothing else
    catches)."""
    from command_center.db import migrations

    with pg_connection_factory() as conn:
        migrations.upgrade(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_proc WHERE proname='backlog_mark_duplicate'")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name='backlog_task' AND column_name='duplicate_of'")
            assert cur.fetchone()[0] == 1
        migrations.downgrade(conn, target=17)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_proc WHERE proname='backlog_mark_duplicate'")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM information_schema.columns "
                        "WHERE table_name='backlog_task' AND column_name='duplicate_of'")
            assert cur.fetchone()[0] == 0
        migrations.upgrade(conn)  # must not raise 'already exists'
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_proc WHERE proname='backlog_mark_duplicate'")
            assert cur.fetchone()[0] == 1
