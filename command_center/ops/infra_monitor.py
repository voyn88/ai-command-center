"""Fail-closed health check for the current AICC worker architecture.

The predecessor lived only in ``/usr/local/sbin`` and watched retired unit
names.  This module deliberately derives health from the templated worker
lanes, the durable queue, and the configured metrics endpoint instead.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
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

# Where a probe's finding source may come from besides `--record-findings`.
# `voyn-queue-monitor.service` sets it as `Environment=` rather than appending
# the flag to its ExecStart: that line carries the control host's absolute
# install path, and in this public repository `scripts/ci/prepush/
# leak_guard.sh` refuses any ADDED line containing one -- the guarded publisher
# runs the same guard, so re-typing the line to append a flag would have been a
# refused publish rather than a lint. The flag still wins when both are given.
FINDING_SOURCE_ENV = "AICC_MONITOR_FINDING_SOURCE"

# The longest a free lane can take to notice due work. Below this floor the
# work has not yet been offered to a claimer, so `throughput_stalled` -- the
# one check that fires INSIDE the stall window -- must not read a
# just-enqueued item as starvation.
#
# IT IS TWICE `WorkerConfig.idle_max_seconds`, NOT ONCE, and the difference is
# a live false positive rather than a rounding argument. The daemon's idle
# poll is
#
#     self._sleep(min(idle + random.uniform(0, idle), cap))
#     idle = min(idle * 2, self._config.idle_max_seconds)
#
# so `idle_max_seconds` (30s) caps THE BACKOFF, not the sleep: once the
# backoff saturates, each gap between polls is uniform on [30s, 60s). This
# constant was 30.0 and was pinned by a test asserting only
# `>= WorkerConfig().idle_max_seconds`, which the wrong number satisfies. An
# item enqueued onto a queue that had been quiet all night could therefore sit
# 30-60s before any lane had polled -- unoffered, not starved -- and be read
# at the probe's next two-minute sample as `throughput_stalled`, which is
# precisely the reading this floor exists to prevent ("a queue that had been
# empty all night would otherwise go red the second the first task was
# enqueued", below in `evaluate`).
#
# `test_the_throughput_floor_covers_the_workers_poll_ceiling` now derives the
# bound by running the daemon's own backoff to saturation instead of restating
# one of its constants, so a future change to either the cap or the jitter
# fails the test rather than the fleet.
CLAIM_POLL_CEILING_SECONDS = 60.0

# WHICH QUEUE THIS PROBE MEASURES. `work_item.queue` is a real dimension --
# `queue_enqueue` writes it, UNIQUE(queue, idempotency_key) keys on it, and
# `queue_claim(p_queue, ...)` SERVES EXACTLY ONE OF THEM -- so a measurement
# that spans queues is judged against a fleet that does not serve them all.
# The default is the queue the worker lanes actually claim from
# (`WorkerConfig.queue`), and
# `test_the_probes_queue_default_matches_the_daemons_own` pins the two
# together so they cannot drift.
#
# It is a NAMED CONSTANT here rather than an import of `WorkerConfig` because
# this module runs on the CONTROL host: `voyn-queue-monitor.service` execs it
# out of the control-plane checkout, and importing the worker package to read
# one string would make the probe depend on code that host does not otherwise
# need. `DEFAULT_CLAIM_CAPACITY` is pinned to `deploy/aicc/worker-lanes` the
# same way and for the same reason.
#
# A fleet that runs a SECOND queue gets a second probe with its own
# `--queue`, its own `--claim-capacity` and its own finding source -- not this
# one widened. Every threshold on this probe's ExecStart is already a fact
# about one queue's fleet (how many lanes claim from it, how long one of its
# attempts may run), and there is no honest way to judge two fleets by one
# set of them.
DEFAULT_QUEUE = "execution"


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
    #: Claims under a live, renewing lease -- lanes doing their job.
    #:
    #: NOT what ``--claim-capacity`` is weighed against: that is ``claimed``,
    #: because capacity asks which lanes are HOLDING an item and a lane whose
    #: lease slipped is still holding one (monitor_finding #13366; see
    #: ``evaluate``). This is the narrower fact -- how much of that holding
    #: is provably alive -- and it is what ``live_claim_age_seconds`` ages.
    attended_claims: int = 0
    #: Age of the oldest claim under a live lease. Bounded by
    #: ``--max-claim-seconds``, never by ``--max-stalled-seconds``.
    live_claim_age_seconds: float | None = None
    #: How long since the fleet last CHANGED what it was holding -- took an
    #: item (a claim) or gave one back (an attempt leaving ``active``). It is
    #: the only thing in this snapshot that measures the LANES rather than the
    #: work, and ``evaluate`` uses it to bound how long due ready work can be
    #: said to have been ignored. ``None`` when the queue has no attempts at
    #: all: no evidence either way, so it bounds nothing.
    fleet_idle_seconds: float | None = None


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
# It sorts pending work into three DISJOINT classes and times each one, and
# separately times the FLEET -- how long since any lane took an item or gave
# one back. It does not decide which of them is stalled: that needs the
# fleet's claim capacity, which lives in `evaluate` because the database
# cannot know how many worker lanes are running.
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
#
# And neither was THAT sufficient, because capacity gates the comparison while
# the ready row's clock keeps running underneath it (monitor_finding #2471).
# By the time a lane frees, the item it is about to claim has been due for
# hours, and for the moment before that claim commits the probe sees due work
# with a free lane and calls the fleet's own backpressure a stall. The last
# column below is what bounds it: the fleet's own clock, so that "ignored"
# cannot outlast "there was somebody ignoring it".
#
# ALL OF IT IS SCOPED TO ONE QUEUE (monitor_finding #2766)
# ---------------------------------------------------------------------------
# Every class above is a statement about A FLEET: "claimable by a free lane",
# "no lane is holding it", "lanes doing their job". `queue_claim(p_queue, ...)`
# takes the queue as its first argument and will not look outside it, and the
# lanes pass exactly one (`WorkerConfig.queue`). So the rows of another queue
# are not this fleet's business in either direction, and until this filter
# existed the probe got both directions wrong:
#
#   * A due `ready` row on any other queue was counted as work this fleet was
#     ignoring. No lane can ever claim it, so no fleet action could ever make
#     the probe green again -- an `open` `queue_stalled` finding, and the task
#     the planner mints from it, with no reachable exit. "The monitor clears
#     the finding when it measures healthy" was not a promise the measurement
#     could keep.
#   * Worse, and in the fail-OPEN direction a fail-closed monitor must never
#     have: claims on another queue counted toward THIS queue's
#     `--claim-capacity`. Two claims anywhere in the table filled this
#     queue's capacity, made `spare_capacity` false, and a genuine hours-old
#     stall on `execution` was excused as backpressure behind a fleet that was
#     not working on it at all.
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
         -- ONE QUEUE, because one fleet serves one queue. See the note above
         -- the parameter in `read_queue_snapshot`.
         WHERE w.queue = %s
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
        )),
        -- HOW LONG SINCE THE FLEET LAST MOVED. Occupancy changes at exactly
        -- two events, and `work_attempt` timestamps both: a claim (the row is
        -- inserted with `created_at = now()`, state 'active') and an attempt
        -- leaving 'active' -- completed, failed, or expired by the reaper --
        -- which stamps `updated_at = now()`. So the most recent of those two
        -- is the last moment the fleet took work or gave it back.
        --
        -- HEARTBEATS ARE DELIBERATELY EXCLUDED, and the CASE is what excludes
        -- them: `queue_heartbeat` also writes `updated_at = now()`, but on a
        -- row that is still 'active'. A renewed lease says a lane is alive,
        -- not that it was free to take anything, so reading `updated_at` for
        -- an active attempt would make a single heartbeating lane look like a
        -- fleet claiming continuously.
        --
        -- NULL when no attempt has ever been made, which is the honest answer
        -- for a queue nothing has ever claimed: `evaluate` then bounds
        -- nothing, and a ready item nobody has ever picked up is timed from
        -- its own due age alone.
        --
        -- SCOPED TO THIS QUEUE TOO, and that is not symmetry for its own
        -- sake: this column exists to say whether the lanes that serve THE
        -- MEASURED QUEUE are moving. An attempt on another queue's item is
        -- another fleet's lane doing another fleet's work; letting it wind
        -- this clock forward would excuse a stall here on the strength of
        -- progress somewhere else.
        (SELECT extract(epoch FROM (now() - max(
                    CASE WHEN a.state = 'active' THEN a.created_at
                         ELSE a.updated_at END)))
           FROM work_attempt_public a
           JOIN work_item wq ON wq.work_item_id = a.work_item_id
          WHERE wq.queue = %s)
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
        fleet_idle,
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
        fleet_idle_seconds=seconds(fleet_idle),
    )


def read_queue_snapshot(queue: str = DEFAULT_QUEUE) -> QueueSnapshot:
    """Measure one queue -- the one whose fleet this probe's thresholds
    describe. ``queue`` is bound twice because the statement asks two
    questions of it: which work is pending, and whether the lanes serving that
    work have moved."""
    from command_center.db import pool
    from command_center.db.config import load_config

    pool.open_pool(load_config())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(_QUEUE_SNAPSHOT_SQL, (queue, queue))
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
        #
        # OCCUPANCY, NOT ATTENDANCE, AND THE DIFFERENCE IS A LIVE FALSE
        # POSITIVE (monitor_finding #13366). This was
        # `queue.attended_claims`, which asks "how many lanes hold a LIVE
        # LEASE" -- but the question capacity has to answer is "how many
        # lanes are HOLDING AN ITEM", and a lane whose lease slipped is still
        # holding one. `queue_claim` will not hand it a second: the item stays
        # `claimed` until the reaper takes it back, and until then the lane is
        # running its handler with nowhere to put another attempt.
        #
        # A lapsed lease is still how a healthy long attempt can look for a
        # few seconds, and the measurement must survive it however rare it
        # gets. It is no longer ORDINARY, and that changed under this comment
        # rather than in it: the beat ran at `visibility_seconds / 3` (100s
        # against a 300s window), which puts the third beat ON the deadline,
        # so any two consecutive failed beats lapsed the lease -- a database
        # blip, or the `voyn-aicc-pgtunnel.service` restart the credential
        # rotation cycles on its own schedule. `WorkerDaemon` now renews with
        # a whole beat of margin (`beat_interval_seconds`,
        # VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED / monitor_finding #13420),
        # so that blip costs nothing and this rule has correspondingly less
        # to excuse. What still reaches it -- an outage past the tolerance, a
        # host that is gone -- is the same shape, and
        # `aicc-queue-reaper.timer` still clears it on the next minute while
        # the probe samples every two.
        #
        # MEASURED against a real PostgreSQL 16 server -- two lanes 40
        # minutes into legitimate attempts, the two surplus dispatched items
        # ready and due behind them, and lane B's lease 30 seconds past its
        # deadline while lane B is still running:
        #
        #     both leases live   attended=2 claimed=2  -> ok
        #     B's lease slipped  attended=1 claimed=2  -> queue_stalled
        #
        # Nothing about the fleet changed between those two lines. The lapse
        # itself is 30s, nowhere near the window; the whole verdict is the
        # 2400s due-ready clock, compared against 900s on the strength of a
        # free lane that does not exist.
        #
        # THE FLEET CLOCK CANNOT CATCH THIS ONE, which is why it survived the
        # bound that was added for exactly this shape. `fleet_idle_seconds`
        # is 2400 here PRECISELY BECAUSE both lanes have been busy the whole
        # time: no attempt changed state, which is what two healthy long runs
        # look like. The bound below excuses the instant a lane frees; it has
        # nothing to say about a fleet that never freed one.
        #
        # WHAT THIS GIVES UP, AND WHY IT IS ALREADY COVERED. A lapsed claim
        # can also mean the lane is GONE, in which case a lane really is free
        # and this excuses due work for as long as the claim sits unreaped.
        # That case is not lost, because it is measured twice over:
        # `lapsed_claim_age_seconds` is appended below UNCONDITIONALLY -- not
        # gated by capacity, not bounded by the fleet clock -- and fires at
        # this same window; and the reaper moves the item out of `claimed`
        # within a minute, after which the count drops, spare capacity
        # reappears and the due-ready clock runs again on its own. Nothing
        # here can outlast the reaper, and a reaper that stops is the one
        # thing the unconditional clock is for.
        spare_capacity = queue.claimed < max(claim_capacity, 1)
        # HOW LONG THE DUE READY WORK HAS ACTUALLY BEEN IGNORED, which is not
        # how long it has been due. The capacity test above gates the
        # COMPARISON but not the CLOCK, and the queue is designed to hold work
        # no lane can attend yet, so that clock runs for hours while the fleet
        # is legitimately full. The moment occupancy drops -- the instant
        # between one attempt committing its result and the next claim, a lane
        # restarting under a self-deploy tick (every 5 minutes), a drain
        # finishing -- the same hours-old number is compared against 900s and
        # the probe reports a stall that was never true. The timer samples
        # every two minutes, so this is not a race that might happen: it is
        # the `queue_stalled` this fleet mints over and over at an attempt
        # boundary, against lanes that are doing exactly their job.
        #
        # An item is only being ignored while there is somebody to ignore it,
        # so the wait is bounded by how long THE FLEET has been standing
        # still. A fleet that took or handed back an item a second ago is
        # serving this queue, and `queue_claim` hands out the oldest due row
        # first, so the item being timed is next in line -- it is behind
        # capacity, not behind a broken lane. A fleet that has not moved at
        # all for the whole stall window has no such answer, and the clock
        # runs in full.
        #
        # WHAT THIS GIVES UP: a lane claiming a steady stream of work that
        # OUTRANKS the waiting item (`queue_claim` orders by priority first,
        # and `_review_enqueue` uses 100 against `backlog_dispatch`'s 0) keeps
        # the fleet clock fresh while the low-priority item waits. That is
        # priority starvation -- the queue serving its own policy -- and it
        # wants its own failure code, not a `queue_stalled` task that sends
        # the fleet looking for a stopped lane.
        ready_due_starved = queue.ready_due_age_seconds
        if ready_due_starved is not None and queue.fleet_idle_seconds is not None:
            ready_due_starved = min(ready_due_starved, queue.fleet_idle_seconds)
        # STARVED = pending work nobody is accountable for AND nobody is
        # merely too busy for. A lapsed claim is starved at any capacity: no
        # lane is holding it, so no lane being free is irrelevant to it -- and
        # for the same reason the fleet clock does not bound it either. A
        # neighbouring lane claiming away beside a zombie says nothing about
        # the zombie; only the reaper does, and the lapse age is what measures
        # whether `aicc-queue-reaper.timer` (every minute) is still recovering.
        starved_ages = [queue.lapsed_claim_age_seconds]
        if spare_capacity:
            starved_ages.append(ready_due_starved)
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
    parser.add_argument(
        "--queue",
        default=DEFAULT_QUEUE,
        help=(
            "Which queue to measure. Defaults to the one the worker lanes "
            "claim from. Every other threshold here describes THAT queue's "
            "fleet, so a second queue needs a second probe (its own "
            "--claim-capacity and its own --record-findings source) rather "
            "than this one widened."
        ),
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
        # Read at parse time, not import time, so a unit's `Environment=` and a
        # test's monkeypatched environment are both seen.
        default=os.environ.get(FINDING_SOURCE_ENV, ""),
        help=(
            f"Defaults to ${FINDING_SOURCE_ENV}. "
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
        queue = None if args.skip_queue else read_queue_snapshot(args.queue)
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
