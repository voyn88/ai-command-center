"""The queue-claim protocol (0002), proved against a real PostgreSQL server.

Every claim this suite makes is one the database itself decides, so none of it
can be faked out by a stub: exclusivity is `FOR UPDATE SKIP LOCKED` under real
parallel connections, the refusals are the server's, the recovery is a `SIGKILL`
of a real process, and the privilege matrix is exercised *as the roles*, never
as the superuser — a denial observed from a superuser session is a denial the
grant graph never produced.

Two shapes of false green are avoided deliberately, both of which this
protocol's design hit before it was trusted:

* **An assertion that touched nothing.** A statement that matches zero rows
  raises nothing and looks like a constraint holding. Every constraint check
  below targets a row by primary key and asserts that row's state *before* the
  statement that must fail.
* **A privilege denial wearing a constraint's label.** `permission denied for
  table work_item` and `violates check constraint` are both refusals and both
  read as PASS. The constraint checks therefore run as the table owner, where
  no grant can be the reason, and the grant checks are their own tests.

Skipped wholesale unless `AICC_TEST_PG_ADMIN_DSN` is set — see `conftest`.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from command_center.db import migrations, roles

# `serial` because this module is the only one that creates and drops
# **cluster-wide** objects: the per-host LOGIN roles the protocol's identity
# rests on. Roles are not scoped to a database, so that DDL races the
# session-scoped `role_passwords` fixture as each xdist worker starts, and
# PostgreSQL reports it as `tuple concurrently updated` in whichever of the two
# lost — a failure with nothing to do with the code under test. Measured, not
# guessed: `pytest tests/db -n auto` fails this way and `-m "not serial"` plus
# the serial tail (what CI runs) does not.
pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

QUEUE = "execution"

#: Long enough that nothing below expires by accident, short enough that the
#: deliberate-expiry tests do not dominate the suite's wall time.
LONG_VISIBILITY = 300
SHORT_VISIBILITY = 1


# ---------------------------------------------------------------------------
# Provisioning — the production order, which is also the only order that proves
# the grants: bootstrap as superuser, migrate as the migrator, grant as the
# owner of the tables the migration just created.
# ---------------------------------------------------------------------------


def _as_role(dsn: str, role: str, password: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=password)
    return make_conninfo(**params)


def _provision(admin_conn, psycopg, test_dsn, role_passwords) -> None:
    roles.apply_bootstrap(admin_conn)
    with psycopg.connect(
        _as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]),
        autocommit=True,
    ) as conn:
        migrations.upgrade(conn)
        roles.apply_table_grants(conn)


@contextmanager
def _worker_hosts(admin_conn, psycopg, test_dsn, count: int):
    """`count` per-host LOGIN roles, created the way production creates them.

    The protocol's identity is the role PostgreSQL authenticated, so a suite
    that ran every "different worker" through the one shared `aicc_worker`
    login would be unable to tell exclusivity from identity: two sessions with
    the same `session_user` cannot demonstrate that `not_claimant` discriminates
    anything. These are `render_worker_host_role()`'s output, so the mechanism
    under test is the one that would ship.
    """
    from psycopg import sql

    suffix = secrets.token_hex(4)
    names = [f"aicc_wh_{suffix}_{index}" for index in range(count)]
    password = secrets.token_urlsafe(24)
    try:
        with admin_conn.cursor() as cur:
            for name in names:
                for statement in roles.render_worker_host_role(name):
                    cur.execute(statement)
                cur.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(name), sql.Literal(password)
                    )
                )
        yield [_as_role(test_dsn, name, password) for name in names], names
    finally:
        with admin_conn.cursor() as cur:
            for name in names:
                try:
                    cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
                except Exception:  # noqa: BLE001 — cleanup must not mask a failure
                    admin_conn.rollback()


def _claim_migration():
    """The claim migration by NAME, not by position in the set.

    `discover()[-1]` meant "the claim migration" only while it was the last one;
    once a later migration lands, the same expression keeps passing while
    measuring something else entirely.
    """
    return next(m for m in migrations.discover() if m.slug == "queue_claim")


def _token() -> tuple[str, str]:
    """A 256-bit capability and the SHA-256 the database will store.

    Generated here, on the worker's side, exactly as a worker process would:
    the preimage never reaches the database, so a role with SELECT on
    `work_attempt`, a backup, or a log cannot impersonate the holder.
    """
    token = secrets.token_hex(32)
    return token, hashlib.sha256(token.encode("utf-8")).hexdigest()


def _enqueue(conn, key: str, **kwargs) -> str:
    payload = json.dumps(kwargs.pop("payload", {"job": key}))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT queue_enqueue(%s, %s, %s::jsonb, NULL, %s, %s, %s, %s, %s)",
            (
                kwargs.pop("queue", QUEUE),
                key,
                payload,
                kwargs.pop("repository_id", None),
                kwargs.pop("max_attempts", 3),
                kwargs.pop("priority", 0),
                kwargs.pop("delay_seconds", 0),
                kwargs.pop("backoff_seconds", 0),
            ),
        )
        assert not kwargs, kwargs
        return cur.fetchone()[0]


def _claim(conn, token_hash: str, *, queue: str = QUEUE, visibility: int = LONG_VISIBILITY):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM queue_claim(%s, %s, %s)", (queue, token_hash, visibility))
        return cur.fetchone()  # (ok, reason, item, attempt, attempt_no, until, payload)


def _call(conn, sql_text: str, params):
    with conn.cursor() as cur:
        cur.execute(sql_text, params)
        return cur.fetchone()


def _item(admin_conn, item_id: str) -> tuple:
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT state, attempt_count, max_attempts, current_attempt_id, result_id, "
            "dead_reason FROM work_item WHERE work_item_id = %s",
            (item_id,),
        )
        return cur.fetchone()


def _events(admin_conn, item_id: str) -> list[tuple]:
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT event, outcome, reason, actor_role, seq FROM work_event "
            "WHERE work_item_id = %s ORDER BY seq",
            (item_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Exclusivity, under real concurrency
# ---------------------------------------------------------------------------


def test_only_one_of_sixteen_concurrent_claimers_wins(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Sixteen connections, one ready item, one barrier.

    Sequential calls would prove nothing here: the second would simply find the
    item no longer `ready`. The exclusivity claim is about what happens when
    sixteen transactions reach the same row inside the same instant, so they
    have to actually do that — separate connections released together, not one
    connection called sixteen times.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE]), autocommit=True
    ) as app:
        item_id = _enqueue(app, "solo")

    with _worker_hosts(admin_conn, psycopg, test_dsn, 4) as (host_dsns, _names):
        barrier = threading.Barrier(16)
        verdicts: list[object] = [None] * 16

        def claim(index: int) -> None:
            try:
                with psycopg.connect(host_dsns[index % 4], autocommit=True) as conn:
                    barrier.wait(timeout=30)
                    verdicts[index] = _claim(conn, _token()[1])
            except Exception as exc:  # noqa: BLE001 — recorded and asserted below
                verdicts[index] = exc

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)

    assert not [v for v in verdicts if isinstance(v, Exception)], verdicts
    winners = [v for v in verdicts if v[0]]
    assert len(winners) == 1, [v[1] for v in verdicts]

    # Every loser was refused cleanly rather than blocked or errored: SKIP
    # LOCKED means the contended row is invisible, not queued behind.
    assert {v[1] for v in verdicts if not v[0]} == {"no_work"}

    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_attempt WHERE work_item_id = %s", (item_id,))
        assert cur.fetchone()[0] == 1


def test_concurrent_workers_never_hand_one_item_to_two_owners(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Twelve workers draining twenty-four items: every item claimed exactly once."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE]), autocommit=True
    ) as app:
        for index in range(24):
            _enqueue(app, f"item-{index}")

    claimed: list[str] = []
    lock = threading.Lock()

    with _worker_hosts(admin_conn, psycopg, test_dsn, 4) as (host_dsns, _names):
        barrier = threading.Barrier(12)
        errors: list[Exception] = []

        def drain(index: int) -> None:
            try:
                with psycopg.connect(host_dsns[index % 4], autocommit=True) as conn:
                    barrier.wait(timeout=30)
                    while True:
                        verdict = _claim(conn, _token()[1])
                        if not verdict[0]:
                            return
                        with lock:
                            claimed.append(verdict[2])
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=drain, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

    assert not errors, errors
    assert len(claimed) == 24
    assert len(set(claimed)) == 24, "an item was handed to two workers"

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM work_item i WHERE (SELECT count(*) FROM work_attempt a "
            "WHERE a.work_item_id = i.work_item_id AND a.state = 'active') > 1"
        )
        assert cur.fetchone()[0] == 0


def test_a_claimer_is_never_blocked_by_another_transactions_row_lock(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """`SKIP LOCKED` buys throughput, not correctness — pinned as what it is.

    This test exists because the claim it replaces was false. The migration used
    to say three guards produced exclusivity and that the concurrency tests
    measured all three; independent acceptance ran the mutations and found that
    removing `SKIP LOCKED`, removing the `state='ready'` CAS, and making the
    attempt index non-unique each left the entire suite green. Reproduced here
    before writing this: all three, and all three together, still pass.

    What actually refuses the loser is the row lock plus the predicate. Under
    READ COMMITTED a waiter re-evaluates `state='ready'` after the winner
    commits, finds it false, and gets no row — with or without `SKIP LOCKED`.

    So `SKIP LOCKED` is pinned for the property it really has: a claimer steps
    past a row another transaction is holding instead of queuing behind it. With
    a plain `FOR UPDATE` the call below blocks on the held lock until
    `statement_timeout` cancels it, which is a failure this test can see.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "held")

    migrator_dsn = _as_role(
        test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]
    )

    # A transaction holding the only claimable row, and not letting go.
    with psycopg.connect(migrator_dsn) as holder:
        with holder.cursor() as cur:
            cur.execute(
                "SELECT work_item_id FROM work_item WHERE work_item_id = %s FOR UPDATE",
                (item_id,),
            )
            assert cur.fetchone()[0] == item_id

        with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
            with psycopg.connect(host_dsns[0], autocommit=True) as worker:
                with worker.cursor() as cur:
                    cur.execute("SET statement_timeout = '4s'")
                started = time.monotonic()
                verdict = _claim(worker, _token()[1])
                elapsed = time.monotonic() - started

        # Stepped past it rather than waiting for it.
        assert verdict[0] is False and verdict[1] == "no_work"
        assert elapsed < 2.0, f"the claimer queued behind the held row ({elapsed:.2f}s)"
        holder.rollback()

    # And the row was never in doubt: releasing the hold makes it claimable again,
    # so the refusal above was the lock, not the item having gone somewhere.
    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            assert _claim(worker, _token()[1])[0] is True


def test_the_attempt_number_is_unique_per_item(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The backstop, pinned for existing rather than for producing exclusivity.

    Making this index non-unique leaves every concurrency test green, because
    the row lock and the predicate mean two claimers never reach the same
    `attempt_no` to begin with. So the honest assertion is the direct one: the
    database refuses a duplicate. Run as the table owner, where no grant can be
    the reason for the refusal, and against a row whose existence is asserted
    first so a statement that matched nothing cannot pass for a constraint.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE]), autocommit=True
    ) as app:
        item_id = _enqueue(app, "unique-attempt")

    migrator_dsn = _as_role(
        test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]
    )
    insert = (
        "INSERT INTO work_attempt (attempt_id, work_item_id, attempt_no, "
        "claimed_by_role, claim_token_hash, visibility_seconds, visible_until, "
        "state, created_at, updated_at) VALUES "
        "(%s, %s, 1, session_user, %s, 60, now() + interval '1 min', "
        "'active', now(), now())"
    )
    with psycopg.connect(migrator_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(insert, ("wat_first", item_id, "0" * 64))
            cur.execute(
                "SELECT count(*) FROM work_attempt WHERE work_item_id = %s AND attempt_no = 1",
                (item_id,),
            )
            assert cur.fetchone()[0] == 1, "the first attempt must exist for this to mean anything"

    with psycopg.connect(migrator_dsn) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation) as raised:
            with conn.cursor() as cur:
                cur.execute(insert, ("wat_second", item_id, "0" * 64))
    assert "idx_work_attempt_item_no" in str(raised.value)


def test_one_claim_call_consumes_exactly_one_attempt(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The counter, not just the verdict.

    `queue_claim` returns a composite, and PostgreSQL evaluates
    `SELECT (queue_claim(…)).*` **once per output column** — seven calls, seven
    attempts consumed, a plausible-looking verdict and complete silence. Every
    call site here uses `SELECT * FROM f(…)`, which evaluates once; this asserts
    the consequence rather than the spelling, because a correct-looking result
    is exactly what the wrong form produces.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE]), autocommit=True
    ) as app:
        item_id = _enqueue(app, "counted", max_attempts=5)

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            verdict = _claim(worker, _token()[1])
            assert verdict[0] and verdict[4] == 1

    state = _item(admin_conn, item_id)
    assert state[1] == 1, "one call must spend one attempt"
    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_attempt WHERE work_item_id = %s", (item_id,))
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM work_event WHERE work_item_id = %s AND event = 'claim' "
            "AND outcome = 'granted'",
            (item_id,),
        )
        assert cur.fetchone()[0] == 1


def test_no_call_site_uses_the_per_column_expansion_form() -> None:
    """`(f(x)).*` and `(f(x)).ok` re-evaluate `f` once per referenced column.

    Cheap to write, silent when wrong, and it survives review because the result
    looks right. Pinned as a rule about this module's own source so a later
    addition cannot reintroduce it — the counter test above would catch it for
    `queue_claim`, but not for a call added to some other function.
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))

    # Docstrings are excluded by identity rather than by pattern: the paragraph
    # above names the bad form on purpose, and a rule that cannot tell the
    # warning from the mistake gets deleted the first time it fires on prose.
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }

    bad_form = re.compile(r"\(\s*queue_\w+\s*\([^)]*\)\s*\)\s*\.")

    offenders = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and bad_form.search(node.value)
    ]
    assert offenders == [], offenders

    # The rule is shown to fire, so an empty result above means "none present"
    # rather than "the pattern never matches anything". Assembled from pieces so
    # the sample is not itself a string constant the scan would report — a
    # control that trips its own gate gets deleted rather than fixed.
    sample = "SELECT (" + "queue_claim" + "(%s)).*"
    assert bad_form.search(sample), sample


def test_queue_claim_takes_no_actor_argument(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """There is nothing to spoof, and nothing to forget to validate.

    The defect this closes (`VOYN-W0-AICC-AUTH-HTTP-01`) is an executor that
    names itself in a request body. A validated `actor` parameter would be a
    weaker fix than no parameter at all, so the absence is asserted from
    `pg_proc` rather than left to the reader of the migration.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT p.proargnames FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'queue_claim'"
        )
        argument_names = cur.fetchone()[0]

    assert argument_names == ["p_queue", "p_claim_token_hash", "p_visibility_seconds"]
    assert not [a for a in argument_names if "actor" in a or "role" in a or "user" in a]


def test_the_claimant_cannot_be_declared(admin_conn, psycopg, test_dsn, role_passwords):
    """Belt and braces over the grants: even the table owner cannot forge one.

    Run as the owner deliberately. As a worker the insert fails on the missing
    privilege, which is a real refusal by the wrong mechanism and would leave
    the trigger untested.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE]), autocommit=True
    ) as app:
        item_id = _enqueue(app, "forge")

    migrator_dsn = _as_role(
        test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]
    )
    with psycopg.connect(migrator_dsn) as conn:
        with pytest.raises(psycopg.errors.InvalidAuthorizationSpecification):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO work_attempt (attempt_id, work_item_id, attempt_no, "
                    "claimed_by_role, claim_token_hash, visibility_seconds, visible_until, "
                    "state, created_at, updated_at) VALUES "
                    "('wat_forged', %s, 1, 'aicc_worker', %s, 60, now() + interval '1 min', "
                    "'active', now(), now())",
                    (item_id, "0" * 64),
                )

    # The same insert naming the connected role succeeds, so the refusal above
    # was the claimant check and not the row failing for some other reason.
    with psycopg.connect(migrator_dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO work_attempt (attempt_id, work_item_id, attempt_no, "
                "claimed_by_role, claim_token_hash, visibility_seconds, visible_until, "
                "state, created_at, updated_at) VALUES "
                "('wat_honest', %s, 1, session_user, %s, 60, now() + interval '1 min', "
                "'active', now(), now())",
                (item_id, "0" * 64),
            )


# ---------------------------------------------------------------------------
# The stale owner
# ---------------------------------------------------------------------------


def test_a_stale_owner_cannot_record_a_result(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The acceptance clause, and the audit trail that proves each refusal ran.

    Two refusals are exercised, not one: the deadline alone (before any reaper
    has run) and the attempt's own state (after the takeover). Asserting only
    the second would leave the window between expiry and the next reap open.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "stale")

    with _worker_hosts(admin_conn, psycopg, test_dsn, 2) as (host_dsns, _names):
        token_a, hash_a = _token()
        with psycopg.connect(host_dsns[0], autocommit=True) as worker_a:
            verdict = _claim(worker_a, hash_a, visibility=SHORT_VISIBILITY)
            assert verdict[0], verdict[1]
            attempt_a = verdict[3]

            time.sleep(SHORT_VISIBILITY + 0.5)

            # No sweeper has run: the item still says `claimed`, and the refusal
            # below is therefore delivered by the deadline alone.
            assert _item(admin_conn, item_id)[0] == "claimed"

            refusal = _call(
                worker_a,
                "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                (attempt_a, token_a, json.dumps({"done": True})),
            )
            assert refusal == (False, "claim_expired")
            assert _call(
                worker_a, "SELECT ok, reason FROM queue_heartbeat(%s, %s)", (attempt_a, token_a)
            ) == (False, "claim_expired")

            with psycopg.connect(app_dsn, autocommit=True) as app:
                assert _call(app, "SELECT queue_reap()", ())[0] == 1

            token_b, hash_b = _token()
            with psycopg.connect(host_dsns[1], autocommit=True) as worker_b:
                taken = _claim(worker_b, hash_b)
                assert taken[0] and taken[2] == item_id
                assert taken[4] == 2, "the takeover is attempt 2, not a re-run of attempt 1"

                # A's claim is now refused by the attempt's own state.
                assert _call(
                    worker_a,
                    "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                    (attempt_a, token_a, json.dumps({"done": True})),
                ) == (False, "attempt_expired")
                assert _call(
                    worker_a,
                    "SELECT ok, reason FROM queue_fail(%s, %s, %s, true)",
                    (attempt_a, token_a, "late"),
                ) == (False, "attempt_expired")

                with admin_conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM work_result WHERE work_item_id = %s", (item_id,)
                    )
                    assert cur.fetchone()[0] == 0, "the stale owner wrote a result"

                completion = _call(
                    worker_b,
                    "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                    (taken[3], token_b, json.dumps({"done": True})),
                )
                assert completion == (True, None)

    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_result WHERE work_item_id = %s", (item_id,))
        assert cur.fetchone()[0] == 1, "exactly one acknowledged result per item"

    # Every refusal is durably on the record. They returned rather than raising,
    # so nothing rolled the audit back with the transaction that wrote it.
    rejected = [e for e in _events(admin_conn, item_id) if e[1] == "rejected"]
    assert [e[2] for e in rejected] == [
        "claim_expired",
        "claim_expired",
        "attempt_expired",
        "attempt_expired",
    ]


def test_the_fence_refuses_an_owner_whose_item_moved_on(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The guard that needs no clock, no reaper, and no proof of death.

    The refusals above were delivered by the deadline and by the attempt's own
    state, so on that evidence alone the fence could be dead code. This is the
    case it exists for: an attempt still `active` and still inside its
    visibility window, whose item has nonetheless moved on — a half-failed
    reaper, or bookkeeping rolled back. Constructed as the owner, because no
    granted path can produce it.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "fenced")

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        token, token_hash = _token()
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            verdict = _claim(worker, token_hash)
            attempt_id = verdict[3]
            assert _call(
                worker,
                "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                (attempt_id, token, json.dumps({"first": True})),
            ) == (True, None)

            state = _item(admin_conn, item_id)
            assert state[0] == "succeeded" and state[3] is None

            # Force the attempt back to a live claim. The item has moved on.
            with admin_conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_attempt SET state = 'active', result_id = NULL, "
                    "visible_until = now() + interval '1 hour' WHERE attempt_id = %s",
                    (attempt_id,),
                )
                assert cur.rowcount == 1

            assert _call(
                worker,
                "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                (attempt_id, token, json.dumps({"second": True})),
            ) == (False, "attempt_superseded")

    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_result WHERE work_item_id = %s", (item_id,))
        assert cur.fetchone()[0] == 1


def test_a_leaked_token_used_by_another_role_is_refused(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Possession of the capability is necessary and not sufficient.

    The role says which host may act; the token says which process on that host
    holds this attempt. A leaked token replayed from another host is refused on
    the role, which is why both exist.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE]), autocommit=True
    ) as app:
        _enqueue(app, "leaked")

    with _worker_hosts(admin_conn, psycopg, test_dsn, 2) as (host_dsns, names):
        token, token_hash = _token()
        with psycopg.connect(host_dsns[0], autocommit=True) as worker_a:
            verdict = _claim(worker_a, token_hash)
            attempt_id = verdict[3]

        with psycopg.connect(host_dsns[1], autocommit=True) as worker_b:
            assert _call(
                worker_b,
                "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                (attempt_id, token, json.dumps({"stolen": True})),
            ) == (False, "not_claimant")
            # A wrong token from the right role is refused on the token, so the
            # two guards are shown to be independent rather than one test twice.
            assert _call(
                worker_b,
                "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                (attempt_id, "wrong", json.dumps({"stolen": True})),
            ) == (False, "bad_claim_token")

    with admin_conn.cursor() as cur:
        cur.execute("SELECT claimed_by_role FROM work_attempt WHERE attempt_id = %s", (attempt_id,))
        assert cur.fetchone()[0] == names[0]


# ---------------------------------------------------------------------------
# Recovery: process death, and a partition that is not process death
# ---------------------------------------------------------------------------


_CHILD = textwrap.dedent(
    """
    import hashlib, json, sys, time
    import psycopg

    dsn, queue, token = sys.argv[1], sys.argv[2], sys.argv[3]
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    conn = psycopg.connect(dsn)          # autocommit off: one explicit transaction
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM queue_claim(%s, %s, %s)", (queue, token_hash, 300))
        verdict = cur.fetchone()
    conn.commit()
    assert verdict[0], verdict[1]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
            (verdict[3], token, json.dumps({"done": True})),
        )
        completed = cur.fetchone()

    print(json.dumps({
        "backend_pid": conn.info.backend_pid,
        "work_item_id": verdict[2],
        "attempt_id": verdict[3],
        "completed_ok": completed[0],
        "completed_reason": completed[1],
    }), flush=True)

    # The window, held open rather than timed. The completion above is applied
    # and not committed; everything the protocol makes externally observable
    # about it must still be invisible when this process is killed.
    time.sleep(600)
    """
)


def test_a_sigkilled_worker_exposes_no_terminal_state_before_it_is_durable(
    admin_conn, psycopg, test_dsn, role_passwords, tmp_path
):
    """No observable state of an attempt precedes what it denotes being durable.

    A randomly timed `SIGKILL` cannot reproduce a millisecond window; it would
    produce a false green. So the window is *stretched* rather than the timing
    tuned — the child holds its transaction open at exactly the point where the
    completion is applied and not yet committed, leaving the sequence of
    operations untouched. The child reports `completed_ok`, which is what proves
    the kill lands inside the window and not before or after it.

    The detector is shown to work before it is used to clear the protocol: an
    external reader polls throughout, and
    `test_a_half_acknowledged_item_is_unrepresentable` demonstrates that a
    terminal state written without its evidence is refused by the database
    rather than merely absent from the API.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "killed")

    script = tmp_path / "worker_child.py"
    script.write_text(_CHILD, encoding="utf-8")

    with _worker_hosts(admin_conn, psycopg, test_dsn, 2) as (host_dsns, _names):
        token = secrets.token_hex(32)
        child = subprocess.Popen(
            [sys.executable, str(script), host_dsns[0], QUEUE, token],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            line = child.stdout.readline()
            assert line, child.stderr.read()
            reported = json.loads(line)
            assert reported["completed_ok"] is True, reported
            assert reported["work_item_id"] == item_id

            # The external reader: an independent connection, polling through
            # the whole held window. It must never observe the terminal state.
            for _ in range(20):
                assert _item(admin_conn, item_id)[0] == "claimed"
                with admin_conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM work_result WHERE work_item_id = %s", (item_id,)
                    )
                    assert cur.fetchone()[0] == 0
                time.sleep(0.02)

            os.kill(child.pid, signal.SIGKILL)
            child.wait(timeout=30)
        finally:
            if child.poll() is None:  # pragma: no cover — only on an assertion above
                child.kill()
                child.wait(timeout=30)

        # Wait for the server to notice: the row locks the dead backend held are
        # released asynchronously, and reaping before that would block on them.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            with admin_conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid = %s",
                    (reported["backend_pid"],),
                )
                if cur.fetchone()[0] == 0:
                    break
            time.sleep(0.1)
        else:  # pragma: no cover — the child was killed, the backend must go
            pytest.fail("the killed worker's backend never went away")

        # Neither half committed.
        state = _item(admin_conn, item_id)
        assert state[0] == "claimed" and state[4] is None
        with admin_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM work_result WHERE work_item_id = %s", (item_id,))
            assert cur.fetchone()[0] == 0
            cur.execute(
                "SELECT state FROM work_attempt WHERE attempt_id = %s", (reported["attempt_id"],)
            )
            assert cur.fetchone()[0] == "active"

        # Recovery needs no proof of death and no cooperation from the corpse:
        # the visibility timeout elapses and the reaper returns the item.
        with admin_conn.cursor() as cur:
            cur.execute(
                "UPDATE work_attempt SET visible_until = now() - interval '1 second' "
                "WHERE attempt_id = %s",
                (reported["attempt_id"],),
            )
            assert cur.rowcount == 1

        with psycopg.connect(app_dsn, autocommit=True) as app:
            assert _call(app, "SELECT queue_reap()", ())[0] == 1

        token_b, hash_b = _token()
        with psycopg.connect(host_dsns[1], autocommit=True) as worker_b:
            taken = _claim(worker_b, hash_b)
            assert taken[0] and taken[2] == item_id and taken[4] == 2
            assert _call(
                worker_b,
                "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                (taken[3], token_b, json.dumps({"done": True})),
            ) == (True, None)

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT state, outcome_reason FROM work_attempt WHERE work_item_id = %s "
            "ORDER BY attempt_no",
            (item_id,),
        )
        assert cur.fetchall() == [("expired", "visibility_timeout"), ("succeeded", None)]
        cur.execute("SELECT count(*) FROM work_result WHERE work_item_id = %s", (item_id,))
        assert cur.fetchone()[0] == 1, "at-least-once execution, exactly-once acknowledgement"


def test_a_partitioned_owner_is_alive_and_still_refused(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The dangerous case, and the one that distinguishes this from a lease.

    A `SIGKILL` leaves nothing behind to make a wrong decision. A partition
    leaves a process that is alive and believes its claim is current. Requiring
    proof of death before re-delivery — which is right for a repository writer
    lease, where a second writer corrupts a worktree irreversibly — would mean a
    worker on an unreachable host blocks its task forever, the exact opposite of
    this protocol's acceptance. Safety comes from the fence at the write path
    instead, which is what this test shows: re-delivered while the owner lives,
    and the owner refused when it wakes.

    The owner's session is held open and idle throughout, which is what a
    partition looks like from the server's side; `pg_stat_activity` is asserted
    rather than assumed so "the owner was alive" is measured, not narrated.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "partitioned")

    with _worker_hosts(admin_conn, psycopg, test_dsn, 2) as (host_dsns, _names):
        token_a, hash_a = _token()
        with psycopg.connect(host_dsns[0], autocommit=True) as owner:
            verdict = _claim(owner, hash_a, visibility=SHORT_VISIBILITY)
            assert verdict[0], verdict[1]
            owner_pid = owner.info.backend_pid

            time.sleep(SHORT_VISIBILITY + 0.5)

            with admin_conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (owner_pid,)
                )
                assert cur.fetchone()[0] == 1, "the owner's backend is gone; this is not a partition"

            with psycopg.connect(app_dsn, autocommit=True) as app:
                assert _call(app, "SELECT queue_reap()", ())[0] == 1

            token_b, hash_b = _token()
            with psycopg.connect(host_dsns[1], autocommit=True) as worker_b:
                taken = _claim(worker_b, hash_b)
                assert taken[0] and taken[2] == item_id

                # The owner wakes and reconnects to a world that moved on.
                assert _call(
                    owner,
                    "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                    (verdict[3], token_a, json.dumps({"done": True})),
                ) == (False, "attempt_expired")

                assert _call(
                    worker_b,
                    "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                    (taken[3], token_b, json.dumps({"done": True})),
                ) == (True, None)

    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_result WHERE work_item_id = %s", (item_id,))
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Acknowledgement only after a durable result
# ---------------------------------------------------------------------------


def test_a_half_acknowledged_item_is_unrepresentable(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """`succeeded` implies a result — even for a role holding direct DML.

    Run as the table owner, so the refusal cannot be a missing grant wearing a
    constraint's label, and targeted at one row by primary key whose state is
    asserted first, so a statement that matched nothing cannot be mistaken for
    the constraint holding.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE]), autocommit=True
    ) as app:
        item_id = _enqueue(app, "half")

    assert _item(admin_conn, item_id)[:1] == ("ready",)
    assert _item(admin_conn, item_id)[4] is None

    migrator_dsn = _as_role(
        test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]
    )
    with psycopg.connect(migrator_dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation) as raised:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_item SET state = 'succeeded' WHERE work_item_id = %s",
                    (item_id,),
                )
    assert "work_item_succeeded_has_result" in str(raised.value)

    # And the inverse half-state: a result recorded on an item that never says so.
    with psycopg.connect(migrator_dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_item SET state = 'claimed' WHERE work_item_id = %s", (item_id,)
                )


def test_completion_without_a_result_is_refused_by_the_signature_itself(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """There is no `queue_ack()`.

    The result is a required argument of the only function that can reach
    `succeeded`, so the window "acknowledged but the result was lost" is not
    expressible through the granted API rather than merely guarded against.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE]), autocommit=True
    ) as app:
        _enqueue(app, "ackless")

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname LIKE 'queue\\_%%' "
            "AND p.proname NOT IN "
            "('queue_enqueue','queue_claim','queue_heartbeat','queue_complete',"
            "'queue_fail','queue_reap','queue_redrive')"
        )
        assert cur.fetchone()[0] == 0, "an eighth queue entry point appeared"

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        token, token_hash = _token()
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            verdict = _claim(worker, token_hash)
            assert _call(
                worker,
                "SELECT ok, reason FROM queue_complete(%s, %s, NULL)",
                (verdict[3], token),
            ) == (False, "result_required")


def test_every_protocol_entry_point_is_a_function_not_a_procedure(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """A `PROCEDURE` can `COMMIT` part-way; a function cannot.

    That is what makes each state and the rows justifying it become visible at
    the same instant — they are the same commit — rather than making it a rule
    about how the callers are written.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT p.proname, p.prokind FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND (p.proname LIKE 'queue\\_%%' "
            "OR p.proname LIKE '\\_queue\\_%%')"
        )
        kinds = dict(cur.fetchall())

    assert kinds, "no queue functions found"
    assert set(kinds.values()) == {"f"}, kinds


# ---------------------------------------------------------------------------
# Bounded retry and the dead-letter queue
# ---------------------------------------------------------------------------


def test_retry_is_bounded_and_exhaustion_dead_letters(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The budget is spent at the item, so the bound is data rather than caller discipline."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "bounded", max_attempts=2, backoff_seconds=0)

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            token, token_hash = _token()
            first = _claim(worker, token_hash)
            assert first[0] and first[4] == 1
            assert _call(
                worker,
                "SELECT ok, reason FROM queue_fail(%s, %s, %s, true)",
                (first[3], token, "boom"),
            ) == (True, "requeued")
            assert _item(admin_conn, item_id)[0] == "ready"

            token, token_hash = _token()
            second = _claim(worker, token_hash)
            assert second[0] and second[4] == 2
            assert _call(
                worker,
                "SELECT ok, reason FROM queue_fail(%s, %s, %s, true)",
                (second[3], token, "boom again"),
            ) == (True, "dead_lettered")

            # The budget is spent; the queue hands out nothing more.
            assert _claim(worker, _token()[1])[1] == "no_work"

    state = _item(admin_conn, item_id)
    assert state[0] == "dead"
    assert state[1] == 2 and state[2] == 2
    assert state[5].startswith("max_attempts_exhausted:")


def test_a_non_retryable_failure_dead_letters_without_spending_the_budget(
    admin_conn, psycopg, test_dsn, role_passwords
):
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "fatal", max_attempts=5)

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            token, token_hash = _token()
            verdict = _claim(worker, token_hash)
            assert _call(
                worker,
                "SELECT ok, reason FROM queue_fail(%s, %s, %s, false)",
                (verdict[3], token, "malformed payload"),
            ) == (True, "dead_lettered")

    state = _item(admin_conn, item_id)
    assert state[0] == "dead" and state[1] == 1 and state[2] == 5
    assert state[5] == "non_retryable: malformed payload"


def test_the_dead_letter_view_preserves_the_cause_and_the_history(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The DLQ is a view, so there is no second authority over "this item failed".

    A dedicated table would need its own insert path, its own consistency rule
    against `work_item.state`, and a redrive moving rows between two tables
    non-atomically. The dead-lettered item already *is* the complete record.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "dlq", max_attempts=2, backoff_seconds=0)

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            for reason in ("first failure", "second failure"):
                token, token_hash = _token()
                verdict = _claim(worker, token_hash)
                _call(
                    worker,
                    "SELECT ok, reason FROM queue_fail(%s, %s, %s, true)",
                    (verdict[3], token, reason),
                )

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT relkind FROM pg_class WHERE relname = 'work_dlq'"
        )
        assert cur.fetchone()[0] == "v", "the dead-letter queue must not be a second table"

    with psycopg.connect(app_dsn, autocommit=True) as app:
        row = _call(
            app,
            "SELECT dead_reason, attempts_recorded, last_attempt_reason, max_attempts "
            "FROM work_dlq WHERE work_item_id = %s",
            (item_id,),
        )
    assert row[0] == "max_attempts_exhausted: second failure"
    assert row[1] == 2, "every attempt that led here is preserved beside the cause"
    assert row[2] == "second failure"


def test_redrive_returns_a_dead_item_without_resetting_its_history(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """A redrive loop cannot silently grant infinite retries.

    `attempt_count` keeps counting and the budget is widened explicitly, so each
    widening is a recorded act rather than a reset that looks like a fresh item.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "redriven", max_attempts=1, backoff_seconds=0)

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            token, token_hash = _token()
            verdict = _claim(worker, token_hash)
            _call(
                worker,
                "SELECT ok, reason FROM queue_fail(%s, %s, %s, true)",
                (verdict[3], token, "transient"),
            )
        assert _item(admin_conn, item_id)[0] == "dead"

        with psycopg.connect(app_dsn, autocommit=True) as app:
            assert _call(app, "SELECT queue_redrive(%s, 2)", (item_id,))[0] is True

        state = _item(admin_conn, item_id)
        assert state[0] == "ready"
        assert state[1] == 1, "the attempt history is not reset"
        assert state[2] == 3, "the budget is widened explicitly"
        assert state[5] is None

        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            retaken = _claim(worker, _token()[1])
            assert retaken[0] and retaken[4] == 2, "the retry continues the numbering"


def test_redrive_of_an_unknown_item_is_refused_and_the_refusal_survives(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The audit's own foreign key must not turn a refusal into an exception.

    `work_event.work_item_id` references `work_item`, so auditing a refusal
    *against the id that was not found* would abort the transaction on the audit
    write — losing the record of the refusal, which is precisely the failure
    mode the return-rather-than-raise rule exists to prevent. The unknown id
    goes into `detail`, where nothing references it.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])

    with psycopg.connect(app_dsn, autocommit=True) as app:
        assert _call(app, "SELECT queue_redrive(%s, 1)", ("wki_does_not_exist",))[0] is False
        live_id = _enqueue(app, "not-dead")
        assert _call(app, "SELECT queue_redrive(%s, 1)", (live_id,))[0] is False

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT reason, detail ->> 'requested_work_item_id' FROM work_event "
            "WHERE event = 'redrive' AND outcome = 'rejected' AND work_item_id IS NULL"
        )
        assert cur.fetchall() == [("unknown_work_item", "wki_does_not_exist")]

    assert [e[2] for e in _events(admin_conn, live_id) if e[1] == "rejected"] == [
        "not_dead_lettered"
    ]


# ---------------------------------------------------------------------------
# The audit, and the rule that makes it survivable
# ---------------------------------------------------------------------------


def test_a_refusal_is_audited_because_it_returned_rather_than_raised(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Measured, not assumed: `RAISE` would roll back the audit it just wrote.

    An exception aborts the transaction that contains the audit row, so a
    protocol that refuses by raising has no durable record of its refusals. This
    asserts the property the rule buys — the row is there afterwards — and the
    control is its neighbour: a statement that *does* raise inside the same
    connection leaves nothing behind.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "audited")

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        token, token_hash = _token()
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            verdict = _claim(worker, token_hash)
            assert _call(
                worker,
                "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                (verdict[3], "not-the-token", json.dumps({})),
            ) == (False, "bad_claim_token")

    events = _events(admin_conn, item_id)
    rejected = [e for e in events if e[1] == "rejected"]
    assert [(e[0], e[2]) for e in rejected] == [("complete", "bad_claim_token")]

    # The actor is derived the same way the claimant is: `session_user`, which
    # no argument of any function in the migration can influence.
    assert {e[3] for e in events} <= {roles.APP_ROLE, *_names}

    # Gap-free per item, so a deleted audit row is detectable.
    assert [e[4] for e in events] == list(range(1, len(events) + 1))


#: Every `_queue_audit()` call site in `0002_queue_claim.up.sql`, as the row it
#: must produce. Deleting any one of them has to fail a test, which is the whole
#: point: independent acceptance removed the reaper's audit write and the suite
#: stayed green, so "every decision is audited" was a property of the migration's
#: prose rather than of the gate — the same shape of hole as a store nothing
#: reconciles, and it surfaces at the moment the audit is finally needed.
#:
#: A static scan cannot carry this. There are a dozen call sites and they differ
#: only in their arguments, so a missing one looks exactly like a site that was
#: never there. The rows are the evidence; the source is not.
REQUIRED_AUDIT_SITES = (
    ("enqueue", "granted", None),
    ("enqueue", "rejected", "duplicate_idempotency_key"),
    ("claim", "granted", None),
    ("claim", "rejected", "bad_claim_token_hash"),
    ("claim", "rejected", "attempt_budget_exhausted"),
    ("heartbeat", "granted", None),
    ("heartbeat", "rejected", "bad_claim_token"),
    ("complete", "granted", None),
    ("complete", "rejected", "bad_claim_token"),
    ("fail", "granted", "requeued"),
    ("fail", "granted", "dead_lettered"),
    ("fail", "rejected", "attempt_expired"),
    ("expire", "granted", "requeued"),
    ("expire", "granted", "dead_lettered"),
    ("redrive", "granted", None),
    ("redrive", "rejected", "not_dead_lettered"),
    ("redrive", "rejected", "unknown_work_item"),
)

#: The one branch no test reaches, recorded rather than quietly missing from the
#: list above. `lost_claim_race` fires when the `state='ready'` CAS on the claim
#: UPDATE is false, and the row lock taken by the SELECT immediately before it
#: means nothing can make that happen. It is defence in depth against a future
#: change that removes the lock; see the comment at that line in the migration.
UNREACHABLE_AUDIT_SITES = (("claim", "rejected", "lost_claim_race"),)


def test_every_reachable_audit_site_writes_a_row(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Drive every audited decision and assert each one left its record.

    Written after independent acceptance deleted the reaper's `_queue_audit`
    call and nothing failed. One assertion per call site rather than a single
    lifecycle walk, because a lifecycle walk covers whichever branches it
    happens to take and is silent about the rest — the reaper's two branches
    among them.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        worker_dsn = host_dsns[0]

        # enqueue: granted, then rejected as a duplicate.
        with psycopg.connect(app_dsn, autocommit=True) as app:
            requeued_item = _enqueue(app, "audit-requeue", max_attempts=3, backoff_seconds=0)
            _enqueue(app, "audit-requeue")

        with psycopg.connect(worker_dsn, autocommit=True) as worker:
            # claim: rejected on a malformed token, then granted.
            assert _claim(worker, "too-short")[1] == "bad_claim_token_hash"
            token, token_hash = _token()
            first = _claim(worker, token_hash)
            assert first[0], first[1]

            # heartbeat: rejected, then granted.
            assert _call(
                worker, "SELECT ok, reason FROM queue_heartbeat(%s, %s)", (first[3], "wrong")
            ) == (False, "bad_claim_token")
            assert _call(
                worker, "SELECT ok FROM queue_heartbeat(%s, %s)", (first[3], token)
            ) == (True,)

            # complete: rejected on the token (granted comes later, on its own item).
            assert _call(
                worker,
                "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                (first[3], "wrong", json.dumps({})),
            ) == (False, "bad_claim_token")

            # fail: granted/requeued.
            assert _call(
                worker,
                "SELECT ok, reason FROM queue_fail(%s, %s, %s, true)",
                (first[3], token, "transient"),
            ) == (True, "requeued")

            # expire: granted/requeued — the reaper branch that returns an item.
            token, token_hash = _token()
            second = _claim(worker, token_hash, visibility=SHORT_VISIBILITY)
            assert second[0], second[1]
            with admin_conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_attempt SET visible_until = now() - interval '1 second' "
                    "WHERE attempt_id = %s",
                    (second[3],),
                )
            with psycopg.connect(app_dsn, autocommit=True) as app:
                assert _call(app, "SELECT queue_reap()", ())[0] == 1
            assert _item(admin_conn, requeued_item)[0] == "ready"

            # fail: rejected — the stale owner of the attempt the reaper expired.
            assert _call(
                worker,
                "SELECT ok, reason FROM queue_fail(%s, %s, %s, true)",
                (second[3], token, "late"),
            ) == (False, "attempt_expired")

            # expire: granted/dead_lettered — the reaper branch that exhausts.
            token, token_hash = _token()
            third = _claim(worker, token_hash, visibility=SHORT_VISIBILITY)
            assert third[0] and third[4] == 3
            with admin_conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_attempt SET visible_until = now() - interval '1 second' "
                    "WHERE attempt_id = %s",
                    (third[3],),
                )
            with psycopg.connect(app_dsn, autocommit=True) as app:
                assert _call(app, "SELECT queue_reap()", ())[0] == 1
            assert _item(admin_conn, requeued_item)[0] == "dead"

            # redrive: rejected on an unknown id, granted on the dead item, then
            # rejected because it is no longer dead.
            with psycopg.connect(app_dsn, autocommit=True) as app:
                assert _call(app, "SELECT queue_redrive(%s, 1)", ("wki_nope",))[0] is False
                assert _call(app, "SELECT queue_redrive(%s, 2)", (requeued_item,))[0] is True
                assert _call(app, "SELECT queue_redrive(%s, 1)", (requeued_item,))[0] is False

            # fail: granted/dead_lettered, on a non-retryable failure.
            #
            # Priorities from here on, because the redrive above put its item
            # back on the queue: without them the next claim takes whichever row
            # sorts first and the test asserts against the wrong item.
            with psycopg.connect(app_dsn, autocommit=True) as app:
                fatal_item = _enqueue(app, "audit-fatal", max_attempts=2, priority=10)
            token, token_hash = _token()
            fatal = _claim(worker, token_hash)
            assert fatal[0] and fatal[2] == fatal_item
            assert _call(
                worker,
                "SELECT ok, reason FROM queue_fail(%s, %s, %s, false)",
                (fatal[3], token, "malformed"),
            ) == (True, "dead_lettered")

            # complete: granted.
            with psycopg.connect(app_dsn, autocommit=True) as app:
                complete_item = _enqueue(app, "audit-complete", priority=20)
            token, token_hash = _token()
            done = _claim(worker, token_hash)
            assert done[0] and done[2] == complete_item
            assert _call(
                worker,
                "SELECT ok FROM queue_complete(%s, %s, %s::jsonb)",
                (done[3], token, json.dumps({"done": True})),
            ) == (True,)

            # claim: rejected on an exhausted budget. Forced as the owner — the
            # failure and expiry paths dead-letter such items, so reaching it
            # through the API alone is not possible, and leaving it out of the
            # list would make the gate silent about a real audit site.
            with psycopg.connect(app_dsn, autocommit=True) as app:
                stuck = _enqueue(app, "audit-exhausted", max_attempts=1, priority=30)
            with admin_conn.cursor() as cur:
                cur.execute(
                    "UPDATE work_item SET attempt_count = max_attempts WHERE work_item_id = %s",
                    (stuck,),
                )
                assert cur.rowcount == 1
            assert _claim(worker, _token()[1])[1] == "attempt_budget_exhausted"

    with admin_conn.cursor() as cur:
        cur.execute("SELECT event, outcome, reason FROM work_event")
        observed = {tuple(row) for row in cur.fetchall()}

    missing = [site for site in REQUIRED_AUDIT_SITES if site not in observed]
    assert missing == [], f"audited decisions that left no row: {missing}"

    # The registry must not drift into describing rows nothing produces, which
    # would let a deleted site hide behind an entry that was always aspirational.
    for site in UNREACHABLE_AUDIT_SITES:
        assert site not in observed, f"{site} is reachable after all; move it into the required set"

    # And the observed set must not contain an audited decision the registry has
    # never heard of — a new site added without a row here is a site nothing
    # pins, which is exactly how the reaper's went unnoticed.
    unregistered = observed - set(REQUIRED_AUDIT_SITES) - set(UNREACHABLE_AUDIT_SITES)
    assert unregistered == set(), f"audit sites with no entry in REQUIRED_AUDIT_SITES: {unregistered}"


def test_the_reaper_audits_both_of_its_branches(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Named separately because this is the site acceptance deleted.

    The sweep above would catch it, but a reader looking for "is re-delivery
    audited?" should find a test that says so rather than have to trust a
    registry entry.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "reaped", max_attempts=2, backoff_seconds=0)

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            for _ in range(2):
                verdict = _claim(worker, _token()[1], visibility=SHORT_VISIBILITY)
                assert verdict[0], verdict[1]
                with admin_conn.cursor() as cur:
                    cur.execute(
                        "UPDATE work_attempt SET visible_until = now() - interval '1 second' "
                        "WHERE attempt_id = %s",
                        (verdict[3],),
                    )
                with psycopg.connect(app_dsn, autocommit=True) as app:
                    assert _call(app, "SELECT queue_reap()", ())[0] == 1

    expiries = [e for e in _events(admin_conn, item_id) if e[0] == "expire"]
    assert [(e[1], e[2]) for e in expiries] == [
        ("granted", "requeued"),
        ("granted", "dead_lettered"),
    ]
    # The actor is the reaper's connected role, not the worker whose attempt it
    # expired — the one place the audit's actor and the attempt's owner differ.
    assert {e[3] for e in expiries} == {roles.APP_ROLE}


def test_enqueue_is_idempotent_per_queue_and_key(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """At-least-once delivery of the *enqueue* produces duplicate work otherwise.

    No amount of claim-side exclusivity can merge two items that both legitimately
    exist, so the guard has to be at the enqueue boundary.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        first = _enqueue(app, "same-key")
        second = _enqueue(app, "same-key")
        other_queue = _enqueue(app, "same-key", queue="other")

    assert first == second
    assert other_queue != first, "the key is scoped to its queue"

    assert [e[2] for e in _events(admin_conn, first) if e[1] == "rejected"] == [
        "duplicate_idempotency_key"
    ]
    with admin_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM work_item")
        assert cur.fetchone()[0] == 2


def test_a_heartbeat_cannot_widen_its_own_lease(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The caller supplies no duration, so it cannot extend past its own policy."""
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE]), autocommit=True
    ) as app:
        _enqueue(app, "beating")

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT p.proargnames FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND p.proname = 'queue_heartbeat'"
        )
        assert cur.fetchone()[0] == ["p_attempt_id", "p_claim_token"]

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        token, token_hash = _token()
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            verdict = _claim(worker, token_hash, visibility=5)
            beat = _call(
                worker, "SELECT ok, visible_until FROM queue_heartbeat(%s, %s)", (verdict[3], token)
            )
            assert beat[0] is True

    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT visibility_seconds, extract(epoch from (visible_until - now())) "
            "FROM work_attempt WHERE attempt_id = %s",
            (verdict[3],),
        )
        seconds, remaining = cur.fetchone()
        assert seconds == 5
        assert 0 < remaining <= 5, "the heartbeat recomputed the deadline from the row"


def test_repository_id_is_provenance_and_gates_nothing(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Gating dispatch on it would be a repository lease in the wrong database.

    The claim answers "who is executing attempt N of item W". Who may *mutate*
    repository R is a different question, answered elsewhere and verified at the
    mutation site; a claim-time check would be stale by the time a git command
    ran and would create a second, weaker authority over it.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])
    with psycopg.connect(app_dsn, autocommit=True) as app:
        _enqueue(app, "repo-a", repository_id="repo-1")
        _enqueue(app, "repo-b", repository_id="repo-1")

    with _worker_hosts(admin_conn, psycopg, test_dsn, 2) as (host_dsns, _names):
        with psycopg.connect(host_dsns[0], autocommit=True) as one:
            with psycopg.connect(host_dsns[1], autocommit=True) as two:
                first = _claim(one, _token()[1])
                second = _claim(two, _token()[1])

    assert first[0] and second[0], "two items of one repository must both be claimable"
    assert first[2] != second[2]


# ---------------------------------------------------------------------------
# The grant graph, exercised as the roles themselves
# ---------------------------------------------------------------------------


QUEUE_RELATIONS = (
    "work_item",
    "work_attempt",
    "work_result",
    "work_event",
    "work_item_public",
    "work_attempt_public",
    "work_dlq",
)


def test_a_worker_host_reaches_the_protocol_and_nothing_else(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Exercised as the host role, which is the only way this proves anything.

    A compromised execution host can claim work from its queue, and that is all:
    no table privilege, no enqueue, no reap, no redrive, and no read of other
    workers' claims or of the audit. Enumerating the queue would itself be a
    disclosure — what else is pending, and which hosts hold it — so a worker's
    own work arrives in the claim verdict instead.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with psycopg.connect(
        _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE]), autocommit=True
    ) as app:
        _enqueue(app, "least-privilege")

    with _worker_hosts(admin_conn, psycopg, test_dsn, 1) as (host_dsns, _names):
        for relation in QUEUE_RELATIONS:
            with psycopg.connect(host_dsns[0]) as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with conn.cursor() as cur:
                        cur.execute(f"SELECT * FROM {relation}")

        for statement, params in (
            ("SELECT queue_enqueue(%s, %s, '{}'::jsonb)", (QUEUE, "smuggled")),
            ("SELECT queue_reap()", ()),
            ("SELECT queue_redrive(%s, 1)", ("wki_anything",)),
        ):
            with psycopg.connect(host_dsns[0]) as conn:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    with conn.cursor() as cur:
                        cur.execute(statement, params)

        # The four it may run, so the denials above are not a broken connection.
        with psycopg.connect(host_dsns[0], autocommit=True) as worker:
            token, token_hash = _token()
            verdict = _claim(worker, token_hash)
            assert verdict[0], verdict[1]
            assert _call(
                worker, "SELECT ok FROM queue_heartbeat(%s, %s)", (verdict[3], token)
            ) == (True,)
            assert _call(
                worker,
                "SELECT ok FROM queue_complete(%s, %s, %s::jsonb)",
                (verdict[3], token, json.dumps({"done": True})),
            ) == (True,)


def test_the_control_plane_can_schedule_and_observe_but_not_execute(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """A compromised control plane cannot forge an execution record.

    It enqueues, reaps and redrives — all audited — and it reads. It cannot
    claim, so no attempt can be attributed to a worker that did not authenticate
    as one; and it cannot write the queue tables directly, so "an item became
    dead" has exactly one code path, which is also the one that audits.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    app_dsn = _as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])

    with psycopg.connect(app_dsn, autocommit=True) as app:
        item_id = _enqueue(app, "control-plane")
        for relation in ("work_item", "work_result", "work_event", "work_item_public", "work_dlq"):
            _call(app, f"SELECT count(*) FROM {relation}", ())

    with psycopg.connect(app_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM queue_claim(%s, %s, 60)", (QUEUE, "0" * 64))

    with psycopg.connect(app_dsn) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM work_attempt")

    for statement in (
        "UPDATE work_item SET state = 'dead' WHERE work_item_id = %s",
        "INSERT INTO work_result (result_id, attempt_id, work_item_id, payload, created_at) "
        "VALUES ('r', 'a', %s, '{}'::jsonb, now())",
    ):
        with psycopg.connect(app_dsn) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                with conn.cursor() as cur:
                    cur.execute(statement, (item_id,))

    # It reads attempts through the view, which omits the capability itself.
    with psycopg.connect(app_dsn, autocommit=True) as app:
        with app.cursor() as cur:
            cur.execute("SELECT * FROM work_attempt_public LIMIT 0")
            assert "claim_token_hash" not in [d.name for d in cur.description]


# ---------------------------------------------------------------------------
# Reversibility
# ---------------------------------------------------------------------------


def _schema_snapshot(conn) -> dict:
    """Everything a downgrade could leave behind, in one comparable value."""
    snapshot: dict[str, list] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name, data_type, is_nullable, column_default, is_identity "
            "FROM information_schema.columns WHERE table_schema = 'public' "
            "ORDER BY table_name, ordinal_position"
        )
        snapshot["columns"] = cur.fetchall()
        cur.execute(
            "SELECT c.relname, c.relkind FROM pg_class c JOIN pg_namespace n "
            "ON n.oid = c.relnamespace WHERE n.nspname = 'public' ORDER BY c.relname, c.relkind"
        )
        snapshot["relations"] = cur.fetchall()
        cur.execute(
            "SELECT conrelid::regclass::text, conname, pg_get_constraintdef(oid) "
            "FROM pg_constraint WHERE connamespace = 'public'::regnamespace ORDER BY 1, 2"
        )
        snapshot["constraints"] = cur.fetchall()
        cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'public' ORDER BY 1")
        snapshot["indexes"] = cur.fetchall()
        cur.execute(
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' ORDER BY 1, 2"
        )
        snapshot["functions"] = cur.fetchall()
        cur.execute(
            "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'public' ORDER BY 1"
        )
        snapshot["types"] = cur.fetchall()
        cur.execute(
            "SELECT tgname, tgrelid::regclass::text FROM pg_trigger WHERE NOT tgisinternal ORDER BY 1"
        )
        snapshot["triggers"] = cur.fetchall()
    return snapshot


def test_up_down_up_down_leaves_the_schema_byte_identical(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The test is not "does it drop the tables".

    This migration introduces a candidate authority (`work_item`) alongside an
    existing mirror (`queue_entry`). A downgrade that left any part of the new
    authority behind would leave the database holding two partial claims on the
    same question — the duplicate authority the project rules forbid outright.
    So the assertion is that the schema afterwards is indistinguishable from the
    schema before, twice over.
    """
    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    migrator_dsn = _as_role(
        test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]
    )
    assert _claim_migration().version == 2, "this test pins the claim migration"

    with psycopg.connect(migrator_dsn, autocommit=True) as conn:
        # Strip everything above the claim migration first, and drive the cycle
        # with an explicit target afterwards. Reading `discover()[-1]` was
        # correct while the claim migration was the last one and would have gone
        # on passing while quietly measuring a later migration instead.
        migrations.downgrade(conn, target=1)
        before = _schema_snapshot(admin_conn)

        assert migrations.upgrade(conn, target=2) == (2,)
        with_claim = _schema_snapshot(admin_conn)
        assert with_claim != before, "the snapshot notices nothing; it would pass on anything"

        assert migrations.downgrade(conn, target=1) == (2,)
        assert _schema_snapshot(admin_conn) == before

        assert migrations.upgrade(conn, target=2) == (2,)
        assert _schema_snapshot(admin_conn) == with_claim

        assert migrations.downgrade(conn, target=1) == (2,)
        assert _schema_snapshot(admin_conn) == before


def test_the_queue_mirror_is_untouched_by_this_migration(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """`queue_entry` sits outside the parity gate, so its shape may not change.

    Altering it would be a data migration with no coverage. The claim protocol
    therefore stands beside the mirror rather than reshaping it, and retiring
    the mirror belongs to the JSON-queue contraction, not here.
    """
    claim_migration = _claim_migration()
    statements = [
        line
        for sql_text in (claim_migration.up_sql, claim_migration.down_sql)
        for line in sql_text.splitlines()
        if "queue_entry" in line and not line.strip().startswith("--")
    ]
    assert statements == [], statements

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'queue_entry' "
            "ORDER BY ordinal_position"
        )
        after = cur.fetchall()

    migrator_dsn = _as_role(
        test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]
    )
    with psycopg.connect(migrator_dsn, autocommit=True) as conn:
        migrations.downgrade(conn, target=1)
    with admin_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = 'queue_entry' "
            "ORDER BY ordinal_position"
        )
        assert cur.fetchall() == after
