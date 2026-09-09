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

# How many items the fleet can hold CLAIMED at once: one per worker lane, since
# a lane runs a single attempt at a time (`worker.daemon.run_forever` claims,
# handles, then loops). The default is the canonical lane registry
# `deploy/aicc/worker-lanes`, and `--claim-capacity` overrides it for a fleet
# that has scaled;
# `test_the_claim_capacity_default_matches_the_canonical_lane_registry` pins
# the two together so adding a lane cannot silently leave this number behind.
#
# It has to exist because THE QUEUE IS DESIGNED TO HOLD MORE DISPATCHED WORK
# THAN THE FLEET CAN CLAIM. `PlanLimits.wip_limit` is 4 against these 2 lanes
# (`backlog_dispatch` bounds concurrency by per-repository writer leases, and
# the fleet has three repositories), so a due `ready` item routinely waits for
# a lane to free -- for as long as a whole attempt, which
# `voyn-aicc-worker@.service` allows 3660s plus provisioning. That item is
# queued behind a busy fleet, which is backpressure and not a stall.
DEFAULT_CLAIM_CAPACITY = 2

# The longest a free lane can take to notice due work: the daemon's idle poll
# backs off to `WorkerConfig.idle_max_seconds` (30s) and no further, so by then
# every free lane has polled at least once. Below this floor the work has not
# yet been offered to a claimer, so `throughput_stalled` -- the one check that
# fires INSIDE the stall window -- must not read a just-enqueued item as
# starvation. `test_the_throughput_floor_covers_the_workers_poll_ceiling` pins
# it against the daemon's own constant.
CLAIM_POLL_CEILING_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    ready: int
    claimed: int
    succeeded: int
    dead: int
    success_age_seconds: float | None
    recent_dead: int = 0
    #: dead-lettered in the trailing hour by an executor quota/spend/rate refusal
    recent_quota_dead: int = 0
    #: succeeded in the trailing hour (throughput); None when not measured
    recent_succeeded: int | None = None
    # The three disjoint classes of pending work. `_QUEUE_SNAPSHOT_SQL` says
    # how each is measured; `evaluate` decides which of them is STARVED,
    # because that question needs `--claim-capacity` and the database does not
    # know how many lanes the fleet runs.
    #: Ready items whose ``available_at`` has passed -- claimable right now by
    #: any free lane. An item still inside its retry backoff or enqueue delay
    #: is waiting by design and is deliberately not counted.
    ready_due: int = 0
    #: How long the oldest due ready item has been claimable. ``None`` exactly
    #: when ``ready_due`` is 0 -- one filter produces both.
    ready_due_age_seconds: float | None = None
    #: Claims with no live lease: the attempt expired (or vanished) and no
    #: reaper recovered it. The zombie claim, starved at any capacity.
    lapsed_claims: int = 0
    #: How long the oldest lapsed claim has been leaseless. ``None`` exactly
    #: when ``lapsed_claims`` is 0.
    lapsed_claim_age_seconds: float | None = None
    #: Claims under a live, renewing lease -- lanes doing their job. Weighed
    #: against ``--claim-capacity`` to tell queued work from stalled work.
    attended_claims: int = 0
    #: Age of the oldest claim under a live lease. Bounded by
    #: ``--max-claim-seconds``, never by ``--max-stalled-seconds``.
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
# WHAT THIS STATEMENT DOES AND DOES NOT DECIDE
# ---------------------------------------------------------------------------
# It sorts pending work into three DISJOINT classes and times each one. It
# does not decide which of them is stalled: that needs the fleet's claim
# capacity, which lives in `evaluate` because the database cannot know how
# many worker lanes are running.
#
#   * READY AND DUE (`available_at <= now()`) -- claimable this second by any
#     free lane; `queue_claim` takes the oldest such row with no repository or
#     lane affinity. A ready item still inside its retry backoff or enqueue
#     delay is waiting BY DESIGN and is not in this class; counting it made the
#     queue's own backoff look like a stall.
#   * LAPSED CLAIM -- claimed, but the attempt's lease expired or vanished and
#     no reaper recovered it. This is the zombie the stall check was written
#     for. `aicc-queue-reaper.timer` runs every minute, so a lapse older than
#     the stall window means recovery itself is broken, at any capacity.
#   * ATTENDED CLAIM -- claimed under a live, renewing lease. The lease IS the
#     liveness proof: `queue_heartbeat` only renews while a worker is alive and
#     still owns the attempt, and the moment it stops, `visible_until` lapses
#     and the row moves into the class above on its own.
#
# WHY THE CLASSES EXIST (monitor_finding #481, `control-01:queue`)
# ---------------------------------------------------------------------------
# The stall clock used to be `now() - min(updated_at)` over every ready or
# claimed row, and `work_item.updated_at` for a claimed row is the moment it
# was CLAIMED -- heartbeats renew `work_attempt.visible_until` and never touch
# the item. So a lane doing exactly what it is deployed to do reported a stall
# the moment its run passed `--max-stalled-seconds`, and the fleet's own units
# say that is the ordinary case, not the exception:
# `voyn-aicc-worker@.service` sets `TimeoutStopSec=3660s` for a single attempt,
# and the planner's cascade adds worktree provisioning (a 600s clone timeout)
# on top of the agent's own run.
#
# Separating the attended claims was necessary and not sufficient: the ready
# rows kept the probe red on their own. `PlanLimits.wip_limit` is 4 against 2
# lanes, so the queue is MEANT to hold work no lane can attend yet, and that
# surplus row sits ready and due for a whole attempt. Hence the counts below
# and the capacity comparison in `evaluate`: a due ready item is a stall only
# when a lane was free to take it.
_QUEUE_SNAPSHOT_SQL = """
    WITH pending AS (
        SELECT
            w.state,
            w.updated_at,
            w.dead_reason,
            (w.state = 'claimed'
             AND coalesce(a.state = 'active' AND a.visible_until > now(), false))
                AS attended,
            (w.state = 'ready' AND w.available_at <= now()) AS ready_due,
            -- A ready item has been claimable since it became due, which is
            -- its enqueue/retry time when that is later than its last touch.
            greatest(w.updated_at, w.available_at) AS due_since,
            -- A claim has been leaseless since the lease lapsed; with no
            -- attempt row at all, since the claim itself.
            coalesce(a.visible_until, w.updated_at) AS leaseless_since
          FROM work_item w
          LEFT JOIN work_attempt_public a ON a.attempt_id = w.current_attempt_id
    ), classified AS (
        SELECT *, (state = 'claimed' AND NOT attended) AS lapsed_claim
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
        count(*) FILTER (WHERE ready_due),
        extract(epoch FROM (
            now() - min(due_since) FILTER (WHERE ready_due)
        )),
        count(*) FILTER (WHERE lapsed_claim),
        extract(epoch FROM (
            now() - min(leaseless_since) FILTER (WHERE lapsed_claim)
        )),
        count(*) FILTER (WHERE attended),
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
        recent_dead,
        recent_quota_dead,
        recent_succeeded,
        ready_due,
        ready_due_age,
        lapsed_claims,
        lapsed_claim_age,
        attended_claims,
        live_claim_age,
    ) = row

    def seconds(value: Any) -> float | None:
        return float(value) if value is not None else None

    return QueueSnapshot(
        ready=int(ready),
        claimed=int(claimed),
        succeeded=int(succeeded),
        dead=int(dead),
        success_age_seconds=seconds(success_age),
        recent_dead=int(recent_dead),
        recent_quota_dead=int(recent_quota_dead),
        recent_succeeded=int(recent_succeeded),
        ready_due=int(ready_due),
        ready_due_age_seconds=seconds(ready_due_age),
        lapsed_claims=int(lapsed_claims),
        lapsed_claim_age_seconds=seconds(lapsed_claim_age),
        attended_claims=int(attended_claims),
        live_claim_age_seconds=seconds(live_claim_age),
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
    claim_capacity: int = DEFAULT_CLAIM_CAPACITY,
) -> MonitorReport:
    active_workers = sum(state == "active" for state in worker_states.values())
    failures: list[str] = []
    if active_workers < minimum_active_workers:
        failures.append(f"active_workers:{active_workers}<{minimum_active_workers}")
    if not prometheus_ready:
        failures.append("prometheus_unready")

    # An old last-success timestamp is normal when there is no work. It becomes
    # context in the report once work appears; the oldest STARVED item is the
    # alert clock. Unrelated successful work must not hide a zombie claim, and
    # neither a lane legitimately holding a live lease for longer than the
    # stall window nor work queued behind a full fleet may be reported as one.
    if queue is not None:
        # Is any lane free to claim? `greatest(p_wip_limit, 1)` is the same
        # convention `backlog_dispatch` applies to its own cap: a capacity of
        # 0 would otherwise mean "no lane can ever claim", which would excuse
        # every unclaimed item forever.
        spare_capacity = queue.attended_claims < max(claim_capacity, 1)
        # STARVED = pending work nobody is accountable for AND nobody is
        # merely too busy for. A lapsed claim is starved at any capacity: no
        # lane is holding it, so no lane being free is irrelevant to it.
        starved_ages = [queue.lapsed_claim_age_seconds]
        if spare_capacity:
            starved_ages.append(queue.ready_due_age_seconds)
        measured = [age for age in starved_ages if age is not None]
        # The oldest starved item, or None when nothing is starved. Counts are
        # not consulted: each age comes from the same filter as its count and
        # is None exactly when that count is 0, so a second gate on the counts
        # could only ever short-circuit ahead of the threshold it guards.
        starved_age = max(measured) if measured else None

        if starved_age is not None and starved_age > max_stalled_seconds:
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
        # Throughput: zero successes in the trailing hour, with starved work to
        # corroborate it, is a stalled pipeline even when every lane shows
        # active -- the lanes may be spinning on refusals. It is the one check
        # that fires INSIDE the stall window, so it carries that window's two
        # bounds explicitly: above `CLAIM_POLL_CEILING_SECONDS`, because work
        # no claimer has been offered yet proves nothing (an hour with no
        # successes is ordinary here -- one attempt may run longer than that --
        # so a queue that had been empty all night would otherwise go red the
        # second the first task was enqueued); and at or below the stall
        # window, above which `queue_stalled` already reports it.
        if (
            queue.recent_succeeded is not None
            and queue.recent_succeeded == 0
            and starved_age is not None
            and CLAIM_POLL_CEILING_SECONDS < starved_age <= max_stalled_seconds
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
        "--claim-capacity",
        type=int,
        default=DEFAULT_CLAIM_CAPACITY,
        help=(
            "How many items the fleet can hold claimed at once -- one per "
            "worker lane. While that many claims are under live leases, due "
            "ready work is queued behind a busy fleet rather than stalled, "
            "and only --max-claim-seconds bounds it."
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
            "Reconcile this source's monitor_findings with the measurement: "
            "every failure becomes an open finding, and every finding this tick "
            "no longer measures is cleared -- each independently, so one "
            "lingering red check cannot hold the others open. The planner turns "
            "a red monitor into a pipeline task instead of a failed unit."
        ),
    )
    return parser


def finding_key(failure: str) -> str:
    """The stable identity of a failure: its code before the first ':'
    (`active_workers:2<4` -> `active_workers`), truncated like the column."""
    return failure.split(":", 1)[0].strip()[:200] or failure[:200]


def record_findings(source: str, failures: tuple[str, ...], detail: dict[str, Any]) -> None:
    """Reconcile this source's open findings with what this tick measured,
    through the SECURITY DEFINER functions of migrations 0021 and 0024
    (granted to aicc_app and aicc_worker). Every failure still measured is
    recorded (or refreshed); every OTHER open finding of this source is
    cleared, whether or not anything is still red.

    A red monitor becomes a task the fleet fixes; a failure that has stopped
    measuring is done being one -- even when a sibling check is still red.
    That independence is the whole point: the findings are keyed per failure
    because the planner mints one task per failure, so clearing them only on
    an all-green tick made every one of them hostage to the worst of them
    (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, monitor_finding #481: this probe
    runs with `--max-recent-dead 0`, so one dead-lettered item an hour keeps
    `dead_letter_growth` red forever, and `queue_stalled` -- healthy again
    since the stall clock was fixed -- had no reachable way out behind it).

    Never lets a recording problem mask the measurement: raises, and main()
    reports monitor_error while still exiting non-zero."""
    from command_center.db import pool
    from command_center.db.config import load_config

    # Identity is the failure CODE (before the first ':'), never the
    # measurement: "dead_letter_growth:53>0" and "dead_letter_growth:54>0" are
    # one open finding and one task, not one per tick (control-01,
    # 2026-09-08: four tasks in ten minutes for the same red probe). The
    # measured text travels in the detail. It is also what the clear below
    # matches on, so the two calls have to agree on it -- hence one list,
    # zipped with the failures it came from, rather than the key computed
    # twice.
    keyed = [(finding_key(failure), failure) for failure in failures]

    pool.open_pool(load_config())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            for key, failure in keyed:
                cur.execute(
                    "SELECT monitor_record_finding(%s, %s, %s::jsonb)",
                    (
                        source,
                        key,
                        json.dumps({**detail, "failure": failure}, sort_keys=True),
                    ),
                )
            # Clearing runs on EVERY tick, not only a green one. With no
            # failures the argument is empty and this is exactly 0021's
            # `monitor_clear_finding(source)`; with failures it clears the
            # complement, leaving the still-red rows untouched so they keep
            # their `opened_at`, their `finding_id` and the `task_id` the
            # planner linked to them.
            cur.execute(
                "SELECT monitor_clear_finding(%s, %s)",
                (source, [key for key, _failure in keyed]),
            )
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
            claim_capacity=args.claim_capacity,
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
