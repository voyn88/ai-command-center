"""backlog_mark_duplicate (0025): the legal exit for an OPEN duplicate.

The dedup scan (`bge-m3` embeddings over the live tasks, pairwise cosine,
threshold 0.75) confirmed real duplicates among tasks that were already OPEN,
and the store had no move for them: `backlog_triage` (0008) decides
'duplicate' only from UNTRIAGED, and the linear machine has no OPEN -> DECIDED
edge. This module exercises the seam that closes that gap on live PostgreSQL,
under the real `aicc_app` grants — the decision itself, the recorded
canonical, every refusal, and the pool actually shrinking afterwards.

Two of the tests here deserve naming, because they are the ones the first
cut of this function would have failed while every other test passed:

* `test_no_argument_combination_raises_instead_of_refusing` — the whole
  argument cross-product, asserting a verdict rather than an exception.
  `backlog_event.task_id` is a foreign key to `backlog_task`, so any refusal
  that audits with an unverified `p_task_id` raises a foreign-key violation
  instead of returning `ok=false`; the first cut checked the canonical
  argument's shape before confirming the subject existed, and
  `backlog_mark_duplicate(MISSING, MISSING)` therefore crashed
  (adversarial review of 69267384). Per-branch tests that each happened to use
  a task created beforehand could not see it — the branch was reachable, the
  test inputs were not. So the combination is enumerated by machine.
* `test_a_refusal_for_a_missing_task_audits_with_a_null_task_id` — the
  positive statement of the same rule, so the NULL is understood as the
  deliberate thing it is rather than tidied away by someone making the audit
  "more informative".
"""

from __future__ import annotations

import pytest

from tests.db.test_backlog_planner import _test_repo_routes, rig  # noqa: F401

# `rig` provisions cluster-level roles (aicc_migrator, aicc_worker, ...) that
# every xdist worker's database shares; running these tests under xdist
# parallelism races that provisioning against every other module that also
# pulls in `rig`. See the same note in `test_backlog_triage.py`.
pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

#: A task id that is never created. Shaped like a real one so the refusal is
#: about the row being absent and not about the id being malformed.
MISSING = "VOYN-W0-DUP-NOPE"


def _open(store, task_id, **overrides):
    from tests.db.test_backlog_planner import _task

    assert store.upsert_task(_task(task_id, **overrides))[0]
    return task_id


def _mark(factory, task_id, canonical, detail=None):
    with factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT ok, reason, revision FROM backlog_mark_duplicate(%s, %s, %s)",
            (task_id, canonical, detail),
        )
        return cur.fetchone()


def _status(factory, task_id):
    with factory() as c, c.cursor() as cur:
        cur.execute("SELECT status FROM backlog_task WHERE task_id=%s", (task_id,))
        row = cur.fetchone()
        return row[0] if row else None


def _events(factory, task_id):
    with factory() as c, c.cursor() as cur:
        cur.execute(
            "SELECT outcome, reason, detail FROM backlog_event "
            "WHERE event='mark_duplicate' AND task_id IS NOT DISTINCT FROM %s "
            "ORDER BY event_id",
            (task_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def test_an_open_duplicate_reaches_decided(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-A")
    _open(store, "VOYN-W0-DUP-CANON")
    ok, reason, revision = _mark(
        f, "VOYN-W0-DUP-A", "VOYN-W0-DUP-CANON", "cosine 0.81 (bge-m3)"
    )
    assert ok and reason == "DECIDED"
    assert _status(f, "VOYN-W0-DUP-A") == "DECIDED"
    # The canonical is untouched: it is the task that carries the work now.
    assert _status(f, "VOYN-W0-DUP-CANON") == "OPEN"
    # The optimistic lock advances like every other accepted mutation, and the
    # verdict reports the value a caller would have to write against next.
    with f() as c, c.cursor() as cur:
        cur.execute(
            "SELECT revision FROM backlog_task WHERE task_id=%s", ("VOYN-W0-DUP-A",)
        )
        assert cur.fetchone()[0] == revision


def test_the_canonical_is_recorded_as_a_row_not_a_sentence(rig):  # noqa: F811
    """A queryable, foreign-keyed pair — the point of the table.

    Recording "superseded by X" only in the audit detail would make the claim
    unqueryable and let X be a task that never existed.
    """
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-B")
    _open(store, "VOYN-W0-DUP-CANON")
    assert _mark(f, "VOYN-W0-DUP-B", "VOYN-W0-DUP-CANON", "same 16-vs-23 drift")[0]
    with f() as c, c.cursor() as cur:
        cur.execute(
            "SELECT canonical_task_id, detail FROM backlog_duplicate WHERE task_id=%s",
            ("VOYN-W0-DUP-B",),
        )
        assert cur.fetchone() == ("VOYN-W0-DUP-CANON", "same 16-vs-23 drift")


def test_the_decision_is_audited_with_both_endpoints(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-C")
    _open(store, "VOYN-W0-DUP-CANON")
    assert _mark(f, "VOYN-W0-DUP-C", "VOYN-W0-DUP-CANON", "scan pair 7")[0]
    (outcome, reason, detail), = _events(f, "VOYN-W0-DUP-C")
    assert outcome == "granted"
    assert reason == "VOYN-W0-DUP-CANON"
    assert detail["canonical"] == "VOYN-W0-DUP-CANON"
    assert detail["from"] == "OPEN" and detail["to"] == "DECIDED"
    assert detail["detail"] == "scan pair 7"


def test_the_dependents_of_a_retired_duplicate_are_named_in_the_audit(rig):  # noqa: F811
    """The known, pre-existing cost of retiring a task, made answerable.

    `backlog_eligible` requires every dependency to be DONE and DECIDED is not
    DONE, so a task depending on a retired duplicate stops being dispatchable.
    That is true of `backlog_triage(..., 'duplicate')` since 0008 and is not
    introduced here; re-pointing the edge at the canonical is a cycle-checked
    act that belongs with `backlog_add_dependency`. What this pins is that the
    consequence is recorded rather than silent — otherwise "why did this task
    go quiet?" is only answerable by reading the dependency graph.
    """
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-DEP")
    _open(store, "VOYN-W0-DUP-CANON")
    _open(store, "VOYN-W0-DUP-NEEDER")
    with f() as c, c.cursor() as cur:
        cur.execute(
            "SELECT ok FROM backlog_add_dependency(%s, %s)",
            ("VOYN-W0-DUP-NEEDER", "VOYN-W0-DUP-DEP"),
        )
        assert cur.fetchone()[0]

    assert _mark(f, "VOYN-W0-DUP-DEP", "VOYN-W0-DUP-CANON")[0]
    (_, _, detail), = _events(f, "VOYN-W0-DUP-DEP")
    assert detail["dependents"] == ["VOYN-W0-DUP-NEEDER"]
    assert detail["canonical_status"] == "OPEN"

    # And the consequence itself, stated rather than implied.
    with f() as c, c.cursor() as cur:
        cur.execute("SELECT task_id FROM backlog_eligible")
        assert "VOYN-W0-DUP-NEEDER" not in {row[0] for row in cur.fetchall()}


def test_a_canonical_that_is_already_done_is_allowed(rig):  # noqa: F811
    """"The work is already delivered over there" is the commonest real case.

    The canonical's status is recorded rather than constrained, because a
    canonical that was DONE and one that was OPEN make the same row here and
    are different decisions.
    """
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-J")
    _open(store, "VOYN-W0-DUP-CANONDONE", status="DONE")
    assert _mark(f, "VOYN-W0-DUP-J", "VOYN-W0-DUP-CANONDONE")[0]
    (_, _, detail), = _events(f, "VOYN-W0-DUP-J")
    assert detail["canonical_status"] == "DONE"


def test_a_retired_duplicate_leaves_the_dispatch_pool(rig):  # noqa: F811
    """The reason the seam exists: an OPEN duplicate is dispatchable work.

    `backlog_eligible` selects `status = 'OPEN'`, so until this function
    existed a confirmed duplicate kept queueing a run to re-deliver work
    already delivered.
    """
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-D2")
    _open(store, "VOYN-W0-DUP-CANON")

    def eligible():
        with f() as c, c.cursor() as cur:
            cur.execute("SELECT task_id FROM backlog_eligible")
            return {row[0] for row in cur.fetchall()}

    assert {"VOYN-W0-DUP-D2", "VOYN-W0-DUP-CANON"} <= eligible()
    assert _mark(f, "VOYN-W0-DUP-D2", "VOYN-W0-DUP-CANON")[0]
    remaining = eligible()
    assert "VOYN-W0-DUP-D2" not in remaining
    assert "VOYN-W0-DUP-CANON" in remaining


# ---------------------------------------------------------------------------
# The refusals, each as data
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("canonical", [None, "", "   "], ids=["null", "empty", "blank"])
def test_canonical_is_required(rig, canonical):  # noqa: F811
    """DECIDED without naming the survivor is a quieter way of deleting a finding."""
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-E")
    ok, reason, _ = _mark(f, "VOYN-W0-DUP-E", canonical)
    assert not ok and reason == "canonical_required"
    assert _status(f, "VOYN-W0-DUP-E") == "OPEN"


def test_self_duplicate_is_refused(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-F")
    ok, reason, _ = _mark(f, "VOYN-W0-DUP-F", "VOYN-W0-DUP-F")
    assert not ok and reason == "self_duplicate"
    assert _status(f, "VOYN-W0-DUP-F") == "OPEN"


def test_an_unknown_canonical_is_refused(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-G")
    ok, reason, _ = _mark(f, "VOYN-W0-DUP-G", MISSING)
    assert not ok and reason == "unknown_canonical"
    assert _status(f, "VOYN-W0-DUP-G") == "OPEN"


def test_an_unknown_task_is_refused(rig):  # noqa: F811
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-CANON")
    ok, reason, _ = _mark(f, MISSING, "VOYN-W0-DUP-CANON")
    assert not ok and reason == "unknown_task"


@pytest.mark.parametrize(
    "status",
    ["UNTRIAGED", "IN_PROGRESS", "READY_TO_REVIEW", "DONE", "DECIDED", "DEFER_TO_USER"],
)
def test_only_from_open(rig, status):  # noqa: F811
    """One state in. IN_PROGRESS holds a lease, READY_TO_REVIEW has a PR in
    flight, and UNTRIAGED is `backlog_triage`'s decision, which already exists.
    """
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-H", status=status)
    _open(store, "VOYN-W0-DUP-CANON")
    ok, reason, _ = _mark(f, "VOYN-W0-DUP-H", "VOYN-W0-DUP-CANON")
    assert not ok and reason == f"not_open: {status}"
    assert _status(f, "VOYN-W0-DUP-H") == status


def test_a_gate_is_refused(rig):  # noqa: F811
    """A gate closes through its own acceptance act — the rule
    `backlog_transition` enforces, which retiring it as a duplicate would
    route around."""
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-G1", kind="gate")
    _open(store, "VOYN-W0-DUP-CANON")
    ok, reason, _ = _mark(f, "VOYN-W0-DUP-G1", "VOYN-W0-DUP-CANON")
    assert not ok and reason == "gate_is_control_record"
    assert _status(f, "VOYN-W0-DUP-G1") == "OPEN"


def test_a_canonical_that_is_itself_a_duplicate_is_refused(rig):  # noqa: F811
    """One hop, never a chain, and the refusal names the real head.

    Otherwise the pointer leads to a DECIDED row rather than to the work.
    """
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-HEAD")
    _open(store, "VOYN-W0-DUP-MID")
    _open(store, "VOYN-W0-DUP-TAIL")
    assert _mark(f, "VOYN-W0-DUP-MID", "VOYN-W0-DUP-HEAD")[0]
    ok, reason, _ = _mark(f, "VOYN-W0-DUP-TAIL", "VOYN-W0-DUP-MID")
    assert not ok
    assert reason == "canonical_is_a_duplicate: VOYN-W0-DUP-HEAD"
    assert _status(f, "VOYN-W0-DUP-TAIL") == "OPEN"


def test_every_refusal_is_recorded_as_an_event(rig):  # noqa: F811
    """Refusals are data. A rejected decision that left no trace would make
    "nothing tried to retire this task" and "something tried and was refused"
    the same observation."""
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-I")
    assert _mark(f, "VOYN-W0-DUP-I", None)[0] is False
    assert _mark(f, "VOYN-W0-DUP-I", "VOYN-W0-DUP-I")[0] is False
    assert _mark(f, "VOYN-W0-DUP-I", MISSING)[0] is False
    assert [
        (outcome, reason) for outcome, reason, _ in _events(f, "VOYN-W0-DUP-I")
    ] == [
        ("rejected", "canonical_required"),
        ("rejected", "self_duplicate"),
        ("rejected", "unknown_canonical"),
    ]


def test_a_task_the_importer_reopens_can_be_retired_again(rig):  # noqa: F811
    """Why the insert is `ON CONFLICT DO UPDATE` rather than a plain INSERT.

    `backlog_upsert_task` sets `status` unconditionally on an existing row, so
    a reconciled Markdown file can legitimately bring a retired task back to
    OPEN. A plain INSERT would then raise a primary-key violation on the second
    decision — an exception, in a function whose whole contract is that
    refusals are data.
    """
    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-K")
    _open(store, "VOYN-W0-DUP-CANON")
    _open(store, "VOYN-W0-DUP-CANON2")
    assert _mark(f, "VOYN-W0-DUP-K", "VOYN-W0-DUP-CANON", "first call")[0]

    _open(store, "VOYN-W0-DUP-K")  # the importer reconciles it back to OPEN
    assert _status(f, "VOYN-W0-DUP-K") == "OPEN"

    assert _mark(f, "VOYN-W0-DUP-K", "VOYN-W0-DUP-CANON2", "second call")[0]
    with f() as c, c.cursor() as cur:
        cur.execute(
            "SELECT canonical_task_id, detail FROM backlog_duplicate WHERE task_id=%s",
            ("VOYN-W0-DUP-K",),
        )
        # The table keeps the current decision...
        assert cur.fetchall() == [("VOYN-W0-DUP-CANON2", "second call")]
    # ...and the audit keeps both.
    assert [reason for outcome, reason, _ in _events(f, "VOYN-W0-DUP-K")
            if outcome == "granted"] == ["VOYN-W0-DUP-CANON", "VOYN-W0-DUP-CANON2"]


# ---------------------------------------------------------------------------
# The validation-order rule, stated twice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("subject_exists", [True, False], ids=["known", "missing"])
@pytest.mark.parametrize(
    "canonical_kind", ["null", "blank", "self", "missing", "known"]
)
def test_no_argument_combination_raises_instead_of_refusing(
    rig, subject_exists, canonical_kind  # noqa: F811
):
    """Every (subject, canonical) combination returns a verdict, never raises.

    This is the machine version of the rule the module docstring explains:
    `backlog_event.task_id` is a foreign key, so a refusal that audits with an
    unverified task id raises `ForeignKeyViolation` and destroys both the
    verdict and the audit row it was supposed to write. Enumerating the
    cross-product is what makes the rule hold for branches added later —
    hand-picked inputs per branch is exactly the shape that missed it once.
    """
    f, store, _ = rig
    subject = _open(store, "VOYN-W0-DUP-X") if subject_exists else MISSING
    _open(store, "VOYN-W0-DUP-CANON")
    canonical = {
        "null": None,
        "blank": "   ",
        "self": subject,
        "missing": "VOYN-W0-DUP-ALSO-NOPE",
        "known": "VOYN-W0-DUP-CANON",
    }[canonical_kind]

    ok, reason, _ = _mark(f, subject, canonical)

    assert isinstance(ok, bool)
    if not subject_exists:
        # Absence is decided before anything else is inspected, so the reason
        # is the same whatever the canonical argument is.
        assert (ok, reason) == (False, "unknown_task")
    elif canonical_kind == "known":
        assert (ok, reason) == (True, "DECIDED")
    else:
        assert ok is False and reason != "unknown_task"

    # The connection is still usable: an exception here would have aborted the
    # caller's transaction, which is the failure this test exists to catch.
    with f() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM backlog_event WHERE event='mark_duplicate'")
        assert cur.fetchone()[0] >= 1


def test_a_refusal_for_a_missing_task_audits_with_a_null_task_id(rig):  # noqa: F811
    """The NULL is deliberate, not an omission.

    `backlog_event.task_id` references `backlog_task`, so the one refusal that
    cannot name its subject carries the requested ids in the detail instead —
    the same shape `backlog_transition` uses. Making this audit "more
    informative" by writing `p_task_id` is precisely the regression.
    """
    f, _, _ = rig
    assert _mark(f, MISSING, MISSING)[:2] == (False, "unknown_task")
    (outcome, reason, detail), = _events(f, None)
    assert (outcome, reason) == ("rejected", "unknown_task")
    assert detail["requested_task_id"] == MISSING
    assert detail["requested_canonical"] == MISSING


# ---------------------------------------------------------------------------
# Concurrency, grants, reversibility
# ---------------------------------------------------------------------------


def test_two_simultaneous_marks_of_one_task_apply_once(rig):  # noqa: F811
    """The row lock is the only serializer (there is no revision argument, as
    in `backlog_triage`). Without it both callers read OPEN and both apply,
    and the task ends up pointing at whichever canonical wrote last."""
    import threading

    f, store, _ = rig
    _open(store, "VOYN-W0-DUP-TC")
    _open(store, "VOYN-W0-DUP-CANON")
    _open(store, "VOYN-W0-DUP-CANON2")
    results = []
    barrier = threading.Barrier(2)

    def worker(canonical):
        with f() as c, c.cursor() as cur:
            barrier.wait()
            cur.execute(
                "SELECT ok, reason FROM backlog_mark_duplicate(%s, %s, %s)",
                ("VOYN-W0-DUP-TC", canonical, None),
            )
            results.append(cur.fetchone())

    threads = [
        threading.Thread(target=worker, args=(c,))
        for c in ("VOYN-W0-DUP-CANON", "VOYN-W0-DUP-CANON2")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    granted = [r for r in results if r[0]]
    refused = [r for r in results if not r[0]]
    assert len(granted) == 1, results
    assert len(refused) == 1 and refused[0][1] == "not_open: DECIDED", results
    with f() as c, c.cursor() as cur:
        cur.execute("SELECT count(*) FROM backlog_duplicate WHERE task_id=%s",
                    ("VOYN-W0-DUP-TC",))
        assert cur.fetchone()[0] == 1


def test_the_worker_role_cannot_retire_a_task_as_a_duplicate(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """An execution host must not be able to close the task it was given.

    Declared to `aicc_app` only (`roles._APP_BACKLOG_FUNCTIONS`), and the
    migration itself revokes PUBLIC's default EXECUTE — so the denial holds on
    a migrated database even before `apply_table_grants` runs.
    """
    from command_center.db import roles
    from tests.db.test_grant_compliance import _as_role, _migrate_only

    _migrate_only(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, roles.WORKER_ROLE, role_passwords), autocommit=True
    ) as conn, conn.cursor() as cur:
        with pytest.raises(Exception, match="permission denied"):
            # A non-existent task id is fine: the denial must happen before
            # any data check.
            cur.execute(
                "SELECT public.backlog_mark_duplicate(%s, %s)", (MISSING, MISSING)
            )


def test_migration_0025_is_reversible_without_residue(pg_connection_factory):
    """Live up -> down -> up. `CREATE TABLE`/`CREATE FUNCTION` are not
    `IF NOT EXISTS`/`OR REPLACE`, so a down that forgot either object would
    break the second up — which nothing else in the suite catches."""
    from command_center.db import migrations

    def present(conn):
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM pg_proc WHERE proname='backlog_mark_duplicate'"
            )
            function = cur.fetchone()[0]
            cur.execute("SELECT to_regclass('public.backlog_duplicate')")
            table = cur.fetchone()[0]
        return function, table

    with pg_connection_factory() as conn:
        migrations.upgrade(conn)
        assert present(conn) == (1, "backlog_duplicate")
        migrations.downgrade(conn, target=24)
        assert present(conn) == (0, None)
        migrations.upgrade(conn)  # must not raise 'already exists'
        assert present(conn) == (1, "backlog_duplicate")
