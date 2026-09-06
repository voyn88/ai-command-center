#!/usr/bin/env python3
"""One CLI, deployed byte-identical to both real hosts, that drives the
queue-claim protocol (`command_center/db/sql/0002_queue_claim.up.sql`) over a
plain `psql` subprocess -- no `psycopg` install required on either side, so
the same file runs unmodified on a host that never got the Python driver.

This exists for the SRV-05 cross-host proof
(`docs/operations/SRV05_LINUX_SYSTEMD_VERIFICATION.md`, properties 12-13):
proving that a lease abandoned by a SIGKILLed worker on one host is picked up
by exactly one other real host, and that a worker cut off from the queue
cannot out-argue the queue's own visibility-timeout decision. Both claims are
about what two independent OS processes on two independent machines actually
observe, which is exactly what this script lets a driver orchestrate over SSH:
each action is one queue-protocol step, taken by whichever host invokes it,
against the one shared PostgreSQL database that arbitrates them.

Every SQL value travels through a psql `-v name=value` variable and `:'name'`
substitution rather than string-formatted into the query text, so a payload or
reason containing a quote cannot break out of its literal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
import sys
import time


def _run_sql(dsn: str, sql: str, variables: dict[str, str], timeout: float = 30) -> list[str]:
    argv = ["psql", dsn, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-t", "-A", "-F", "|"]
    for key, value in variables.items():
        argv += ["-v", f"{key}={value}"]
    argv += ["-c", sql]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"psql failed (rc={result.returncode}): {result.stderr.strip()}")
    line = result.stdout.strip("\n")
    return line.split("|") if line else []


def _emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


def cmd_claim(args: argparse.Namespace) -> int:
    token = secrets.token_hex(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    fields = _run_sql(
        args.dsn,
        "SELECT ok, reason, work_item_id, attempt_id, attempt_no, visible_until "
        "FROM queue_claim(:'queue', :'hash', :vis)",
        {"queue": args.queue, "hash": token_hash, "vis": str(args.visibility)},
    )
    ok, reason, work_item_id, attempt_id, attempt_no, visible_until = (
        fields + [""] * 6
    )[:6]
    payload = {
        "pid": os.getpid(),
        "host_label": args.host_label,
        "ok": ok == "t",
        "reason": reason or None,
        "work_item_id": work_item_id or None,
        "attempt_id": attempt_id or None,
        "attempt_no": int(attempt_no) if attempt_no else None,
        "visible_until": visible_until or None,
        "token": token if ok == "t" else None,
    }
    _emit(payload)
    if payload["ok"] and args.hold_seconds > 0:
        # Represents an in-flight worker still holding the attempt: the
        # process the driver will later SIGKILL, or that a partition will cut
        # off from the database mid-run.
        time.sleep(args.hold_seconds)
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    fields = _run_sql(
        args.dsn,
        "SELECT ok, reason FROM queue_heartbeat(:'attempt_id', :'token')",
        {"attempt_id": args.attempt_id, "token": args.token},
    )
    ok, reason = (fields + ["", ""])[:2]
    _emit({"host_label": args.host_label, "ok": ok == "t", "reason": reason or None})
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    fields = _run_sql(
        args.dsn,
        "SELECT ok, reason FROM queue_complete(:'attempt_id', :'token', :'result'::jsonb)",
        {"attempt_id": args.attempt_id, "token": args.token, "result": args.result},
    )
    ok, reason = (fields + ["", ""])[:2]
    _emit({"host_label": args.host_label, "ok": ok == "t", "reason": reason or None})
    return 0


def cmd_fail(args: argparse.Namespace) -> int:
    fields = _run_sql(
        args.dsn,
        "SELECT ok, reason FROM queue_fail(:'attempt_id', :'token', :'reason', :retryable)",
        {
            "attempt_id": args.attempt_id,
            "token": args.token,
            "reason": args.reason,
            "retryable": "true" if args.retryable else "false",
        },
    )
    ok, reason = (fields + ["", ""])[:2]
    _emit({"host_label": args.host_label, "ok": ok == "t", "reason": reason or None})
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    fields = _run_sql(args.dsn, "SELECT queue_reap()", {})
    reaped = int(fields[0]) if fields else 0
    _emit({"host_label": args.host_label, "reaped": reaped})
    return 0


def cmd_enqueue(args: argparse.Namespace) -> int:
    fields = _run_sql(
        args.dsn,
        "SELECT queue_enqueue(:'queue', :'key', :'payload'::jsonb, NULL, NULL, "
        ":max_attempts, 0, 0, 0)",
        {
            "queue": args.queue,
            "key": args.key,
            "payload": args.payload,
            "max_attempts": str(args.max_attempts),
        },
    )
    _emit({"host_label": args.host_label, "work_item_id": fields[0] if fields else None})
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    fields = _run_sql(
        args.dsn,
        "SELECT state, attempt_count, current_attempt_id, result_id, dead_reason "
        "FROM work_item WHERE work_item_id = :'item'",
        {"item": args.work_item_id},
    )
    state, attempt_count, current_attempt_id, result_id, dead_reason = (
        fields + [""] * 5
    )[:5]
    _emit(
        {
            "host_label": args.host_label,
            "state": state or None,
            "attempt_count": int(attempt_count) if attempt_count else None,
            "current_attempt_id": current_attempt_id or None,
            "result_id": result_id or None,
            "dead_reason": dead_reason or None,
        }
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True, help="libpq connection string/URI")
    parser.add_argument("--host-label", required=True, help="which real host ran this")
    sub = parser.add_subparsers(dest="action", required=True)

    p_claim = sub.add_parser("claim")
    p_claim.add_argument("--queue", required=True)
    p_claim.add_argument("--visibility", type=int, default=60)
    p_claim.add_argument("--hold-seconds", type=float, default=0)
    p_claim.set_defaults(func=cmd_claim)

    p_hb = sub.add_parser("heartbeat")
    p_hb.add_argument("--attempt-id", required=True)
    p_hb.add_argument("--token", required=True)
    p_hb.set_defaults(func=cmd_heartbeat)

    p_complete = sub.add_parser("complete")
    p_complete.add_argument("--attempt-id", required=True)
    p_complete.add_argument("--token", required=True)
    p_complete.add_argument("--result", default='{"ok": true}')
    p_complete.set_defaults(func=cmd_complete)

    p_fail = sub.add_parser("fail")
    p_fail.add_argument("--attempt-id", required=True)
    p_fail.add_argument("--token", required=True)
    p_fail.add_argument("--reason", default="probe_fail")
    p_fail.add_argument("--retryable", action="store_true")
    p_fail.set_defaults(func=cmd_fail)

    p_reap = sub.add_parser("reap")
    p_reap.set_defaults(func=cmd_reap)

    p_enqueue = sub.add_parser("enqueue")
    p_enqueue.add_argument("--queue", required=True)
    p_enqueue.add_argument("--key", required=True)
    p_enqueue.add_argument("--payload", default="{}")
    p_enqueue.add_argument("--max-attempts", type=int, default=3)
    p_enqueue.set_defaults(func=cmd_enqueue)

    p_inspect = sub.add_parser("inspect")
    p_inspect.add_argument("--work-item-id", required=True)
    p_inspect.set_defaults(func=cmd_inspect)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
