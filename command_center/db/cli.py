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
import logging
import sys
from contextlib import nullcontext
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

    backfill = sub.add_parser(
        "historical-backfill",
        help="One-time copy (VOYN-W0-AICC-SRV-07): every pre-dual-write row in "
        "the SQLite runtime database into its PostgreSQL mirror, parents "
        "before children, then reconcile each table (idempotent; safe to "
        "re-run after a partial failure). Must run on a host in the SQLite "
        "writer's own time zone -- see command_center/db/historical_backfill.py.",
    )
    backfill.add_argument("sqlite_path", help="Path to the SQLite runtime database.")
    backfill.add_argument(
        "--table",
        action="append",
        dest="tables",
        default=None,
        metavar="TABLE",
        help="Restrict the run to this table (repeatable); default is every "
        "mirrored table, in dependency order.",
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
                    review_once,
                )

                store = WorkQueueStore(lambda: _nc(conn))
                enqueue = _review_enqueue(store)
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

            if args.command == "historical-backfill":
                from pathlib import Path as _Path

                from command_center.db.historical_backfill import (
                    run_historical_backfill,
                )

                report = run_historical_backfill(
                    _Path(args.sqlite_path), lambda: nullcontext(conn), tables=args.tables
                )
                failed = False
                for table_report in report.tables:
                    print(
                        f"{table_report.table:24} source={table_report.source_rows:<6} "
                        f"upserted={table_report.upserted:<6} "
                        f"errors={len(table_report.errors):<3} "
                        f"divergent={len(table_report.divergent)}"
                    )
                    for error in table_report.errors:
                        print(f"  ERROR      {error}")
                    for row in table_report.divergent:
                        print(f"  DIVERGENT  {row.get('id')}: {row.get('fields')}")
                    failed = failed or not table_report.ok
                return 1 if failed else 0

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
