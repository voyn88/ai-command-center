"""backlog_mark_duplicate (0018): the missing seam out of OPEN, for a task
found to duplicate another that is already live (VOYN-W0-AICC-BGE-M3-DEDUP-SCAN).

backlog_triage's 'duplicate' decision only fires from UNTRIAGED, and its
canonical reference is free text in an optional audit detail. This is the
OPEN -> DECIDED counterpart, with a required, FK-checked canonical task."""

from __future__ import annotations


from tests.db.test_backlog_planner import _task, rig  # noqa: F401


def _open(store, task_id, **overrides):
    assert store.upsert_task(_task(task_id, **overrides))[0]


def _mark_duplicate(factory, task_id, canonical_task_id, detail=None):
    with factory() as c, c.cursor() as cur:
        cur.execute("SELECT ok, reason FROM backlog_mark_duplicate(%s, %s, %s)",
                    (task_id, canonical_task_id, detail))
        return cur.fetchone()


def _status(factory, task_id):
    with factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        return cur.fetchone()[0]


def test_duplicate_of_open_task_is_decided_with_traceable_canonical(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-T1")
    _open(store, "VOYN-W0-T2")
    ok, reason = _mark_duplicate(f, "VOYN-W0-T2", "VOYN-W0-T1", "same finding, different words")
    assert ok and reason == "DECIDED"
    assert _status(f, "VOYN-W0-T2") == "DECIDED"
    with f() as c, c.cursor() as cur:
        cur.execute("SELECT canonical_task_id, detail FROM backlog_duplicate WHERE task_id=%s",
                    ("VOYN-W0-T2",))
        canonical, detail = cur.fetchone()
        assert canonical == "VOYN-W0-T1"
        assert "same finding" in detail
        cur.execute("SELECT detail FROM backlog_event WHERE task_id=%s AND event='mark_duplicate' "
                    "AND outcome='granted'", ("VOYN-W0-T2",))
        assert "VOYN-W0-T1" in str(cur.fetchone()[0])


def test_canonical_is_required(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-T3")
    ok, reason = _mark_duplicate(f, "VOYN-W0-T3", None)
    assert not ok and reason == "canonical_required"
    assert _status(f, "VOYN-W0-T3") == "OPEN"


def test_self_duplicate_is_refused(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-T4")
    ok, reason = _mark_duplicate(f, "VOYN-W0-T4", "VOYN-W0-T4")
    assert not ok and reason == "self_duplicate"
    assert _status(f, "VOYN-W0-T4") == "OPEN"


def test_unknown_canonical_is_refused(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-T5")
    ok, reason = _mark_duplicate(f, "VOYN-W0-T5", "VOYN-W0-DOES-NOT-EXIST")
    assert not ok and "unknown_canonical" in reason
    assert _status(f, "VOYN-W0-T5") == "OPEN"


def test_mark_duplicate_only_from_open(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-T6", status="UNTRIAGED")
    _open(store, "VOYN-W0-T7")
    ok, reason = _mark_duplicate(f, "VOYN-W0-T6", "VOYN-W0-T7")
    assert not ok and "not_open" in reason
    assert _status(f, "VOYN-W0-T6") == "UNTRIAGED"


def test_already_decided_task_cannot_be_marked_duplicate_twice(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-T8")
    _open(store, "VOYN-W0-T9")
    _open(store, "VOYN-W0-T10")
    ok, _ = _mark_duplicate(f, "VOYN-W0-T8", "VOYN-W0-T9")
    assert ok
    ok, reason = _mark_duplicate(f, "VOYN-W0-T8", "VOYN-W0-T10")
    assert not ok and "not_open" in reason


def test_unknown_task_is_refused(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-T11")
    ok, reason = _mark_duplicate(f, "VOYN-W0-DOES-NOT-EXIST", "VOYN-W0-T11")
    assert not ok and reason == "unknown_task"


def test_migration_0018_is_reversible_without_residue(pg_connection_factory):
    """Live up->down->up: down drops the function and table, up recreates them
    with no 'already exists' residue."""
    from command_center.db import migrations

    with pg_connection_factory() as conn:
        migrations.upgrade(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_proc WHERE proname='backlog_mark_duplicate'")
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT count(*) FROM pg_class WHERE relname='backlog_duplicate'")
            assert cur.fetchone()[0] == 1
        migrations.downgrade(conn, target=17)
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_proc WHERE proname='backlog_mark_duplicate'")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM pg_class WHERE relname='backlog_duplicate'")
            assert cur.fetchone()[0] == 0
        migrations.upgrade(conn)  # must not raise 'already exists'
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_proc WHERE proname='backlog_mark_duplicate'")
            assert cur.fetchone()[0] == 1
