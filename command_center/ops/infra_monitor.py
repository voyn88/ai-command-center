"""Fail-closed health check for the current AICC worker architecture.

The predecessor lived only in ``/usr/local/sbin`` and watched retired unit
names.  This module deliberately derives health from the templated worker
lanes, the durable queue, and the configured metrics endpoint instead.
"""

from __future__ import annotations

import argparse
import http.client
import json
import subprocess
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

# The ceiling on ONE claim held under a live, renewing lease. Above the whole
# of a legitimate attempt and nowhere near it: `voyn-aicc-worker@.service`
# gives a single attempt TimeoutStopSec=3660s, on top of which the handler
# provisions a worktree (a 600s clone timeout). 5400s leaves half an hour of
# headroom over that and still catches a lease being renewed by a process that
# has stopped making progress.
DEFAULT_MAX_CLAIM_SECONDS = 5400.0


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    ready: int
    claimed: int
    succeeded: int
    dead: int
    success_age_seconds: float | None
    #: How long the oldest UNATTENDED pending item has been waiting -- see
    #: ``_QUEUE_SNAPSHOT_SQL`` for what "unattended" means and why a pending
    #: item under a live lease is deliberately excluded. ``None`` when every
    #: pending item is attended (or there is no pending work at all).
    pending_age_seconds: float | None
    recent_dead: int = 0
    #: dead-lettered in the trailing hour by an executor quota/spend/rate refusal
    recent_quota_dead: int = 0
    #: succeeded in the trailing hour (throughput); None when not measured
    recent_succeeded: int | None = None
    #: How many pending items are unattended right now. Counted by the same
    #: filter that produces ``pending_age_seconds``, so the two always agree:
    #: 0 exactly when the age is ``None``.
    pending_unattended: int = 0
    #: Age of the oldest claim that IS under a live lease -- the fleet working,
    #: not the fleet stuck. ``None`` when nothing is claimed under a live lease.
    live_claim_age_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class MonitorReport:
    ok: bool
    active_workers: int
    discovered_workers: int
    queue: QueueSnapshot | None
    prometheus_ready: bool
    failures: tuple[str, ...]


def parse_worker_units(output: str) -> dict[str, str]:
    """Parse ``systemctl list-units`` without accepting retired unit names."""
    states: dict[str, str] = {}
    for line in output.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 4:
            continue
        unit, _load, active, _sub = fields[:4]
        if unit.startswith("voyn-aicc-worker@") and unit.endswith(".service"):
            states[unit] = active
    return states


def discover_worker_units() -> dict[str, str]:
    completed = subprocess.run(
        [
            "systemctl",
            "list-units",
            "voyn-aicc-worker@*.service",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return parse_worker_units(completed.stdout)


# The queue measurement, as one named statement so a test can execute exactly
# what production executes against a real server.
#
# WHAT "UNATTENDED" MEANS, AND WHY THE STALL CLOCK IS BUILT ON IT
# ---------------------------------------------------------------------------
# The stall clock used to be `now() - min(updated_at)` over every ready or
# claimed row, and `work_item.updated_at` for a claimed row is the moment it
# was CLAIMED -- heartbeats renew `work_attempt.visible_until`, they never
# touch the item. So a lane doing exactly what it is deployed to do reported
# a stall the moment its run passed `--max-stalled-seconds`, and the fleet's
# own units say that is the ordinary case, not the exception:
# `voyn-aicc-worker@.service` sets `TimeoutStopSec=3660s` for a single
# attempt, and the planner's cascade adds worktree provisioning (a 600s clone
# timeout) on top of the agent's own run. `control-01:queue` therefore went
# red with `queue_stalled` on healthy work, opened a monitor finding, and the
# planner minted a task for it (monitor_finding #481) -- a fail-closed
# monitor that could not be green while the fleet worked.
#
# A pending item is UNATTENDED when nobody is accountable for it right now:
#
#   * ready and due (`available_at <= now()`) -- a claimer could have taken it
#     and did not. A ready item still inside its retry backoff or enqueue
#     delay is waiting BY DESIGN and is not counted; counting it made the
#     queue's own backoff look like a stall.
#   * claimed with no live lease -- the attempt expired (or vanished) and no
#     reaper has recovered it. This is the zombie claim the check was written
#     for, and it is still caught: `aicc-queue-reaper.timer` runs every
#     minute, so a lapse older than the stall window means recovery itself is
#     broken.
#
# A claim under a LIVE lease is excluded, because the lease is the liveness
# proof: `queue_heartbeat` only renews while a worker is alive and still owns
# the attempt, and the moment it stops, `visible_until` lapses and the row
# joins the unattended set on its own. Such a claim is measured separately as
# `live_claim_age_seconds` and bounded by `--max-claim-seconds`, so a handler
# wedged behind a heartbeat thread that keeps beating is still caught -- just
# at a ceiling above one legitimate attempt instead of below it.
_QUEUE_SNAPSHOT_SQL = """
    WITH pending AS (
        SELECT
            w.state,
            w.updated_at,
            w.dead_reason,
            (w.state = 'claimed'
             AND coalesce(a.state = 'active' AND a.visible_until > now(), false))
                AS attended,
            CASE
                WHEN w.state = 'ready' THEN greatest(w.updated_at, w.available_at)
                ELSE coalesce(a.visible_until, w.updated_at)
            END AS waiting_since,
            (w.state = 'ready' AND w.available_at > now()) AS not_due
          FROM work_item w
          LEFT JOIN work_attempt_public a ON a.attempt_id = w.current_attempt_id
    ), classified AS (
        SELECT *,
               (state IN ('ready', 'claimed') AND NOT attended AND NOT not_due)
                   AS unattended
          FROM pending
    )
    SELECT
        count(*) FILTER (WHERE state = 'ready'),
        count(*) FILTER (WHERE state = 'claimed'),
        count(*) FILTER (WHERE state = 'succeeded'),
        count(*) FILTER (WHERE state = 'dead'),
        extract(epoch FROM (
            now() - max(updated_at) FILTER (WHERE state = 'succeeded')
        )),
        extract(epoch FROM (
            now() - min(waiting_since) FILTER (WHERE unattended)
        )),
        count(*) FILTER (
            WHERE state = 'dead'
              AND updated_at > now() - interval '1 hour'
        ),
        count(*) FILTER (
            WHERE state = 'dead'
              AND updated_at > now() - interval '1 hour'
              AND dead_reason ~* '(quota|spend limit|weekly limit|session limit|credit balance|rate limit)'
        ),
        count(*) FILTER (
            WHERE state = 'succeeded'
              AND updated_at > now() - interval '1 hour'
        ),
        count(*) FILTER (WHERE unattended),
        extract(epoch FROM (
            now() - min(updated_at) FILTER (WHERE attended)
        ))
    FROM classified
"""


def snapshot_from_row(row: tuple[Any, ...]) -> QueueSnapshot:
    """Map one ``_QUEUE_SNAPSHOT_SQL`` row onto the report's shape.

    Separate from ``read_queue_snapshot`` only so the database test can run
    the production statement and read it the production way: a test that
    re-implemented this mapping could pass while production read the columns
    in a different order.
    """
    (
        ready,
        claimed,
        succeeded,
        dead,
        success_age,
        pending_age,
        recent_dead,
        recent_quota_dead,
        recent_succeeded,
        pending_unattended,
        live_claim_age,
    ) = row
    return QueueSnapshot(
        ready=int(ready),
        claimed=int(claimed),
        succeeded=int(succeeded),
        dead=int(dead),
        success_age_seconds=(float(success_age) if success_age is not None else None),
        pending_age_seconds=(float(pending_age) if pending_age is not None else None),
        recent_dead=int(recent_dead),
        recent_quota_dead=int(recent_quota_dead),
        recent_succeeded=int(recent_succeeded),
        pending_unattended=int(pending_unattended),
        live_claim_age_seconds=(
            float(live_claim_age) if live_claim_age is not None else None
        ),
    )


def read_queue_snapshot() -> QueueSnapshot:
    from command_center.db import pool
    from command_center.db.config import load_config

    pool.open_pool(load_config())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_QUEUE_SNAPSHOT_SQL)
            row = cur.fetchone()
    finally:
        pool.close_pool()
    return snapshot_from_row(row)


def prometheus_is_ready(url: str) -> bool:
    connection: http.client.HTTPConnection | None = None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            return False
        client = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = client(parsed.hostname, parsed.port, timeout=5)
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        connection.request("GET", target)
        response = connection.getresponse()
        body = response.read(256).decode("utf-8", errors="replace")
        return response.status == 200 and "ready" in body.lower()
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


def evaluate(
    worker_states: dict[str, str],
    queue: QueueSnapshot | None,
    *,
    minimum_active_workers: int,
    max_stalled_seconds: float,
    prometheus_ready: bool,
    max_recent_dead: int = 0,
    max_claim_seconds: float = DEFAULT_MAX_CLAIM_SECONDS,
) -> MonitorReport:
    active_workers = sum(state == "active" for state in worker_states.values())
    failures: list[str] = []
    if active_workers < minimum_active_workers:
        failures.append(f"active_workers:{active_workers}<{minimum_active_workers}")
    if not prometheus_ready:
        failures.append("prometheus_unready")

    # An old last-success timestamp is normal when there is no work. It becomes
    # context in the report once work appears; the oldest UNATTENDED pending
    # item is the alert clock (`_QUEUE_SNAPSHOT_SQL` defines unattended and
    # says why). Unrelated successful work must not hide a zombie claim, and a
    # lane legitimately holding a live lease for longer than the stall window
    # must not be reported as one.
    if queue is not None:
        pending_is_stale = (
            queue.pending_age_seconds is not None
            and queue.pending_age_seconds > max_stalled_seconds
        )
        if queue.pending_unattended > 0 and pending_is_stale:
            failures.append("queue_stalled")
        # The other half of the zombie question: a claim whose lease keeps
        # being renewed is progress right up until it is not. The daemon's
        # heartbeat runs in its own thread beside the handler, so a wedged
        # handler can be beaten for as long as the process lives; this ceiling
        # sits above one whole legitimate attempt (the worker unit's
        # TimeoutStopSec=3660s plus provisioning) so only a claim no attempt
        # could explain trips it.
        if (
            queue.live_claim_age_seconds is not None
            and queue.live_claim_age_seconds > max_claim_seconds
        ):
            failures.append(
                f"claim_overdue:{int(queue.live_claim_age_seconds)}s"
                f">{int(max_claim_seconds)}s"
            )
        if queue.recent_dead > max_recent_dead:
            failures.append(
                f"dead_letter_growth:{queue.recent_dead}>{max_recent_dead}"
            )
        # Executor quota/spend/rate refusals are a capacity fact the fleet
        # cannot retry through; surface them as their own class so routing
        # (quota-aware cascade) and budgets get a task, not a guess.
        if queue.recent_quota_dead > 0:
            failures.append(f"executor_quota_exhausted:{queue.recent_quota_dead}")
        # Throughput: work waiting (past the stall clock) with nothing having
        # succeeded in the trailing hour is a stalled pipeline even when every
        # lane shows active -- the lanes may be spinning on refusals. Gated on
        # UNATTENDED work for the same reason `queue_stalled` is: a fleet whose
        # every pending item is under a live lease is busy, not starved, and an
        # hour is a perfectly ordinary length for one attempt here.
        if (
            queue.recent_succeeded is not None
            and queue.recent_succeeded == 0
            and queue.pending_unattended > 0
            and not pending_is_stale
        ):
            failures.append("throughput_stalled:0_succeeded_in_1h")

    return MonitorReport(
        ok=not failures,
        active_workers=active_workers,
        discovered_workers=len(worker_states),
        queue=queue,
        prometheus_ready=prometheus_ready,
        failures=tuple(failures),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m command_center.ops.infra_monitor")
    parser.add_argument("--minimum-active-workers", type=int, default=1)
    parser.add_argument("--max-stalled-seconds", type=float, default=900)
    parser.add_argument(
        "--max-claim-seconds",
        type=float,
        default=DEFAULT_MAX_CLAIM_SECONDS,
        help=(
            "How long one claim may be held under a LIVE (heartbeating) lease "
            "before it is reported as overdue. Distinct from "
            "--max-stalled-seconds, which times work nobody is attending: a "
            "long agent run is attended, so it belongs under this ceiling and "
            "not under that clock."
        ),
    )
    parser.add_argument(
        "--max-recent-dead",
        type=int,
        default=0,
        help="Maximum dead-lettered items allowed in the trailing hour.",
    )
    parser.add_argument("--prometheus-url", required=True)
    parser.add_argument(
        "--skip-workers",
        action="store_true",
        help="Do not inspect local worker units (for the control-host queue probe).",
    )
    parser.add_argument(
        "--skip-queue",
        action="store_true",
        help="Do not read queue tables (for the least-privileged worker-host probe).",
    )
    parser.add_argument(
        "--record-findings",
        metavar="SOURCE",
        default="",
        help=(
            "Record every failure as an open monitor_finding under this source "
            "(and clear the source's findings when healthy), so the planner turns "
            "a red monitor into a pipeline task instead of a failed unit."
        ),
    )
    return parser


def finding_key(failure: str) -> str:
    """The stable identity of a failure: its code before the first ':'
    (`active_workers:2<4` -> `active_workers`), truncated like the column."""
    return failure.split(":", 1)[0].strip()[:200] or failure[:200]


def record_findings(source: str, failures: tuple[str, ...], detail: dict[str, Any]) -> None:
    """Write the measurement to the database through the SECURITY DEFINER
    functions of migration 0021 (granted to aicc_app and aicc_worker). A red
    monitor becomes a task the fleet fixes; a healthy one clears its rows.
    Never lets a recording problem mask the measurement: raises, and main()
    reports monitor_error while still exiting non-zero."""
    from command_center.db import pool
    from command_center.db.config import load_config

    pool.open_pool(load_config())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            if failures:
                for failure in failures:
                    # Identity is the failure CODE (before the first ':'),
                    # never the measurement: "dead_letter_growth:53>0" and
                    # "dead_letter_growth:54>0" are one open finding and one
                    # task, not one per tick (control-01, 2026-09-08: four
                    # tasks in ten minutes for the same red probe). The
                    # measured text travels in the detail.
                    cur.execute(
                        "SELECT monitor_record_finding(%s, %s, %s::jsonb)",
                        (
                            source,
                            finding_key(failure),
                            json.dumps({**detail, "failure": failure}, sort_keys=True),
                        ),
                    )
            else:
                cur.execute("SELECT monitor_clear_finding(%s)", (source,))
            conn.commit()
    finally:
        pool.close_pool()


def _json_report(report: MonitorReport) -> dict[str, Any]:
    payload = asdict(report)
    payload["failures"] = list(report.failures)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        workers = {} if args.skip_workers else discover_worker_units()
        queue = None if args.skip_queue else read_queue_snapshot()
        metrics_ready = prometheus_is_ready(args.prometheus_url)
        report = evaluate(
            workers,
            queue,
            minimum_active_workers=args.minimum_active_workers,
            max_stalled_seconds=args.max_stalled_seconds,
            prometheus_ready=metrics_ready,
            max_recent_dead=args.max_recent_dead,
            max_claim_seconds=args.max_claim_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - the monitor itself must fail closed
        print(json.dumps({"ok": False, "failures": [f"monitor_error:{exc}"]}))
        return 1
    payload = _json_report(report)
    recorded = True
    if args.record_findings:
        try:
            record_findings(args.record_findings, report.failures, payload)
            payload["findings_recorded"] = True
        except Exception as exc:  # noqa: BLE001 - recording must not hide the measurement
            # The measurement is still printed in full; the exit code says
            # the monitor did NOT do its whole job. A healthy measurement
            # whose persistence failed exited 0 before (review of fc167cf7),
            # so a broken finding store went unnoticed exactly when the
            # monitor is trusted to turn red into a task.
            recorded = False
            payload["findings_recorded"] = False
            payload["findings_error"] = str(exc)[:200]
    print(json.dumps(payload, sort_keys=True))
    return 0 if report.ok and recorded else 1


if __name__ == "__main__":
    raise SystemExit(main())
