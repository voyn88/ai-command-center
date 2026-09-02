"""Standalone, reproducible check backing docs/srv04b-two-host-acceptance.md.

Not a pytest module and not collected by the suite. It exists because the
prior acceptance record (rejected: PR #459, ACCEPTANCE: REJECT
3d7ebe2010879cc960fabeb08fc819a51ccf7e74) made specific empirical claims --
192 attempts and exactly 8 winners, a 27.45s stale-owner expiry, NAT
collapsing client addresses -- with no command, config, or raw output a
reviewer could run to check them. This script is that command. It reuses the
same production code paths `tests/db/test_queue_claim.py` does (`command_center.db.roles`,
`command_center.db.migrations`, `render_worker_host_role`) rather than
re-implementing the protocol, and every number it prints is measured in the
run, not asserted.

Usage::

    AICC_TEST_PG_ADMIN_DSN="host=... port=... dbname=postgres user=..." \\
        python3 docs/evidence/srv04b-two-host-acceptance/acceptance_check.py

Requires a real PostgreSQL server reachable with that DSN's role able to
CREATEDB and CREATEROLE (the same requirement `tests/db/conftest.py` states).
Creates and drops its own database and roles; touches nothing else.

What this does NOT claim, and why: it runs both simulated "hosts" as two
independently-authenticated per-host LOGIN roles (`render_worker_host_role`,
the exact mechanism production uses to tell hosts apart) against one shared
PostgreSQL server -- but from one machine, over loopback. It does not use two
physical hosts, does not cross a real network link, and cannot show real
network jitter or a genuine dual-stack TCP blackhole. Every claim below is
scoped to what a single-host, two-role rig can actually establish: the
protocol's *role-identity* guarantees (exclusivity, the stale-owner fence,
claimant derivation, session_user vs current_user, clock independence). It
establishes those mechanically and reproducibly. It does not stand in for a
genuine multi-datacenter network-partition drill, and the record this backs
says so.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))

import psycopg  # noqa: E402
from psycopg import sql  # noqa: E402

from command_center.db import migrations, roles  # noqa: E402

ADMIN_DSN_ENV = "AICC_TEST_PG_ADMIN_DSN"
QUEUE = "acceptance"


def log(msg: str) -> None:
    print(msg, flush=True)


def as_role(dsn: str, role: str, password: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params.update(user=role, password=password)
    return make_conninfo(**params)


def token() -> tuple[str, str]:
    t = secrets.token_hex(32)
    return t, hashlib.sha256(t.encode("utf-8")).hexdigest()


def enqueue(conn, key: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT queue_enqueue(%s, %s, %s::jsonb, NULL, %s, %s, %s, %s, %s)",
            (QUEUE, key, json.dumps({"job": key}), None, 3, 0, 0, 0),
        )
        return cur.fetchone()[0]


def claim(conn, token_hash: str, *, visibility: int = 300):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM queue_claim(%s, %s, %s)", (QUEUE, token_hash, visibility))
        return cur.fetchone()  # (ok, reason, item, attempt, attempt_no, until, payload)


def call(conn, sql_text: str, params):
    with conn.cursor() as cur:
        cur.execute(sql_text, params)
        return cur.fetchone()


def main() -> int:
    admin_dsn_base = os.environ.get(ADMIN_DSN_ENV)
    if not admin_dsn_base:
        log(f"SKIP: {ADMIN_DSN_ENV} is not set")
        return 0

    git_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()
    sql_path = REPO_ROOT / "command_center/db/sql/0002_queue_claim.up.sql"
    sql_sha256 = hashlib.sha256(sql_path.read_bytes()).hexdigest()

    log("=== SRV-04b two-host acceptance check ===")
    log(f"repo HEAD: {git_sha}")
    log(f"0002_queue_claim.up.sql sha256: {sql_sha256}")
    log(f"AICC_TEST_PG_ADMIN_DSN (redacted user/host shape only): {admin_dsn_base.split()[0:2]}")

    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    admin_params = conninfo_to_dict(admin_dsn_base)
    with psycopg.connect(admin_dsn_base, autocommit=True) as bootstrap_admin:
        with bootstrap_admin.cursor() as cur:
            cur.execute("SELECT version()")
            log(f"server version: {cur.fetchone()[0]}")

    dbname = f"aicc_srv04b_{secrets.token_hex(4)}"
    with psycopg.connect(admin_dsn_base, autocommit=True) as bootstrap_admin:
        with bootstrap_admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(dbname)))
    test_dsn = make_conninfo(**{**admin_params, "dbname": dbname})
    log(f"scratch database: {dbname}")

    seed = secrets.token_urlsafe(16)
    role_passwords = {r: hashlib.sha256(f"{seed}:{r}".encode()).hexdigest() for r in roles.ALL_ROLES}

    try:
        with psycopg.connect(test_dsn, autocommit=True) as admin_conn:
            roles.apply_bootstrap(admin_conn)
            # apply_bootstrap() creates every product role NOLOGIN by design
            # (command_center/db/roles.py:render_role_creation) -- passwords
            # and LOGIN are an operator/test-fixture concern, not something
            # committed. tests/db/conftest.py's role_passwords fixture does
            # this same ALTER before connecting as any product role; this
            # script needs the identical step for the two roles it
            # authenticates as directly (MIGRATOR_ROLE to migrate, APP_ROLE
            # to enqueue).
            with admin_conn.cursor() as cur:
                for role in (roles.MIGRATOR_ROLE, roles.APP_ROLE):
                    cur.execute(
                        sql.SQL("ALTER ROLE {} LOGIN PASSWORD {}").format(
                            sql.Identifier(role), sql.Literal(role_passwords[role])
                        )
                    )
            with psycopg.connect(
                as_role(test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]),
                autocommit=True,
            ) as mconn:
                migrations.upgrade(mconn)
                roles.apply_table_grants(mconn)

            app_dsn = as_role(test_dsn, roles.APP_ROLE, role_passwords[roles.APP_ROLE])

            # --- two per-host worker roles, the production mechanism -----------
            host_names = [f"aicc_wh_srv04b_{secrets.token_hex(3)}_{i}" for i in range(2)]
            host_password = secrets.token_urlsafe(24)
            with admin_conn.cursor() as cur:
                for name in host_names:
                    for stmt in roles.render_worker_host_role(name):
                        cur.execute(stmt)
                    cur.execute(
                        sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                            sql.Identifier(name), sql.Literal(host_password)
                        )
                    )
            host_dsns = [as_role(test_dsn, n, host_password) for n in host_names]
            log(f"host A role: {host_names[0]}")
            log(f"host B role: {host_names[1]}")

            # =====================================================================
            log("\n--- 1. Exclusivity: 8 rounds x 24 concurrent claimers, 2 hosts ---")
            total_attempts = 0
            total_winners = 0
            round_results = []
            for r in range(8):
                with psycopg.connect(app_dsn, autocommit=True) as app:
                    item_id = enqueue(app, f"round-{r}")
                verdicts: list[object] = [None] * 24
                barrier = threading.Barrier(24)

                def do_claim(i: int) -> None:
                    try:
                        with psycopg.connect(host_dsns[i % 2], autocommit=True) as conn:
                            barrier.wait(timeout=30)
                            verdicts[i] = claim(conn, token()[1])
                    except Exception as exc:  # noqa: BLE001
                        verdicts[i] = exc

                threads = [threading.Thread(target=do_claim, args=(i,)) for i in range(24)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=60)

                errors = [v for v in verdicts if isinstance(v, Exception)]
                if errors:
                    raise SystemExit(f"round {r}: unexpected errors: {errors}")
                winners = [v for v in verdicts if v[0]]
                total_attempts += 24
                total_winners += len(winners)
                round_results.append(len(winners))
                log(f"  round {r}: item={item_id} attempts=24 winners={len(winners)}")

            log(
                f"TOTAL: attempts={total_attempts} winners={total_winners} "
                f"per_round={round_results}"
            )
            assert total_attempts == 192
            assert total_winners == 8

            # =====================================================================
            log("\n--- 2. Stale-owner fence: measured wall-clock time to attempt_expired ---")
            with psycopg.connect(app_dsn, autocommit=True) as app:
                item_id = enqueue(app, "stale-timed")
            token_a, hash_a = token()
            visibility_seconds = 2
            with psycopg.connect(host_dsns[0], autocommit=True) as owner:
                start = time.monotonic()
                verdict = claim(owner, hash_a, visibility=visibility_seconds)
                assert verdict[0], verdict
                owner_pid = owner.info.backend_pid
                with admin_conn.cursor() as cur:
                    cur.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE pid = %s", (owner_pid,)
                    )
                    assert cur.fetchone()[0] == 1, "owner backend must be alive: not process death"

                # Poll the attempt's own state (never touching queue_complete,
                # which would itself terminate the attempt the moment it is
                # still valid) until the visibility deadline actually elapses
                # server-side and the reaper has expired it -- the real
                # elapsed time, not a fixed sleep guess.
                state = "active"
                while state == "active":
                    with psycopg.connect(app_dsn, autocommit=True) as app:
                        call(app, "SELECT queue_reap()", ())
                    with admin_conn.cursor() as cur:
                        cur.execute(
                            "SELECT state FROM work_attempt WHERE attempt_id = %s",
                            (verdict[3],),
                        )
                        state = cur.fetchone()[0]
                    if state == "active":
                        time.sleep(0.05)
                elapsed = time.monotonic() - start
                log(
                    f"  visibility_seconds={visibility_seconds} "
                    f"measured_elapsed_to_reaped_state({state!r})={elapsed:.3f}s "
                    f"owner backend alive throughout: yes"
                )
                # Now the owner -- still alive, still holding its open session --
                # wakes and tries to write. This is the actual refusal the
                # acceptance record is about.
                result = call(
                    owner,
                    "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                    (verdict[3], token_a, json.dumps({"done": True})),
                )
                log(f"  stale owner's queue_complete: ok={result[0]} reason={result[1]}")
                assert result == (False, "attempt_expired")

            token_b, hash_b = token()
            with psycopg.connect(host_dsns[1], autocommit=True) as worker_b:
                taken = claim(worker_b, hash_b)
                assert taken[0] and taken[2] == item_id
                log(f"  item re-delivered to host B: attempt_no={taken[4]} (expected 2)")

            # =====================================================================
            log("\n--- 3. session_user vs current_user: SET ROLE laundering ---")
            with psycopg.connect(app_dsn, autocommit=True) as app:
                item_id = enqueue(app, "launder")
            token_a, hash_a = token()
            with psycopg.connect(host_dsns[0], autocommit=True) as owner:
                verdict = claim(owner, hash_a)
                assert verdict[0], verdict

            with admin_conn.cursor() as cur:
                cur.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(host_names[0]), sql.Identifier(host_names[1])
                    )
                )
            try:
                with psycopg.connect(host_dsns[1]) as attacker:
                    with attacker.cursor() as cur:
                        cur.execute("SELECT session_user, current_user")
                        before = cur.fetchone()
                        cur.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(host_names[0])))
                        cur.execute("SELECT session_user, current_user")
                        after = cur.fetchone()
                    log(
                        f"  before SET ROLE: session_user={before[0]} current_user={before[1]}"
                    )
                    log(
                        f"  after  SET ROLE: session_user={after[0]} current_user={after[1]} "
                        f"(current_user now equals the victim's role; session_user does not)"
                    )
                    result = call(
                        attacker,
                        "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                        (verdict[3], token_a, json.dumps({"laundered": True})),
                    )
                    attacker.commit()
                    log(f"  queue_complete as launderer: ok={result[0]} reason={result[1]}")
                    assert result == (False, "not_claimant")
            finally:
                with admin_conn.cursor() as cur:
                    cur.execute(
                        sql.SQL("REVOKE {} FROM {}").format(
                            sql.Identifier(host_names[0]), sql.Identifier(host_names[1])
                        )
                    )

            # =====================================================================
            log("\n--- 4. Cross-host token theft (no SET ROLE, just a stolen token) ---")
            with psycopg.connect(app_dsn, autocommit=True) as app:
                item_id = enqueue(app, "theft")
            token_a, hash_a = token()
            with psycopg.connect(host_dsns[0], autocommit=True) as worker_a:
                verdict = claim(worker_a, hash_a)
                attempt_id = verdict[3]
            with psycopg.connect(host_dsns[1], autocommit=True) as worker_b:
                result = call(
                    worker_b,
                    "SELECT ok, reason FROM queue_complete(%s, %s, %s::jsonb)",
                    (attempt_id, token_a, json.dumps({"stolen": True})),
                )
                log(f"  host B replays host A's token: ok={result[0]} reason={result[1]}")
                assert result == (False, "not_claimant")

            # =====================================================================
            log("\n--- 5. Claimant forgery is refused even for the table owner ---")
            with psycopg.connect(app_dsn, autocommit=True) as app:
                item_id = enqueue(app, "forge")
            migrator_dsn = as_role(
                test_dsn, roles.MIGRATOR_ROLE, role_passwords[roles.MIGRATOR_ROLE]
            )
            with psycopg.connect(migrator_dsn) as conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "INSERT INTO work_attempt (attempt_id, work_item_id, attempt_no, "
                            "claimed_by_role, claim_token_hash, visibility_seconds, "
                            "visible_until, state, created_at, updated_at) VALUES "
                            "('wat_forged_srv04b', %s, 1, %s, %s, 60, "
                            "now() + interval '1 min', 'active', now(), now())",
                            (item_id, host_names[0], "0" * 64),
                        )
                    conn.commit()
                    log("  UNEXPECTED: forged insert succeeded")
                    return 1
                except psycopg.errors.InvalidAuthorizationSpecification as exc:
                    conn.rollback()
                    log(f"  forged claimant refused: {exc.diag.message_primary}")

            # =====================================================================
            log("\n--- 6. Zero timestamp parameters across the protocol's functions ---")
            with admin_conn.cursor() as cur:
                cur.execute(
                    "SELECT p.proname, pg_get_function_arguments(p.oid) "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'public' AND p.proname LIKE 'queue_%' "
                    "OR (n.nspname = 'public' AND p.proname LIKE '_queue_%') "
                    "ORDER BY p.proname"
                )
                rows = cur.fetchall()
            timestamp_params = []
            for name, args in rows:
                log(f"  {name}({args})")
                if "timestamp" in args.lower():
                    timestamp_params.append((name, args))
            log(f"  functions inspected: {len(rows)}")
            log(f"  functions with a timestamp-typed parameter: {len(timestamp_params)}")
            assert not timestamp_params, timestamp_params

            # =====================================================================
            log("\n--- 7. Client session timezone does not affect the claim window ---")
            with psycopg.connect(app_dsn, autocommit=True) as app:
                enqueue(app, "tz-plus14")
                enqueue(app, "tz-minus12")
            with psycopg.connect(host_dsns[0]) as conn_plus:
                with conn_plus.cursor() as cur:
                    cur.execute("SET TIME ZONE 'Etc/GMT-14'")  # UTC+14
                verdict_plus = claim(conn_plus, token()[1], visibility=100)
                conn_plus.commit()
            with psycopg.connect(host_dsns[1]) as conn_minus:
                with conn_minus.cursor() as cur:
                    cur.execute("SET TIME ZONE 'Etc/GMT+12'")  # UTC-12
                verdict_minus = claim(conn_minus, token()[1], visibility=100)
                conn_minus.commit()
            until_plus = verdict_plus[5]
            until_minus = verdict_minus[5]
            delta = abs((until_plus - until_minus).total_seconds())
            log(f"  UTC+14 session visible_until (as UTC): {until_plus}")
            log(f"  UTC-12 session visible_until (as UTC): {until_minus}")
            log(f"  |delta| = {delta:.3f}s over a 100s window (both from server now())")
            assert delta < 1.0

            # =====================================================================
            log("\n--- 8. Address-based attribution collapses; role attribution doesn't ---")
            with psycopg.connect(host_dsns[0], autocommit=True) as conn_a, psycopg.connect(
                host_dsns[1], autocommit=True
            ) as conn_b:
                addr_a = call(conn_a, "SELECT inet_client_addr(), session_user", ())
                addr_b = call(conn_b, "SELECT inet_client_addr(), session_user", ())
            log(f"  host A: inet_client_addr={addr_a[0]} session_user={addr_a[1]}")
            log(f"  host B: inet_client_addr={addr_b[0]} session_user={addr_b[1]}")
            log(
                f"  addresses equal: {addr_a[0] == addr_b[0]}  "
                f"(true by construction on one machine -- a stronger case than NAT collapse, "
                f"not a substitute for it) | session_user distinguishes them: "
                f"{addr_a[1] != addr_b[1]}"
            )

            with admin_conn.cursor() as cur:
                for name in host_names:
                    try:
                        cur.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(name)))
                    except Exception:  # noqa: BLE001
                        admin_conn.rollback()

        log("\n=== ALL CHECKS PASSED ===")
        return 0
    finally:
        with psycopg.connect(admin_dsn_base, autocommit=True) as bootstrap_admin:
            with bootstrap_admin.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(dbname)
                    )
                )


if __name__ == "__main__":
    sys.exit(main())
