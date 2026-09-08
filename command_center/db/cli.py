"""Operator entry point for the server database: `python -m command_center.db`.

Deliberately small and explicit. Migrations are not applied as a side effect of
the application starting, because that would make every replica in a rolling
deploy a potential migrator and would run schema changes under the application
credential. `bootstrap` runs once against a new database as a superuser; `upgrade` runs on
every deploy as the migrator. The application credential does neither.

    AICC_PG_USER=postgres       ... python -m command_center.db bootstrap
    AICC_PG_USER=aicc_migrator  ... python -m command_center.db upgrade
    AICC_PG_USER=aicc_app       ... python -m command_center.db status
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import platform
import secrets
import socket
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from command_center.db import migrations, pool, roles
from command_center.db.config import ConfigError, load_config


def _review_enqueue(store: Any, *, priority: int = 100) -> Any:
    """Build the ``enqueue(queue, key, payload, task_id, max_attempts)``
    writer that ``review_once``/``publish_review_verdicts`` call.

    A review run unblocks a merge in minutes; an implementation run can hold
    a worker slot for up to 900s. The claim protocol already orders ready
    work ``priority DESC`` (0002_queue_claim) -- review-class items must
    carry a priority above the 0 that dispatch enqueues at, or they queue
    FIFO behind runs that are already in flight.
    """

    def _enqueue(
        queue: str,
        idempotency_key: str,
        payload: dict[str, Any],
        task_id: str | None,
        max_attempts: int,
    ) -> str:
        return store.enqueue(
            queue,
            idempotency_key=idempotency_key,
            payload=payload,
            task_id=task_id,
            max_attempts=max_attempts,
            priority=priority,
        )

    return _enqueue


def _read_machine_id() -> str:
    """Best-effort local machine identifier for an enrolment descriptor."""
    try:
        return Path("/etc/machine-id").read_text(encoding="utf-8").strip()
    except OSError:
        return platform.node()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m command_center.db")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show the applied schema version.")
    sub.add_parser(
        "bootstrap",
        help="Create roles and set schema privileges (run once, as a superuser).",
    )
    sub.add_parser(
        "upgrade",
        help="Apply pending migrations and re-assert table grants (as the migrator).",
    )

    # The queue's recovery surface (SRV-06). These run as `aicc_app` — the
    # role the SQL protocol granted queue_reap/queue_redrive/work_dlq to —
    # which is why they live in the db CLI beside `status`, not in the worker.
    sub.add_parser(
        "queue-reap",
        help="Expire lapsed leases: requeue items with attempt budget left, "
        "dead-letter the exhausted (idempotent; run by aicc-queue-reaper.timer).",
    )
    dlq = sub.add_parser("queue-dlq", help="List dead-lettered work items.")
    dlq.add_argument("--queue", default=None, help="Restrict to one queue name.")
    dlq.add_argument("--limit", type=int, default=50, help="Rows to show (default 50).")
    redrive = sub.add_parser(
        "queue-redrive",
        help="Return one dead-lettered item to 'ready' with a raised attempt budget.",
    )
    redrive.add_argument("work_item_id", help="The wki_* id from queue-dlq.")
    redrive.add_argument(
        "--extra-attempts",
        type=int,
        default=1,
        help="Additional attempts to grant beyond those already burned (default 1).",
    )

    # The structured backlog store (VOYN-W0-BACKLOG-ORCHESTRATOR BO-S1).
    imp = sub.add_parser(
        "backlog-import",
        help="Reconcile the Markdown backlog projection into the structured "
        "store (idempotent; unparsed lines are reported, never dropped).",
    )
    imp.add_argument("path", help="Path to VOYN_TASKS_BACKLOG.md")
    imp.add_argument(
        "--parse-only",
        action="store_true",
        help="Parse and report without touching the database.",
    )
    sub.add_parser("backlog-status", help="Task counts by status from the store.")
    plan = sub.add_parser(
        "backlog-plan",
        help="One planner tick (BO-S2): release finished lanes, dispatch "
        "eligible tasks to the execution queue (run by aicc-backlog-planner.timer).",
    )
    plan.add_argument("--wip-limit", type=int, default=4)
    plan.add_argument(
        "--dry-run",
        action="store_true",
        help="Report the eligible set without dispatching.",
    )
    review = sub.add_parser(
        "backlog-review",
        help="One review tick (BO-S3b): enqueue an adversarial review run for "
        "each READY_TO_REVIEW task carrying a PR, then publish the ACCEPT "
        "marker for any task whose review already returned a verdict "
        "(aicc-backlog-review.timer). Needs --repo-path.",
    )
    review.add_argument("--repo-path", default=".", help="Local clone for gh calls.")
    review.add_argument(
        "--task-id",
        default=None,
        help="Review and publish a verdict only for this exact backlog task id.",
    )
    sub.add_parser(
        "backlog-merge",
        help="One merge tick (BO-S3b): merge every reviewed PR whose ACCEPT "
        "marker and checks are green, closing the task DONE "
        "(aicc-backlog-merge.timer). Needs --repo-path.",
    ).add_argument("--repo-path", default=".", help="Local clone for gh calls.")
    sub.add_parser(
        "backlog-merge-reconcile",
        help="Report-only audit (VOYN-W0-AICC-MERGE-DONE-BEFORE-TARGET-"
        "VERIFY): flag existing DONE tasks whose 'sha' evidence is not an "
        "ancestor of the default branch (pre-fix rows recorded the PR head, "
        "not the merge commit). Never changes a task's status.",
    ).add_argument("--repo-path", default=".", help="Local clone for gh calls.")

    self_deploy = sub.add_parser(
        "self-deploy",
        help="One self-deploy tick (VOYN-W0-AICC-DEPLOY-AUTOMATION): fast-"
        "forward this host's checkout to the remote default branch, run "
        "migrations when asked, restart the named services, smoke, and roll "
        "back on failure (voyn-aicc-self-deploy.timer). Fail-closed: refuses "
        "diverged/dirty checkouts and dependency-manifest changes.",
    )
    self_deploy.add_argument(
        "--repo-path", default=".", help="This host's runtime checkout."
    )
    self_deploy.add_argument(
        "--restart",
        action="append",
        default=[],
        metavar="SERVICE",
        help="systemd service to restart after the checkout moves "
        "(repeatable; worker hosts list their daemons, control hosts whose "
        "ticks are oneshot need none).",
    )
    self_deploy.add_argument(
        "--migrate",
        action="store_true",
        help="Run `command_center.db upgrade` after moving the checkout "
        "(the database-owning control host only).",
    )
    self_deploy.add_argument(
        "--branch",
        default="main",
        help="Remote branch to deploy from (the repository's default branch).",
    )

    down = sub.add_parser("downgrade", help="Revert migrations down to a version.")
    down.add_argument(
        "--to",
        type=int,
        required=True,
        help="Target version to stop at (0 reverts everything).",
    )
    # A downgrade drops tables. Requiring the flag keeps a mistyped command from
    # destroying a production schema.
    down.add_argument(
        "--yes-i-understand-this-drops-data",
        action="store_true",
        dest="confirmed",
        help="Required acknowledgement that a downgrade is destructive.",
    )

    # Zero-touch onboarding (VOYN-MIN-UNBOXING): a printed code stands in for
    # manual per-host configuration. `enroll-mint` is the operator/control-
    # plane act that prints the code; `enroll-redeem` is the (also operator/
    # control-plane) act that turns a presented code into the new host's
    # database credential -- never the enrolling host itself, which by
    # protocol design (0003_worker_enrollment) has no credential to call with.
    mint = sub.add_parser(
        "enroll-mint",
        help="Mint a one-time enrolment code for a new or re-enrolling host "
        "and print it exactly once.",
    )
    mint.add_argument("principal_id", help="Intended principal id, e.g. worker:srv-a")
    mint.add_argument("host", help="Intended hostname/address for the principal")
    mint.add_argument("--cidr", default=None, help="Expected source CIDR, if any")
    mint.add_argument(
        "--ttl",
        default=None,
        help="Requested ticket lifetime, e.g. '5 minutes' (the server clamps "
        "to a 10-minute default and a 15-minute ceiling regardless).",
    )
    mint.add_argument(
        "--purpose",
        default="enroll",
        choices=("enroll", "re_enroll"),
        help="'re_enroll' readmits a suspended/retired principal "
        "(requires the operator role; the control plane cannot).",
    )

    redeem = sub.add_parser(
        "enroll-redeem",
        help="Redeem a printed enrolment code and produce the device's "
        "PostgreSQL credential. Run this as the operator/control plane, "
        "never on the enrolling host.",
    )
    redeem.add_argument(
        "ticket",
        nargs="?",
        default=None,
        help="The printed one-time code; omitted means read one line from stdin.",
    )
    redeem.add_argument("--machine-id", default=None, help="Defaults to /etc/machine-id")
    redeem.add_argument("--os", default=None, help="Defaults to the local platform")
    redeem.add_argument("--arch", default=None, help="Defaults to the local platform")
    redeem.add_argument("--hostname", default=None, help="Defaults to the local hostname")
    redeem.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the resulting EnvironmentFile here, mode 0600 "
        "(e.g. /etc/aicc/worker.env); default prints it to stdout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    args = build_parser().parse_args(argv)

    if args.command == "self-deploy":
        # Deliberately BEFORE any database configuration or pool: a deploy
        # must work when the database is down or this host has no DB role
        # at all -- restoring a broken host is exactly when it runs
        # (VOYN-W0-AICC-DEPLOY-AUTOMATION). The --migrate subprocess opens
        # its own pool from the environment on the host that has one.
        from command_center.deployment.self_deploy import (
            SelfDeployConfig,
            self_deploy_once,
        )

        deploy_report = self_deploy_once(
            args.repo_path,
            SelfDeployConfig(
                branch=args.branch,
                services=tuple(args.restart),
                migrate=args.migrate,
            ),
        )
        print(f"{deploy_report.outcome.upper():10} {deploy_report.detail}")
        for step in deploy_report.steps:
            print(f"STEP      {step}")
        # A refusal or rollback exits non-zero so systemd surfaces the
        # failed tick to the operator; noop/deployed is success.
        return 0 if deploy_report.outcome in ("noop", "deployed") else 1

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    pool.open_pool(config)
    try:
        with pool.connection() as conn:
            if args.command == "status":
                print(f"target:  {config.redacted()}")
                print(f"applied: {list(migrations.applied_versions(conn))}")
                print(f"version: {migrations.current_version(conn)}")
                return 0

            if args.command == "bootstrap":
                count = roles.apply_bootstrap(conn)
                print(f"applied {count} role/schema statements")
                return 0

            if args.command == "upgrade":
                applied = migrations.upgrade(conn)
                print(f"applied: {list(applied)}" if applied else "already up to date")
                # Unconditionally, not only when something was applied: a table
                # created by a migration starts with no grants, and re-asserting
                # the matrix here is what keeps "migrated" and "reachable by the
                # app" the same state.
                count = roles.apply_table_grants(conn)
                print(f"re-asserted {count} table grants")
                return 0

            if args.command == "queue-reap":
                from command_center.db.work_queue_admin import WorkQueueAdmin

                reaped = WorkQueueAdmin(lambda: nullcontext(conn)).reap()
                print(f"reaped {reaped} lapsed attempt(s)")
                return 0

            if args.command == "queue-dlq":
                from command_center.db.work_queue_admin import WorkQueueAdmin

                letters = WorkQueueAdmin(lambda: nullcontext(conn)).dead_letters(
                    args.queue, limit=args.limit
                )
                if not letters:
                    print("dead-letter queue is empty")
                    return 0
                for letter in letters:
                    print(
                        f"{letter.work_item_id}  queue={letter.queue}  "
                        f"attempts={letter.attempt_count}/{letter.max_attempts}  "
                        f"dead_at={letter.dead_at}\n"
                        f"    dead_reason: {letter.dead_reason}\n"
                        f"    last_attempt: {letter.last_attempt_reason or '(none recorded)'}"
                    )
                return 0

            if args.command == "queue-redrive":
                from command_center.db.work_queue_admin import WorkQueueAdmin

                accepted = WorkQueueAdmin(lambda: nullcontext(conn)).redrive(
                    args.work_item_id, extra_attempts=args.extra_attempts
                )
                if accepted:
                    print(f"redriven: {args.work_item_id}")
                    return 0
                # The refusal is already audited server-side with its cause.
                print(
                    f"refused: {args.work_item_id} is unknown or not dead-lettered",
                    file=sys.stderr,
                )
                return 1

            if args.command == "backlog-import":
                from pathlib import Path

                from command_center.db.backlog_parser import parse_backlog
                from command_center.db.backlog_store import BacklogStore

                text = Path(args.path).read_text(encoding="utf-8")
                if args.parse_only:
                    parsed = parse_backlog(text)
                    print(f"parsed: {len(parsed.tasks)} tasks")
                    for line_no, reason, excerpt in parsed.unparsed:
                        print(f"UNPARSED line {line_no}: {reason} :: {excerpt}")
                    print(f"unparsed: {len(parsed.unparsed)} lines")
                    return 0
                report = BacklogStore(lambda: nullcontext(conn)).import_markdown(text)
                print(
                    f"inserted {report.inserted}, updated {report.updated}, "
                    f"unchanged {report.unchanged}"
                )
                for task_id, reason in report.refused:
                    print(f"REFUSED {task_id}: {reason}")
                for line_no, reason, excerpt in report.unparsed:
                    print(f"UNPARSED line {line_no}: {reason} :: {excerpt}")
                # Refused records are a defect of the file or the vocabulary;
                # surface them in the exit code so a timer/CI run goes red.
                return 1 if report.refused else 0

            if args.command == "backlog-status":
                from command_center.db.backlog_store import BacklogStore

                for status, count in sorted(
                    BacklogStore(lambda: nullcontext(conn)).counts_by_status().items()
                ):
                    print(f"{status}: {count}")
                return 0

            if args.command == "backlog-plan":
                from contextlib import nullcontext as _nc

                from command_center.orchestrator.planner import PlanLimits, plan_once

                if args.dry_run:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT task_id, wave, priority, dispatchable "
                            "FROM backlog_eligible"
                        )
                        for task_id, wave, priority, dispatchable in cur.fetchall():
                            print(
                                f"{task_id}  wave={wave} priority={priority or '-'} "
                                f"{'dispatchable' if dispatchable else 'NO REPO'}"
                            )
                    return 0
                report = plan_once(
                    lambda: _nc(conn), PlanLimits(wip_limit=args.wip_limit)
                )
                if report.planner_busy:
                    print("planner lease held elsewhere; nothing done")
                    return 0
                for task_id, work_item in report.dispatched:
                    print(f"DISPATCHED {task_id} -> {work_item}")
                for task_id, action in report.ingested:
                    print(f"INGESTED  {task_id}: {action}")
                for task_id, park_reason in report.resumed:
                    print(f"RESUMED   {task_id}: {park_reason}")
                for task_id, reason in report.skipped_by_wave_gate:
                    print(f"WAVE-GATE {task_id}: {reason}")
                for task_id, reason in report.refused:
                    print(f"REFUSED   {task_id}: {reason}")
                for task_id, reason in report.undispatchable:
                    print(f"NO-REPO   {task_id}: {reason}")
                return 0

            if args.command == "backlog-review":
                from contextlib import nullcontext as _nc

                from command_center.db.work_queue_store import WorkQueueStore
                from command_center.orchestrator.review_merge import (
                    publish_review_verdicts,
                    reconcile_pr_evidence,
                    review_once,
                )

                store = WorkQueueStore(lambda: _nc(conn))
                enqueue = _review_enqueue(store)
                # Before selecting anything: a task whose PR exists but was
                # never recorded is invisible to every gate downstream. This
                # derives that evidence from the task's own branch, so a pull
                # request opened outside `publish_run` still reaches review.
                evidence = reconcile_pr_evidence(
                    lambda: _nc(conn), args.repo_path, task_id=args.task_id
                )
                for evidence_task_id, pr in evidence.recorded:
                    print(f"PR-FOUND  {evidence_task_id} -> {pr}")
                for evidence_task_id, reason in evidence.skipped:
                    print(f"PR-SKIP   {evidence_task_id}: {reason}")
                report = review_once(
                    lambda: _nc(conn),
                    enqueue,
                    args.repo_path,
                    task_id=args.task_id,
                )
                for task_id, pr in report.reviewed:
                    print(f"REVIEW    {task_id} -> {pr}")
                for task_id, reason in report.skipped:
                    print(f"SKIP      {task_id}: {reason}")
                marker_report = publish_review_verdicts(
                    lambda: _nc(conn), args.repo_path, task_id=args.task_id,
                    # The same queue writer review_once uses: a REJECT
                    # enqueues one finding-verification run before it may
                    # remediate (VOYN-W0-AICC-REVIEW-AUTO-ACCEPT).
                    enqueue=enqueue,
                )
                for task_id, pr in marker_report.reviewed:
                    print(f"MARKER    {task_id} -> {pr}")
                for task_id, new_task_id in marker_report.remediated:
                    print(f"REMEDIATE {task_id} -> {new_task_id}")
                for task_id, reason in marker_report.skipped:
                    print(f"SKIP      {task_id}: {reason}")
                return 0

            if args.command == "backlog-merge":
                from contextlib import nullcontext as _nc

                from command_center.orchestrator.review_merge import merge_once

                report = merge_once(lambda: _nc(conn), args.repo_path)
                for task_id, head in report.merged:
                    print(f"MERGED    {task_id} -> {head}")
                for task_id, reason in report.skipped:
                    print(f"SKIP      {task_id}: {reason}")
                return 0

            if args.command == "backlog-merge-reconcile":
                from contextlib import nullcontext as _nc

                from command_center.orchestrator.review_merge import (
                    reconcile_merge_evidence,
                )

                report = reconcile_merge_evidence(lambda: _nc(conn), args.repo_path)
                for task_id, sha, reason in report.suspect:
                    print(f"SUSPECT   {task_id} sha={sha}: {reason}")
                for task_id, reason in report.skipped:
                    print(f"SKIP      {task_id}: {reason}")
                print(
                    f"verified {len(report.verified)}, "
                    f"suspect {len(report.suspect)}, "
                    f"skipped {len(report.skipped)}"
                )
                # Non-zero exit surfaces a real finding to a human/CI without
                # ever touching the database -- report-only stays report-only.
                return 1 if report.suspect else 0

            if args.command == "enroll-mint":
                ticket_secret = secrets.token_hex(32)
                ticket_hash = hashlib.sha256(
                    ticket_secret.encode("utf-8")
                ).hexdigest()
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM enroll_mint_ticket(%s, %s, %s, %s, %s, %s)",
                        (
                            args.principal_id,
                            args.host,
                            ticket_hash,
                            args.cidr,
                            args.ttl,
                            args.purpose,
                        ),
                    )
                    ticket_id, refused = cur.fetchone()
                if refused:
                    print(f"refused: {refused}", file=sys.stderr)
                    return 1
                print(f"ticket:  {ticket_id}")
                print("code (shown once -- hand it to the device, then discard it):")
                print(ticket_secret)
                return 0

            if args.command == "enroll-redeem":
                from command_center.ops.credential_rotation import scram_verifier

                ticket_secret = args.ticket
                if ticket_secret is None:
                    ticket_secret = sys.stdin.readline().strip()
                if not ticket_secret:
                    print(
                        "no code given (pass it as an argument or on stdin)",
                        file=sys.stderr,
                    )
                    return 2
                descriptor = {
                    "machine_id": args.machine_id or _read_machine_id(),
                    "os": args.os or platform.system().lower(),
                    "arch": args.arch or platform.machine(),
                    "hostname": args.hostname or socket.gethostname(),
                }
                host_secret = secrets.token_hex(32)
                secret_hash = hashlib.sha256(
                    host_secret.encode("utf-8")
                ).hexdigest()
                verifier = scram_verifier(host_secret)
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT * FROM enroll_redeem_ticket(%s, %s, %s, %s::jsonb)",
                        (ticket_secret, secret_hash, verifier, json.dumps(descriptor)),
                    )
                    (
                        principal_id,
                        db_role,
                        credential_id,
                        expires_at,
                        refused,
                    ) = cur.fetchone()
                if refused:
                    print(f"refused: {refused}", file=sys.stderr)
                    return 1
                rendered = (
                    f"AICC_PG_USER={db_role}\nAICC_PG_PASSWORD={host_secret}\n"
                )
                if args.out:
                    args.out.parent.mkdir(parents=True, exist_ok=True)
                    args.out.write_text(rendered, encoding="utf-8")
                    os.chmod(args.out, 0o600)
                    print(f"credential written: {args.out}", file=sys.stderr)
                else:
                    print(rendered, end="")
                print(
                    f"principal={principal_id} role={db_role} "
                    f"credential={credential_id} expires={expires_at.isoformat()}",
                    file=sys.stderr,
                )
                return 0

            if args.command == "downgrade":
                if not args.confirmed:
                    print(
                        "refusing to downgrade without "
                        "--yes-i-understand-this-drops-data",
                        file=sys.stderr,
                    )
                    return 2
                reverted = migrations.downgrade(conn, target=args.to)
                print(
                    f"reverted: {list(reverted)}" if reverted else "nothing to revert"
                )
                return 0
    finally:
        pool.close_pool()

    return 2  # pragma: no cover — argparse rejects unknown commands first


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
