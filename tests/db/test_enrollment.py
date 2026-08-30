"""The worker-host enrolment protocol (0003), against a real PostgreSQL server.

Every claim here is one the database itself decides, so none of it can be faked
out by a stub: the single-use guarantee is a row lock under real parallel
connections, the refusals are the server's, the per-host roles are real cluster
roles that real connections authenticate against, and the privilege matrix is
exercised *as the roles* — a denial observed from a superuser session is a
denial the grant graph never produced.

Three shapes of false green are avoided deliberately.

* **A privilege denial wearing a protocol refusal's label.** `permission denied
  for function enroll_mint_ticket` and a returned `tier_violation` are both
  refusals and both read as PASS. They are separate tests here, and the ones
  about the protocol run as a role that is granted the function.
* **An audit assertion that never had a row to find.** Every refusal test
  counts `principal_event` before and after, because a denial audited and then
  discarded by a rollback looks exactly like a denial that was never audited —
  and that is the specific defect this migration is built around.
* **A reversibility test that only checks tables.** The migration creates
  CLUSTER objects, so the downgrade test asserts the per-host LOGIN roles are
  gone too. A downgrade that dropped the record of a role while leaving the
  login is worse than no downgrade at all.

Skipped wholesale unless `AICC_TEST_PG_ADMIN_DSN` is set — see `conftest`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from contextlib import contextmanager

import pytest

from command_center.db import migrations, roles

# `serial` for the reason `test_queue_claim` is: this module creates and drops
# **cluster-wide** roles, which are not scoped to a database, so that DDL races
# the session-scoped `role_passwords` fixture as each xdist worker starts and
# PostgreSQL reports whichever lost as `tuple concurrently updated`.
pytestmark = [pytest.mark.serial, pytest.mark.usefixtures("role_passwords")]

OPERATOR_PRINCIPAL = "operator:root"
CONTROL_PLANE_PRINCIPAL = "control-plane"

#: A descriptor the way a host reports itself. Unattested by construction:
#: every field is a string the host typed about itself.
DESCRIPTOR = {
    "machine_id": "9f2c1d",
    "os": "linux",
    "arch": "x86_64",
    "hostname": "srv-a",
}


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
        # The first two principals cannot be enrolled by the enrolment protocol
        # — something has to be first — so provisioning creates them as the
        # owner, which is the only role `identity_bootstrap_principal` is
        # reachable from.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT identity_bootstrap_principal(%s, 'operator', %s, %s)",
                (OPERATOR_PRINCIPAL, roles.OPERATOR_ROLE, "Root operator"),
            )
            cur.execute(
                "SELECT identity_bootstrap_principal(%s, 'control_plane', %s, %s)",
                (CONTROL_PLANE_PRINCIPAL, roles.APP_ROLE, "Control plane"),
            )


@contextmanager
def _cluster(admin_conn, psycopg, test_dsn, role_passwords):
    """Provision, and drop the per-host roles the test creates afterwards.

    Per-host roles are cluster objects: dropping the per-test database does not
    remove them, and a leftover `aicc_w_*` login would leak into the next test
    as a name collision — or, in production, as exactly the unrecorded login
    this protocol exists to prevent.
    """
    from psycopg import sql

    _provision(admin_conn, psycopg, test_dsn, role_passwords)
    try:
        yield
    finally:
        with admin_conn.cursor() as cur:
            cur.execute(r"SELECT rolname FROM pg_roles WHERE rolname LIKE 'aicc\_w\_%'")
            for (name,) in cur.fetchall():
                cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))


def _dsn_for(test_dsn: str, role: str, role_passwords: dict[str, str]) -> str:
    return _as_role(test_dsn, role, role_passwords[role])


def _secret() -> tuple[str, str]:
    """A 256-bit secret and the SHA-256 the database will store.

    Generated on the caller's side, exactly as the minter and the enrolling host
    would: the preimage never reaches the database, so a backup, a log line or a
    role with SELECT on the table cannot impersonate the holder.
    """
    value = secrets.token_hex(32)
    return value, hashlib.sha256(value.encode("utf-8")).hexdigest()


def _scram_verifier(password: str, *, iterations: int = 4096) -> str:
    """A SCRAM-SHA-256 verifier, computed here rather than by the server.

    This is the half that makes "the plaintext never travels" true: the host
    computes it locally and the database stores what PostgreSQL authenticates
    against, so the password appears in no statement, no `log_statement` output
    and no `pg_stat_activity.query`.
    """
    salt = os.urandom(16)
    salted = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
    stored_key = hashlib.sha256(client_key).digest()
    server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
    return "SCRAM-SHA-256${}:{}${}:{}".format(
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(stored_key).decode("ascii"),
        base64.b64encode(server_key).decode("ascii"),
    )


def _unique(prefix: str = "worker") -> str:
    return f"{prefix}:{secrets.token_hex(4)}"


def _mint(conn, principal_id: str, ticket_hash: str, **kwargs):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM enroll_mint_ticket(%s, %s, %s, %s, %s, %s)",
            (
                principal_id,
                kwargs.pop("host", principal_id.split(":", 1)[-1] + ".local"),
                ticket_hash,
                kwargs.pop("expected_cidr", None),
                kwargs.pop("ttl", None),
                kwargs.pop("purpose", "enroll"),
            ),
        )
        assert not kwargs, kwargs
        return cur.fetchone()  # (minted_ticket_id, refuse_reason)


def _redeem(conn, ticket: str, secret_hash: str, verifier: str, descriptor: dict):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM enroll_redeem_ticket(%s, %s, %s, %s::jsonb)",
            (ticket, secret_hash, verifier, json.dumps(descriptor)),
        )
        # (principal_id, db_role, credential_id, expires_at, refuse_reason)
        return cur.fetchone()


def _enrol(app_conn, principal_id: str, descriptor: dict | None = None, **kwargs):
    """Mint and redeem in one step. Returns (row, host_secret)."""
    ticket, ticket_hash = _secret()
    minted = _mint(app_conn, principal_id, ticket_hash, **kwargs)
    assert minted[1] is None, minted
    host_secret, host_hash = _secret()
    row = _redeem(
        app_conn, ticket, host_hash, _scram_verifier(host_secret), descriptor or DESCRIPTOR
    )
    return row, host_secret


def _events(conn, principal_id: str | None = None):
    with conn.cursor() as cur:
        if principal_id is None:
            cur.execute(
                "SELECT event_type, outcome, reason, actor_db_role "
                "FROM principal_event ORDER BY id"
            )
        else:
            cur.execute(
                "SELECT event_type, outcome, reason, actor_db_role "
                "FROM principal_event WHERE principal_id = %s ORDER BY id",
                (principal_id,),
            )
        return cur.fetchall()


def _count_events(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM principal_event")
        return cur.fetchone()[0]


def _denied(psycopg, conn, statement: str, params=()) -> str:
    """Run `statement` expecting an insufficient-privilege error; return it.

    Narrowed to `InsufficientPrivilege` on purpose: a typo in a table name also
    raises, also reads as a refusal, and would let a test claim the grant graph
    denied something it never even reached.
    """
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as excinfo:
        with conn.cursor() as cur:
            cur.execute(statement, params)
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# What makes a ticket a different object from the shared password
# ---------------------------------------------------------------------------


def test_a_ticket_is_not_a_database_login(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The first of the four properties, and the one most easily assumed.

    A bootstrap secret that could connect would be the shared `aicc_worker`
    password with a shorter life. This asserts the negative directly: the ticket
    secret authenticates as nothing, and minting one creates no role.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        principal = _unique()
        ticket, ticket_hash = _secret()
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            minted = _mint(app, principal, ticket_hash)
        assert minted[1] is None, minted

        # No role appeared: enrolment, not minting, is what creates one.
        with admin_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'aicc\\_w\\_%'")
            assert cur.fetchone()[0] == 0

        # And the secret itself is not a credential for any existing role.
        for user in (roles.APP_ROLE, roles.WORKER_ROLE, roles.OPERATOR_ROLE):
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(_as_role(test_dsn, user, ticket), connect_timeout=5)


def test_a_ticket_lifetime_is_clamped_by_the_database_not_the_caller(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """A caller asking for 30 days gets the policy ceiling, from the server clock."""
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        _, ticket_hash = _secret()
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            minted = _mint(app, _unique(), ticket_hash, ttl="30 days")
            assert minted[1] is None, minted
            with app.cursor() as cur:
                cur.execute(
                    "SELECT expires_at - issued_at FROM enrollment_ticket_public "
                    "WHERE ticket_id = %s",
                    (minted[0],),
                )
                granted = cur.fetchone()[0]
        assert granted.total_seconds() == 15 * 60, granted


def test_a_ticket_produces_only_the_principal_it_named(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Pre-binding: a stolen ticket cannot enrol a host of the thief's choosing.

    The redeeming call carries a descriptor and nothing else that names an
    identity — there is deliberately no parameter through which a caller could
    ask for a different principal — so this asserts the produced identity equals
    the one fixed at mint time and that no second principal appeared.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        intended = _unique("worker-intended")
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            row, _secret_value = _enrol(app, intended)
        assert row[4] is None, row
        assert row[0] == intended

        with admin_conn.cursor() as cur:
            cur.execute("SELECT principal_id FROM principal WHERE kind = 'worker_host'")
            assert [r[0] for r in cur.fetchall()] == [intended]


# ---------------------------------------------------------------------------
# Single use, under real concurrency
# ---------------------------------------------------------------------------


def test_two_concurrent_redemptions_produce_one_enrolment_and_one_loud_refusal(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The single-use guarantee, under two real connections racing a row lock.

    Serialised redemptions would prove the state machine and say nothing about
    the lock, and the lock is the whole mechanism: without it both callers read
    `state = 'issued'` and both proceed. Both threads are released from a
    barrier so they are inside the function at the same time.

    The refusal is checked three ways, because "the loser was refused" is the
    property the theft alarm rests on: it returns a reason, it returns NO
    credential (so a caller that ignores the reason still has nothing to hand
    the host), and its denial is in `principal_event` AFTER the call returned.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        principal = _unique()
        ticket, ticket_hash = _secret()
        app_dsn = _dsn_for(test_dsn, roles.APP_ROLE, role_passwords)
        with psycopg.connect(app_dsn, autocommit=True) as app:
            assert _mint(app, principal, ticket_hash)[1] is None

        before = _count_events(admin_conn)
        barrier = threading.Barrier(2)
        results: list[tuple] = [None, None]  # type: ignore[list-item]

        def redeem(index: int) -> None:
            host_secret, host_hash = _secret()
            with psycopg.connect(app_dsn, autocommit=True) as conn:
                barrier.wait(timeout=30)
                results[index] = _redeem(
                    conn, ticket, host_hash, _scram_verifier(host_secret), DESCRIPTOR
                )

        threads = [threading.Thread(target=redeem, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
            assert not thread.is_alive()

        winners = [r for r in results if r[4] is None]
        losers = [r for r in results if r[4] is not None]
        assert len(winners) == 1, results
        assert len(losers) == 1, results
        assert losers[0][4] == "ticket_redeemed"
        # The loser received no credential and no role: a caller that ignored
        # the reason would still have nothing to give the host.
        assert losers[0][:4] == (None, None, None, None)

        # THE ALARM IS DURABLE. This is the assertion the whole "return, do not
        # raise" rule exists for: the denial outlived the call that wrote it.
        assert _count_events(admin_conn) > before
        refusals = [
            e for e in _events(admin_conn) if e[1] == "rejected" and e[2] == "ticket_redeemed"
        ]
        assert len(refusals) == 1, refusals

        # And exactly one credential exists for the one principal.
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM principal_credential WHERE principal_id = %s",
                (principal,),
            )
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT state, use_count FROM enrollment_ticket_public")
            assert cur.fetchall() == [("redeemed", 1)]


def test_a_redeemed_ticket_cannot_be_replayed(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The same guarantee without the race, so a lock regression is not the only
    thing that could make the concurrency test above go quiet."""
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        principal = _unique()
        ticket, ticket_hash = _secret()
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            assert _mint(app, principal, ticket_hash)[1] is None
            first_secret, first_hash = _secret()
            first = _redeem(
                app, ticket, first_hash, _scram_verifier(first_secret), DESCRIPTOR
            )
            assert first[4] is None, first

            before = _count_events(admin_conn)
            second_secret, second_hash = _secret()
            second = _redeem(
                app, ticket, second_hash, _scram_verifier(second_secret), DESCRIPTOR
            )
        assert second == (None, None, None, None, "ticket_redeemed")
        assert _count_events(admin_conn) > before

        # The `CHECK` is the belt to the state machine's braces: even the table
        # owner cannot record a second use.
        with pytest.raises(psycopg.errors.CheckViolation):
            with admin_conn.cursor() as cur:
                cur.execute("UPDATE enrollment_ticket SET use_count = 2")


# ---------------------------------------------------------------------------
# Two hosts are two identities
# ---------------------------------------------------------------------------


def test_two_hosts_enrolled_concurrently_get_different_roles_and_credentials(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The whole point of enrolment, stated as the thing that must be true.

    Under the shared password these two would be indistinguishable, and the
    compromise of either would be the compromise of both.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        first, second = _unique("worker-a"), _unique("worker-b")
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            row_a, secret_a = _enrol(app, first)
            row_b, secret_b = _enrol(
                app, second, {**DESCRIPTOR, "machine_id": "other", "hostname": "srv-b"}
            )
        assert row_a[4] is None and row_b[4] is None, (row_a, row_b)
        assert row_a[1] != row_b[1], "two hosts, one database role"
        assert row_a[2] != row_b[2], "two hosts, one credential"

        # Each authenticates to PostgreSQL as its OWN role and passes the gate.
        for row, secret in ((row_a, secret_a), (row_b, secret_b)):
            with psycopg.connect(
                _as_role(test_dsn, row[1], secret), autocommit=True
            ) as host, host.cursor() as cur:
                cur.execute("SELECT ok, reason, principal_id FROM identity_assert(%s)", (secret,))
                assert cur.fetchone() == (True, None, row[0])


def test_a_credential_is_bound_to_its_principals_database_role(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Host B presenting host A's secret is refused, and audited as `act_as`.

    The residual this does NOT close is stated in the migration and is worth
    restating: possession is the whole proof, so anything holding host A's
    secret *and* able to authenticate as host A's role is indistinguishable from
    host A. What is bound — and is enforced per statement — is the role.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            row_a, secret_a = _enrol(app, _unique("worker-a"))
            row_b, secret_b = _enrol(
                app, _unique("worker-b"), {**DESCRIPTOR, "machine_id": "other"}
            )

        before = _count_events(admin_conn)
        with psycopg.connect(
            _as_role(test_dsn, row_b[1], secret_b), autocommit=True
        ) as host_b, host_b.cursor() as cur:
            cur.execute("SELECT ok, reason FROM identity_assert(%s)", (secret_a,))
            assert cur.fetchone() == (False, "principal_role_mismatch")
        assert _count_events(admin_conn) > before
        assert ("act_as", "rejected", "principal_role_mismatch", row_b[1]) in _events(
            admin_conn, row_a[0]
        )


# ---------------------------------------------------------------------------
# Expiry, revocation of a ticket, and the sweeper
# ---------------------------------------------------------------------------


def test_an_expired_ticket_is_refused_and_closed_out(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Both halves, because the close-out UPDATE is the other thing a `RAISE`
    used to discard: the redemption is refused AND the ticket stops being
    `issued`, in the transaction that refused it."""
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        ticket, ticket_hash = _secret()
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            minted = _mint(app, _unique(), ticket_hash)
            assert minted[1] is None, minted
            # Reach past the deadline as the owner rather than sleeping: the
            # property under test is that the comparison is made against the
            # SERVER's clock, not how long a test is willing to wait.
            with admin_conn.cursor() as cur:
                cur.execute(
                    "UPDATE enrollment_ticket SET expires_at = issued_at + interval '1 second' "
                    "WHERE ticket_id = %s",
                    (minted[0],),
                )
                cur.execute(
                    "UPDATE enrollment_ticket SET issued_at = now() - interval '1 hour', "
                    "expires_at = now() - interval '59 minutes' WHERE ticket_id = %s",
                    (minted[0],),
                )

            before = _count_events(admin_conn)
            host_secret, host_hash = _secret()
            row = _redeem(app, ticket, host_hash, _scram_verifier(host_secret), DESCRIPTOR)

        assert row == (None, None, None, None, "ticket_expired")
        assert _count_events(admin_conn) > before
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM enrollment_ticket_public WHERE ticket_id = %s", (minted[0],)
            )
            assert cur.fetchone()[0] == "expired", "the close-out UPDATE was rolled back"


def test_an_expired_ticket_is_refused_regardless_of_the_clients_clock(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Every deadline is the server's `now()`; no client clock is written or read.

    The client's session timezone and `SET LOCAL` cannot move `now()`, so the
    check runs with the connection's clock deliberately misconfigured.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        ticket, ticket_hash = _secret()
        app_dsn = _dsn_for(test_dsn, roles.APP_ROLE, role_passwords)
        with psycopg.connect(app_dsn, autocommit=True) as app:
            minted = _mint(app, _unique(), ticket_hash)
            with admin_conn.cursor() as cur:
                cur.execute(
                    "UPDATE enrollment_ticket SET issued_at = now() - interval '1 hour', "
                    "expires_at = now() - interval '30 minutes' WHERE ticket_id = %s",
                    (minted[0],),
                )
            with app.cursor() as cur:
                cur.execute("SET TIME ZONE 'Pacific/Kiritimati'")
            host_secret, host_hash = _secret()
            row = _redeem(app, ticket, host_hash, _scram_verifier(host_secret), DESCRIPTOR)
        assert row[4] == "ticket_expired"


def test_an_operator_can_revoke_a_ticket_in_flight(
    admin_conn, psycopg, test_dsn, role_passwords
):
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        ticket, ticket_hash = _secret()
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            minted = _mint(app, _unique(), ticket_hash)
        with psycopg.connect(
            _dsn_for(test_dsn, roles.OPERATOR_ROLE, role_passwords), autocommit=True
        ) as operator, operator.cursor() as cur:
            cur.execute("SELECT enroll_revoke_ticket(%s, %s)", (minted[0], "leaked"))
            assert cur.fetchone()[0] is True
            # Idempotent, like every other revocation in the schema.
            cur.execute("SELECT enroll_revoke_ticket(%s, %s)", (minted[0], "leaked"))
            assert cur.fetchone()[0] is False

        before = _count_events(admin_conn)
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            host_secret, host_hash = _secret()
            row = _redeem(app, ticket, host_hash, _scram_verifier(host_secret), DESCRIPTOR)
        assert row == (None, None, None, None, "ticket_revoked")
        assert _count_events(admin_conn) > before


def test_the_sweeper_closes_out_tickets_for_hosts_that_never_arrived(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Hygiene, not correctness — expiry is enforced on every redemption — so
    what this asserts is that nothing redeemable is left behind."""
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            for _ in range(3):
                _mint(app, _unique(), _secret()[1])
            with admin_conn.cursor() as cur:
                cur.execute(
                    "UPDATE enrollment_ticket SET issued_at = now() - interval '1 hour', "
                    "expires_at = now() - interval '30 minutes'"
                )
            with app.cursor() as cur:
                cur.execute("SELECT enroll_sweep_expired()")
                assert cur.fetchone()[0] == 3
                cur.execute(
                    "SELECT count(*) FROM enrollment_ticket_public WHERE state = 'issued'"
                )
                assert cur.fetchone()[0] == 0
        # No principal, role or credential is left by a host that never came.
        with admin_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM principal WHERE kind = 'worker_host'")
            assert cur.fetchone()[0] == 0
            cur.execute("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'aicc\\_w\\_%'")
            assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# Host churn: rebuild, new hardware, clone, readmission
# ---------------------------------------------------------------------------


def test_a_clone_presenting_a_bound_fingerprint_under_a_new_name_is_refused(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """One compromised image quietly becoming a fleet is the failure this stops.

    What it does NOT catch, and the migration says so: a clone that keeps the
    original's secret and never enrols at all.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        original = _unique("worker-original")
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            row, _ = _enrol(app, original)
            assert row[4] is None, row

            before = _count_events(admin_conn)
            clone, _clone_secret = _enrol(app, _unique("worker-clone"), DESCRIPTOR)
        assert clone == (None, None, None, None, "fingerprint_conflict")
        assert _count_events(admin_conn) > before
        assert any(
            e[1:3] == ("rejected", "fingerprint_conflict") for e in _events(admin_conn)
        )


def test_a_rebuild_and_a_hardware_change_are_both_accepted_and_recorded(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Refusing a hardware change would break every legitimate rebuild while the
    descriptor stays unattested — security theatre with an availability cost. So
    the change is accepted and *recorded*, and the record is what an operator
    reads."""
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        principal = _unique()
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            first, _ = _enrol(app, principal)
            assert first[4] is None, first

            rebuilt, _ = _enrol(app, principal, DESCRIPTOR, purpose="re_enroll")
            assert rebuilt[4] is None, rebuilt

            changed, _ = _enrol(
                app, principal, {**DESCRIPTOR, "machine_id": "new-disk"}, purpose="re_enroll"
            )
            assert changed[4] is None, changed

        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT seq, change_reason FROM worker_host_fingerprint "
                "WHERE principal_id = %s ORDER BY seq",
                (principal,),
            )
            assert cur.fetchall() == [
                (1, None),
                (2, "rebuild"),
                (3, "fingerprint_changed"),
            ]


def test_a_revoked_host_needs_an_operator_to_come_back(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """`principal.state` is the block-list, and readmission is a human decision.

    Four things in one test because they are one property: the control plane
    cannot readmit, cannot sidestep readmission by minting a fresh `enroll`
    ticket for the same name, an operator can, and the readmission is recorded
    as such.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        principal = _unique()
        app_dsn = _dsn_for(test_dsn, roles.APP_ROLE, role_passwords)
        operator_dsn = _dsn_for(test_dsn, roles.OPERATOR_ROLE, role_passwords)

        with psycopg.connect(app_dsn, autocommit=True) as app:
            row, host_secret = _enrol(app, principal)
            assert row[4] is None, row

        with psycopg.connect(operator_dsn, autocommit=True) as operator, operator.cursor() as cur:
            cur.execute("SELECT identity_revoke_principal(%s, %s)", (principal, "incident"))
            assert cur.fetchone()[0] == 1
            cur.execute("SELECT state FROM principal WHERE principal_id = %s", (principal,))
            assert cur.fetchone()[0] == "suspended"

        before = _count_events(admin_conn)
        with psycopg.connect(app_dsn, autocommit=True) as app:
            assert _mint(app, principal, _secret()[1], purpose="re_enroll")[1] == (
                "readmission_requires_operator"
            )
            assert _mint(app, principal, _secret()[1])[1] == "principal_exists"
        assert _count_events(admin_conn) - before == 2, "a refused mint left no record"

        ticket, ticket_hash = _secret()
        with psycopg.connect(operator_dsn, autocommit=True) as operator:
            assert _mint(operator, principal, ticket_hash, purpose="re_enroll")[1] is None

        with psycopg.connect(app_dsn, autocommit=True) as app:
            new_secret, new_hash = _secret()
            readmitted = _redeem(
                app, ticket, new_hash, _scram_verifier(new_secret), DESCRIPTOR
            )
        assert readmitted[4] is None, readmitted

        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT change_reason FROM worker_host_fingerprint "
                "WHERE principal_id = %s ORDER BY seq DESC LIMIT 1",
                (principal,),
            )
            assert cur.fetchone()[0] == "readmitted"

        # And the readmitted host works again, with a NEW secret; the old one
        # does not come back with it.
        with psycopg.connect(
            _as_role(test_dsn, readmitted[1], new_secret), autocommit=True
        ) as host, host.cursor() as cur:
            cur.execute("SELECT ok FROM identity_assert(%s)", (new_secret,))
            assert cur.fetchone() == (True,)
            cur.execute("SELECT ok, reason FROM identity_assert(%s)", (host_secret,))
            assert cur.fetchone() == (False, "credential_revoked")


def test_a_revoked_host_can_no_longer_authenticate_to_postgresql(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Revocation has to reach the connection layer, not only the row.

    PostgreSQL checks the password at authentication time only, so a revocation
    that updated the table and left `pg_authid` alone would leave a leaked
    secret working on every new connection.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        principal = _unique()
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            row, host_secret = _enrol(app, principal)
        host_dsn = _as_role(test_dsn, row[1], host_secret)
        with psycopg.connect(host_dsn, autocommit=True) as before_revocation:
            assert before_revocation.execute("SELECT 1").fetchone() == (1,)

        with psycopg.connect(
            _dsn_for(test_dsn, roles.OPERATOR_ROLE, role_passwords), autocommit=True
        ) as operator, operator.cursor() as cur:
            cur.execute("SELECT identity_revoke_principal(%s, %s)", (principal, "incident"))

        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(host_dsn, connect_timeout=5)


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


def test_a_host_rotates_its_own_secret_without_an_availability_gap(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The ordering IS the argument, so the test asserts the ordering.

    At no instant does `pg_authid` hold a value the host does not know (it
    generated the new secret before the call), and at no instant does the host
    hold zero working connections (the established one is never interrupted).
    Both halves are asserted, plus the negative: the superseded secret stops
    working, at the gate and at the connection.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            row, old_secret = _enrol(app, _unique())
        assert row[4] is None, row

        with psycopg.connect(
            _as_role(test_dsn, row[1], old_secret), autocommit=True
        ) as established:
            assert established.execute("SELECT 1").fetchone() == (1,)

            new_secret, new_hash = _secret()
            with established.cursor() as cur:
                cur.execute(
                    "SELECT * FROM enroll_rotate_self(%s, %s, %s)",
                    (old_secret, new_hash, _scram_verifier(new_secret)),
                )
                rotated = cur.fetchone()
            assert rotated[1] is None, rotated

            # The established connection is untouched — PostgreSQL checks the
            # password at authentication time only.
            assert established.execute("SELECT 1").fetchone() == (1,)
            with established.cursor() as cur:
                cur.execute("SELECT ok, reason FROM identity_assert(%s)", (new_secret,))
                assert cur.fetchone() == (True, None)
                cur.execute("SELECT ok, reason FROM identity_assert(%s)", (old_secret,))
                assert cur.fetchone() == (False, "credential_revoked")

        with psycopg.connect(
            _as_role(test_dsn, row[1], new_secret), autocommit=True
        ) as fresh:
            assert fresh.execute("SELECT 1").fetchone() == (1,)
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(_as_role(test_dsn, row[1], old_secret), connect_timeout=5)

        # The superseded credential is closed out rather than left to disagree
        # with `pg_authid`.
        with admin_conn.cursor() as cur:
            cur.execute(
                "SELECT revoke_reason FROM principal_credential_public "
                "WHERE credential_id = %s",
                (row[2],),
            )
            assert cur.fetchone()[0] == "rotated"


def test_rotating_with_a_superseded_secret_is_refused_and_audited(
    admin_conn, psycopg, test_dsn, role_passwords
):
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            row, old_secret = _enrol(app, _unique())
        new_secret, new_hash = _secret()
        with psycopg.connect(
            _as_role(test_dsn, row[1], old_secret), autocommit=True
        ) as host:
            with host.cursor() as cur:
                cur.execute(
                    "SELECT * FROM enroll_rotate_self(%s, %s, %s)",
                    (old_secret, new_hash, _scram_verifier(new_secret)),
                )
                assert cur.fetchone()[1] is None

            before = _count_events(admin_conn)
            newer_secret, newer_hash = _secret()
            with host.cursor() as cur:
                cur.execute(
                    "SELECT * FROM enroll_rotate_self(%s, %s, %s)",
                    (old_secret, newer_hash, _scram_verifier(newer_secret)),
                )
                assert cur.fetchone() == (None, "credential_revoked")
        assert _count_events(admin_conn) > before, "the refusal rolled its own audit back"


# ---------------------------------------------------------------------------
# The network expectation declared at enrolment
# ---------------------------------------------------------------------------


def test_the_cidr_declared_at_enrolment_is_enforced_for_a_worker_host(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """`expected_cidr` is what an operator declared in advance, so it is checked
    before `bound_addr`, which is only where the credential happened to be used
    first.

    Stated as a limit rather than a guarantee: this measures the ENFORCEMENT
    path against a real `inet_client_addr()`. It does not measure two physical
    hosts — a single-host test cluster cannot produce two client addresses — and
    the control it implements is spoofable at the network layer regardless.
    Closing that needs `pg_hba` host restrictions or client certificates.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            row, host_secret = _enrol(app, _unique(), expected_cidr="10.99.0.0/24")
        assert row[4] is None, row

        before = _count_events(admin_conn)
        # Authentication succeeds — this is not a `pg_hba` rule — and the
        # identity gate is what refuses.
        with psycopg.connect(
            _as_role(test_dsn, row[1], host_secret), autocommit=True
        ) as host, host.cursor() as cur:
            cur.execute("SELECT ok, reason FROM identity_assert(%s)", (host_secret,))
            assert cur.fetchone() == (False, "addr_mismatch")
        assert _count_events(admin_conn) > before


# ---------------------------------------------------------------------------
# The grant graph — measured as the roles, never as the superuser
# ---------------------------------------------------------------------------


def test_the_control_plane_cannot_read_a_secret_or_erase_the_evidence(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """The compromised-control-plane bound, as five separate denials.

    What a compromised control plane CAN do is enrol shadow hosts of its own,
    and that is deliberate — requiring a human for every provisioning event puts
    one on the critical path at 3am. These are the bounds on it.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        app_dsn = _dsn_for(test_dsn, roles.APP_ROLE, role_passwords)
        with psycopg.connect(app_dsn, autocommit=True) as app:
            row, _ = _enrol(app, _unique())
            assert row[4] is None, row

            assert "enrollment_ticket" in _denied(
                psycopg, app, "SELECT ticket_hash FROM enrollment_ticket"
            )
            assert "principal_credential" in _denied(
                psycopg, app, "SELECT secret_hash FROM principal_credential"
            )
            # Nor un-redeem a ticket to replay an enrolment.
            assert "enrollment_ticket" in _denied(
                psycopg, app, "UPDATE enrollment_ticket SET state = 'issued', use_count = 0"
            )
            # Nor erase the fingerprint history that reveals a clone, nor forge one.
            assert "worker_host_fingerprint" in _denied(
                psycopg, app, "SELECT * FROM worker_host_fingerprint"
            )
            assert "worker_host_fingerprint" in _denied(
                psycopg,
                app,
                "INSERT INTO worker_host_fingerprint (principal_id, seq, fingerprint_hash, "
                "descriptor_json, observed_at, created_at) "
                "VALUES ('x', 1, 'y', '{}'::jsonb, now(), now())",
            )
            # Nor un-suspend a host an incident retired: `principal.state` is
            # readable and not writable.
            assert "principal" in _denied(
                psycopg, app, "UPDATE principal SET state = 'active'"
            )
            # Nor delete the audit of what it did.
            assert "principal_event" in _denied(
                psycopg, app, "DELETE FROM principal_event"
            )
            # Nor take a host offline: revocation is the operator's lever.
            assert "identity_revoke_principal" in _denied(
                psycopg, app, "SELECT identity_revoke_principal('x', 'y')"
            )

            # And every mint it performed is on a record it cannot delete.
            with app.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM principal_event WHERE event_type = 'enroll_mint'"
                )
                assert cur.fetchone()[0] >= 1


def test_a_worker_reaches_none_of_the_enrolment_surface(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Enrolment is not peer-to-peer: an enrolled host cannot admit others.

    Measured from a real enrolled host's own connection, so these are denials
    the grant graph produced for the role that ships, not for `aicc_worker` in
    the abstract.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            row, host_secret = _enrol(app, _unique())

        with psycopg.connect(
            _as_role(test_dsn, row[1], host_secret), autocommit=True
        ) as host:
            assert "enroll_mint_ticket" in _denied(
                psycopg, host, "SELECT * FROM enroll_mint_ticket('x', 'y', 'z')"
            )
            assert "enroll_redeem_ticket" in _denied(
                psycopg,
                host,
                "SELECT * FROM enroll_redeem_ticket('x', 'y', 'z', '{}'::jsonb)",
            )
            assert "enroll_revoke_ticket" in _denied(
                psycopg, host, "SELECT enroll_revoke_ticket('x', 'y')"
            )
            assert "identity_revoke_principal" in _denied(
                psycopg, host, "SELECT identity_revoke_principal('x', 'y')"
            )
            # Nor enumerate pending enrolments, other hosts, or their machines.
            assert "enrollment_ticket_public" in _denied(
                psycopg, host, "SELECT * FROM enrollment_ticket_public"
            )
            assert "worker_host_fingerprint" in _denied(
                psycopg, host, "SELECT * FROM worker_host_fingerprint"
            )
            assert "principal" in _denied(psycopg, host, "SELECT * FROM principal")

            # What it CAN do: prove its own identity and rotate its own secret.
            with host.cursor() as cur:
                cur.execute("SELECT ok FROM identity_assert(%s)", (host_secret,))
                assert cur.fetchone() == (True,)


def test_the_tier_gate_refuses_a_worker_even_when_the_grant_does_not(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Defence in depth: the grant is the outer gate and the tier is the inner one.

    The shipping answer to "can a worker mint?" is `permission denied`, asserted
    above. This asserts the second gate holds when the first one does not.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            row, host_secret = _enrol(app, _unique())

        before = _count_events(admin_conn)
        with _mint_granted_to_workers(psycopg, test_dsn, role_passwords):
            _drive_tier_violation(psycopg, _as_role(test_dsn, row[1], host_secret))
        assert _count_events(admin_conn) > before
        with admin_conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM enrollment_ticket")
            assert cur.fetchone()[0] == 1, "a refused mint created a ticket"


def test_a_caller_with_no_principal_row_cannot_mint(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """`current_principal()` is `session_user`-derived, so a role nobody
    bootstrapped is nobody — and gets a reason rather than an unhandled NULL."""
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        with admin_conn.cursor() as cur:
            cur.execute(
                "UPDATE principal SET state = 'retired' WHERE principal_id = %s",
                (CONTROL_PLANE_PRINCIPAL,),
            )
        before = _count_events(admin_conn)
        with psycopg.connect(
            _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
        ) as app:
            assert _mint(app, _unique(), _secret()[1])[1] == "no_principal"
        assert _count_events(admin_conn) > before


# ---------------------------------------------------------------------------
# The two layers of the grant matrix
# ---------------------------------------------------------------------------


def test_the_enrolment_grants_are_merged_with_the_queue_grants_not_substituted(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Two tasks declaring rows for one role is the normal case as the schema
    grows, and under a dict update the later one silently wins.

    Both halves are checked in the DATABASE rather than in the mapping, because
    the mapping is what a substitution would also look correct in: a worker that
    can still claim work AND prove its identity is a worker whose two layers of
    grants both survived `render_table_grants()`'s opening `REVOKE ALL`.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        app_dsn = _dsn_for(test_dsn, roles.APP_ROLE, role_passwords)
        with psycopg.connect(app_dsn, autocommit=True) as app:
            row, host_secret = _enrol(app, _unique())
            with app.cursor() as cur:
                cur.execute(
                    "SELECT queue_enqueue('execution', 'merged', '{}'::jsonb, NULL, "
                    "NULL, 3, 0, 0, 0)"
                )

        with psycopg.connect(
            _as_role(test_dsn, row[1], host_secret), autocommit=True
        ) as host, host.cursor() as cur:
            # The 0002 layer.
            cur.execute(
                "SELECT ok, reason FROM queue_claim('execution', %s, 300)",
                (hashlib.sha256(b"claim-token").hexdigest(),),
            )
            assert cur.fetchone() == (True, None)
            # The 0003 layer, on the same role, in the same database.
            cur.execute("SELECT ok FROM identity_assert(%s)", (host_secret,))
            assert cur.fetchone() == (True,)


def test_the_rendered_matrix_keeps_both_layers_for_every_role() -> None:
    """The mapping-level half of the same rule, with no database needed.

    A `dict` update would leave `PRIVILEGES[WORKER_ROLE]` describing only
    whichever contribution was written last, and both tasks' own suites would
    still pass because each is correct in isolation.
    """
    worker = roles.PRIVILEGES[roles.WORKER_ROLE]
    # 0001/0002's contribution.
    assert worker["run"] == frozenset({"SELECT", "INSERT", "UPDATE"})
    assert worker["work_item"] == frozenset()
    # 0003's contribution, on the same mapping.
    assert worker["principal"] == frozenset()
    assert worker["enrollment_ticket"] == frozenset()

    app = roles.PRIVILEGES[roles.APP_ROLE]
    assert app["work_item"] == frozenset({"SELECT"})
    assert app["principal"] == frozenset({"SELECT"})
    assert app["proposal"] == frozenset({"SELECT", "INSERT", "UPDATE"})

    statements = roles.render_grants()
    for signature in (
        "queue_claim(text, text, integer)",
        "enroll_rotate_self(text, text, text)",
    ):
        assert any(
            signature in s and f"TO {roles.WORKER_ROLE};" in s for s in statements
        ), signature
    for signature in ("queue_enqueue", "enroll_mint_ticket"):
        assert any(
            signature in s and f"TO {roles.APP_ROLE};" in s for s in statements
        ), signature


def test_the_operator_role_holds_no_dml_on_any_domain_table() -> None:
    """It is an admission lever, not a second control plane.

    The blanket per-table default is deliberately not applied to it; folding it
    in would hand the most trusted role full DML on every table in the schema,
    which is the opposite of what a tier-0 role is for.
    """
    granted = roles.PRIVILEGES[roles.OPERATOR_ROLE]
    assert set(granted) <= {
        "principal",
        "principal_event",
        "principal_credential",
        "enrollment_ticket",
        "worker_host_fingerprint",
        # Explicit zero-grant declaration for the reserved finalization
        # fencing table.  Presence in the matrix is not access: the empty
        # privilege set proves the operator cannot read or mutate it.
        "run_finalization_claim",
    }
    for table, privileges in granted.items():
        assert privileges <= frozenset({"SELECT"}), table


# ---------------------------------------------------------------------------
# Every audited decision leaves a row
# ---------------------------------------------------------------------------

#: Every `(event_type, outcome, reason)` an operator can observe. A site added
#: to the migration without an entry here is a site nothing pins.
REQUIRED_AUDIT_SITES = (
    ("bootstrap", "granted", None),
    ("enroll_mint", "granted", None),
    ("enroll_mint", "rejected", "no_principal"),
    ("enroll_mint", "rejected", "tier_violation"),
    ("enroll_mint", "rejected", "principal_exists"),
    ("enroll_mint", "rejected", "readmission_requires_operator"),
    ("enroll_mint", "rejected", "unknown_principal"),
    ("enroll_worker", "granted", None),
    ("enroll", "granted", None),
    ("enroll", "rejected", "unknown_ticket"),
    ("enroll", "rejected", "ticket_redeemed"),
    ("enroll", "rejected", "ticket_revoked"),
    ("enroll", "rejected", "ticket_expired"),
    ("enroll", "rejected", "fingerprint_conflict"),
    ("enroll_revoke", "granted", "leaked"),
    ("issue", "granted", None),
    ("revoke", "granted", "rotated"),
    ("revoke", "granted", "incident"),
    ("rotate", "granted", None),
    ("assert", "rejected", "unknown_credential"),
    ("assert", "rejected", "credential_revoked"),
    ("assert", "rejected", "principal_inactive"),
    ("assert", "rejected", "addr_mismatch"),
    ("act_as", "rejected", "principal_role_mismatch"),
)

#: Branches no test reaches, recorded rather than quietly missing from the list
#: above. Each is defence in depth against a future change, and each is
#: unreachable for a stated reason rather than for want of effort.
UNREACHABLE_AUDIT_SITES = (
    # `enroll_redeem_ticket` re-reads the principal under the ticket's row lock,
    # and `enroll_mint_ticket` already refused the mismatching purpose, so
    # neither can be observed without first defeating the mint gate.
    ("enroll", "rejected", "principal_exists"),
    ("enroll", "rejected", "unknown_principal"),
    # Issuance is reached only from redemption and rotation, both of which have
    # just proved the principal active and are issuing to themselves or downward.
    ("issue", "rejected", "principal_inactive"),
    ("issue", "rejected", "tier_violation"),
    # A credential outliving its own expiry needs the sweeper not to have run;
    # the sweeper revokes it first, and `credential_revoked` is what is then
    # observed. Kept because a change to the sweep interval makes it reachable.
    ("assert", "rejected", "credential_expired"),
    # `identity_sweep_expired()` closes out an expired credential with this
    # reason; the enrolment suite drives revocation through the incident lever
    # and rotation instead.
    ("revoke", "granted", "expired"),
    # `principal_retired` is the reason a future retirement path would use.
    ("revoke", "granted", "principal_retired"),
)


def test_every_reachable_audit_site_writes_a_row(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Drive every audited decision and assert each one left its record.

    One walk rather than one assertion per site would cover whichever branches
    it happened to take and be silent about the rest — and the branches this
    protocol most needs recorded are the refusals, which a happy-path walk never
    reaches. The registry is then checked in BOTH directions: no required site
    missing, and no observed site unregistered.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        app_dsn = _dsn_for(test_dsn, roles.APP_ROLE, role_passwords)
        operator_dsn = _dsn_for(test_dsn, roles.OPERATOR_ROLE, role_passwords)
        principal = _unique()

        with psycopg.connect(app_dsn, autocommit=True) as app:
            # enroll_mint: granted; enroll_worker / issue / enroll: granted.
            row, host_secret = _enrol(app, principal)
            assert row[4] is None, row

            # enroll: rejected on an unknown ticket, and on a replay.
            assert _redeem(app, "no-such-ticket", "h", "v", DESCRIPTOR)[4] == "unknown_ticket"

            replay_ticket, replay_hash = _secret()
            assert _mint(app, _unique(), replay_hash)[1] is None
            spare_secret, spare_hash = _secret()
            spare = _redeem(
                app, replay_ticket, spare_hash,
                _scram_verifier(spare_secret),
                {**DESCRIPTOR, "machine_id": "spare"},
            )
            assert spare[4] is None, spare
            assert _redeem(
                app, replay_ticket, spare_hash, _scram_verifier(spare_secret), DESCRIPTOR
            )[4] == "ticket_redeemed"

            # enroll: rejected on a fingerprint already bound elsewhere.
            assert _enrol(app, _unique("worker-clone"), DESCRIPTOR)[0][4] == (
                "fingerprint_conflict"
            )

            # enroll_mint: rejected — the principal exists and this is not a
            # re-enrolment; and the principal does not exist and this is one.
            assert _mint(app, principal, _secret()[1])[1] == "principal_exists"
            assert _mint(app, _unique(), _secret()[1], purpose="re_enroll")[1] == (
                "unknown_principal"
            )

            # enroll: rejected on expiry.
            expiring_ticket, expiring_hash = _secret()
            minted = _mint(app, _unique(), expiring_hash)
            with admin_conn.cursor() as cur:
                cur.execute(
                    "UPDATE enrollment_ticket SET issued_at = now() - interval '1 hour', "
                    "expires_at = now() - interval '30 minutes' WHERE ticket_id = %s",
                    (minted[0],),
                )
            assert _redeem(
                app, expiring_ticket, _secret()[1], "v", DESCRIPTOR
            )[4] == "ticket_expired"

            # enroll: rejected on a revoked ticket, via enroll_revoke: granted.
            revoked_ticket, revoked_hash = _secret()
            revoked = _mint(app, _unique(), revoked_hash)
        with psycopg.connect(operator_dsn, autocommit=True) as operator, operator.cursor() as cur:
            cur.execute("SELECT enroll_revoke_ticket(%s, %s)", (revoked[0], "leaked"))
        with psycopg.connect(app_dsn, autocommit=True) as app:
            assert _redeem(
                app, revoked_ticket, _secret()[1], "v", DESCRIPTOR
            )[4] == "ticket_revoked"

        # assert: rejected on an unknown credential; rotate: granted, which also
        # produces revoke/rotated; then assert: rejected on the superseded one.
        with psycopg.connect(
            _as_role(test_dsn, row[1], host_secret), autocommit=True
        ) as host, host.cursor() as cur:
            cur.execute("SELECT ok, reason FROM identity_assert(%s)", ("nothing-matches",))
            assert cur.fetchone() == (False, "unknown_credential")

            rotated_secret, rotated_hash = _secret()
            cur.execute(
                "SELECT * FROM enroll_rotate_self(%s, %s, %s)",
                (host_secret, rotated_hash, _scram_verifier(rotated_secret)),
            )
            assert cur.fetchone()[1] is None
            cur.execute("SELECT ok, reason FROM identity_assert(%s)", (host_secret,))
            assert cur.fetchone() == (False, "credential_revoked")

        # act_as: one host presenting another's secret, from its own connection.
        with psycopg.connect(
            _as_role(test_dsn, spare[1], spare_secret), autocommit=True
        ) as other, other.cursor() as cur:
            cur.execute("SELECT ok, reason FROM identity_assert(%s)", (rotated_secret,))
            assert cur.fetchone() == (False, "principal_role_mismatch")

        # enroll_mint: rejected/tier_violation, from a real host's connection,
        # with the grant widened only for this block.
        with _mint_granted_to_workers(psycopg, test_dsn, role_passwords):
            _drive_tier_violation(psycopg, _as_role(test_dsn, row[1], rotated_secret))

        # revoke: granted/incident, then enroll_mint: rejected on readmission.
        with psycopg.connect(operator_dsn, autocommit=True) as operator, operator.cursor() as cur:
            cur.execute("SELECT identity_revoke_principal(%s, %s)", (principal, "incident"))
        with psycopg.connect(app_dsn, autocommit=True) as app:
            assert _mint(app, principal, _secret()[1], purpose="re_enroll")[1] == (
                "readmission_requires_operator"
            )

        # addr_mismatch and principal_inactive, on a host enrolled with a CIDR
        # it cannot be inside.
        with psycopg.connect(app_dsn, autocommit=True) as app:
            offsite, offsite_secret = _enrol(
                app,
                _unique("worker-offsite"),
                {**DESCRIPTOR, "machine_id": "offsite"},
                expected_cidr="10.99.0.0/24",
            )
        with psycopg.connect(
            _as_role(test_dsn, offsite[1], offsite_secret), autocommit=True
        ) as host, host.cursor() as cur:
            cur.execute("SELECT ok, reason FROM identity_assert(%s)", (offsite_secret,))
            assert cur.fetchone() == (False, "addr_mismatch")
        with psycopg.connect(operator_dsn, autocommit=True) as operator, operator.cursor() as cur:
            cur.execute(
                "SELECT identity_revoke_principal(%s, %s)", (offsite[0], "incident")
            )
        with admin_conn.cursor() as cur:
            # Un-revoke the credential row alone, leaving the principal
            # suspended, so the gate reaches `principal_inactive` — the branch
            # that only fires when a live credential meets a dead principal.
            cur.execute(
                "UPDATE principal_credential SET revoked_at = NULL, revoke_reason = NULL "
                "WHERE principal_id = %s",
                (offsite[0],),
            )
            cur.execute(
                "UPDATE principal SET expected_cidr = NULL WHERE principal_id = %s",
                (offsite[0],),
            )
            # `identity_revoke_principal` also took the role's login away, so
            # give it back: the branch under test is the one where a live
            # credential meets a dead principal, which needs a connection.
            cur.execute(
                "SELECT identity_set_role_secret(%s, %s, now() + interval '1 hour')",
                (offsite[1], _scram_verifier(offsite_secret)),
            )
        with psycopg.connect(
            _as_role(test_dsn, offsite[1], offsite_secret), autocommit=True
        ) as host, host.cursor() as cur:
            cur.execute("SELECT ok, reason FROM identity_assert(%s)", (offsite_secret,))
            assert cur.fetchone() == (False, "principal_inactive")

        # enroll_mint: rejected/no_principal. Last, because retiring the control
        # plane's own principal is what makes `current_principal()` NULL for it.
        with admin_conn.cursor() as cur:
            cur.execute(
                "UPDATE principal SET state = 'retired' WHERE principal_id = %s",
                (CONTROL_PLANE_PRINCIPAL,),
            )
        with psycopg.connect(app_dsn, autocommit=True) as app:
            assert _mint(app, _unique(), _secret()[1])[1] == "no_principal"

        with admin_conn.cursor() as cur:
            cur.execute("SELECT DISTINCT event_type, outcome, reason FROM principal_event")
            observed = {tuple(r) for r in cur.fetchall()}

    missing = [site for site in REQUIRED_AUDIT_SITES if site not in observed]
    assert missing == [], f"audited decisions that left no row: {missing}"

    for site in UNREACHABLE_AUDIT_SITES:
        assert site not in observed, (
            f"{site} is reachable after all; move it into the required set"
        )

    unregistered = observed - set(REQUIRED_AUDIT_SITES) - set(UNREACHABLE_AUDIT_SITES)
    assert unregistered == set(), (
        f"audit sites with no entry in REQUIRED_AUDIT_SITES: {unregistered}"
    )


MINT_SIGNATURE = "enroll_mint_ticket(text, text, text, inet, interval, text)"


@contextmanager
def _mint_granted_to_workers(psycopg, test_dsn, role_passwords):
    """Widen the mint grant to `aicc_worker` for the duration of one block.

    The shipping answer to "can a worker mint?" is `permission denied`. That
    makes the tier check INSIDE the function unreachable in production and
    therefore untested — which is how a second gate quietly stops being a gate.
    So exactly the tests that are about the tier gate widen the grant, observe
    the refusal, and put it back. Nothing outside those blocks sees it.
    """
    from psycopg import sql

    owner_dsn = _as_role(
        test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]
    )

    def apply(statement: str) -> None:
        with psycopg.connect(owner_dsn, autocommit=True) as owner, owner.cursor() as cur:
            cur.execute(
                sql.SQL(statement).format(
                    sql.SQL(MINT_SIGNATURE), sql.Identifier(roles.WORKER_ROLE)
                )
            )

    apply("GRANT EXECUTE ON FUNCTION {} TO {}")
    try:
        yield
    finally:
        apply("REVOKE EXECUTE ON FUNCTION {} FROM {}")


def _drive_tier_violation(psycopg, host_dsn) -> None:
    """Reach the mint tier gate from a real enrolled host's own connection."""
    with psycopg.connect(host_dsn, autocommit=True) as host, host.cursor() as cur:
        cur.execute(
            "SELECT * FROM enroll_mint_ticket(%s, %s, %s)",
            ("worker:conjured", "conjured.local", _secret()[1]),
        )
        assert cur.fetchone() == (None, "tier_violation")


# ---------------------------------------------------------------------------
# Reversibility
# ---------------------------------------------------------------------------

#: The enrolment migration, named by version rather than reached as "the head".
#: This test shipped while it was the last migration, so `upgrade()` with no
#: target and a literal `(3,)` were the same thing; the next migration to land
#: made them different and turned that into an off-by-one in a tuple.
ENROLMENT_VERSION = 3


def test_up_down_up_down_leaves_no_enrolment_object(
    admin_conn, psycopg, test_dsn, role_passwords
):
    """Tables, views, functions, the composite type — and the cluster roles.

    The roles are the half a schema-only reversibility test would miss, and they
    are the half that matters: a per-host LOGIN outliving the record of it is
    precisely the unaccounted credential this migration exists to remove.
    """
    with _cluster(admin_conn, psycopg, test_dsn, role_passwords):
        migrator_dsn = _as_role(
            test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]
        )
        for cycle in range(2):
            with psycopg.connect(migrator_dsn, autocommit=True) as conn:
                if cycle:
                    # `target`, not "upgrade to the head": this test owns the
                    # enrolment migration's reversibility and nothing else, and
                    # an unqualified `upgrade()` silently pulls in every later
                    # migration — which then shows up as an off-by-one in the
                    # downgrade tuple below rather than as anything readable.
                    assert migrations.upgrade(conn, target=ENROLMENT_VERSION) == (
                        ENROLMENT_VERSION,
                    )
                    roles.apply_table_grants(conn)
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT identity_bootstrap_principal(%s, 'control_plane', %s, %s)",
                            (CONTROL_PLANE_PRINCIPAL, roles.APP_ROLE, "Control plane"),
                        )

            with psycopg.connect(
                _dsn_for(test_dsn, roles.APP_ROLE, role_passwords), autocommit=True
            ) as app:
                row, _ = _enrol(app, _unique())
                assert row[4] is None, row
            with admin_conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_roles WHERE rolname LIKE 'aicc\\_w\\_%'"
                )
                # One per cycle, not cumulative: the previous cycle's downgrade
                # took its role with it, which is what the end of this test
                # asserts directly.
                assert cur.fetchone()[0] == 1

            with psycopg.connect(migrator_dsn, autocommit=True) as conn:
                # Derived from what this database actually has applied, not
                # from the migration set: the two cycles differ, because the
                # first starts from a fully migrated database and the second
                # re-applies only the enrolment migration. Written as a literal,
                # this assertion was correct only while enrolment was the last
                # migration; written against `discover()`, it would be correct
                # only on the first cycle. The ledger unwinds newest first.
                expected = tuple(
                    v
                    for v in sorted(migrations.applied_versions(conn), reverse=True)
                    if v >= ENROLMENT_VERSION
                )
                assert migrations.downgrade(conn, target=ENROLMENT_VERSION - 1) == expected

            with admin_conn.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('principal', 'principal_credential', 'principal_event', "
                    "'enrollment_ticket', 'worker_host_fingerprint')"
                )
                assert cur.fetchall() == [], "an enrolment table survived the downgrade"
                cur.execute(
                    "SELECT table_name FROM information_schema.views "
                    "WHERE table_schema = 'public' AND table_name IN "
                    "('principal_credential_public', 'enrollment_ticket_public')"
                )
                assert cur.fetchall() == [], "an enrolment view survived"
                cur.execute(
                    "SELECT p.proname FROM pg_proc p JOIN pg_namespace n "
                    "ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
                    "AND (p.proname LIKE 'enroll%' OR p.proname LIKE 'identity%' "
                    "OR p.proname = 'current_principal')"
                )
                assert cur.fetchall() == [], "an enrolment function survived"
                cur.execute("SELECT to_regtype('identity_verdict')")
                assert cur.fetchone()[0] is None, "the verdict type survived"
                # The cluster half.
                cur.execute(
                    "SELECT rolname FROM pg_roles WHERE rolname LIKE 'aicc\\_w\\_%'"
                )
                assert cur.fetchall() == [], "a per-host LOGIN role survived the downgrade"

            # The queue protocol below is untouched by all of this.
            with admin_conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM work_item")
                assert cur.fetchone()[0] >= 0
