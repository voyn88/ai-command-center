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
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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
_PROMETHEUS_ERRORS = (OSError, ValueError, http.client.HTTPException)


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
class PrWindowSnapshot:
    """What the PR review-window reconciler has and has not labelled.

    The window-gated workflows (CI, the Acceptance gate, boundary fitness)
    run on a pull request only while it carries a `review-window:` label, and
    the only thing that puts one there is the `backlog-pr-window` tick. So a
    tick that is not deployed is not a quiet degradation: every fleet PR opens
    with no CI at all and nothing says so. That is what happened -- the tick
    had never been installed on the control host, and 24 PRs plus #907 sat
    checkless until an operator labelled them by hand on 2026-09-09
    (VOYN-W0-AICC-PR-WINDOW-RECONCILER-NOT-DEPLOYED-ON-CONTROL). This probe is
    the alarm that was missing: it watches the reconciler's OUTPUT on GitHub,
    not its unit, so it is red whether the tick is absent, disabled, failing,
    or out of GitHub quota.
    """

    #: (number, age_seconds) per open PR that carries fleet `pr` evidence and
    #: none of the reconciler's window labels, older than the grace period.
    unlabelled: tuple[tuple[int, int], ...]
    #: Open PRs listed, and how many of those carry fleet `pr` evidence.
    open_prs: int
    evidence_prs: int
    grace_seconds: float
    #: Set when the probe could not measure (gh, the database, or a parse).
    #: A probe that cannot see is not a probe that saw nothing.
    error: str | None = None


@dataclass(frozen=True, slots=True)
class SourceCloneSnapshot:
    """Whether the source clone used by isolated reviews tracks origin.

    Read-only review lanes clone from the local bound source, so a local clone
    that stopped advancing is a silent review-quality outage even when workers
    and queues look healthy.  This probe compares the local checkout's HEAD to
    the remote default HEAD without updating refs, so it is safe for the
    fail-closed monitor's read-only systemd envelope.
    """

    path: str
    local_head: str | None
    remote_head: str | None
    stale: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class UnitState:
    restarts: int
    active_state: str
    #: ``systemctl show --property=LoadState``: whether this host still has a
    #: unit file for the name at all.  ``not-found`` means it does not -- see
    #: ``RETIRED_LOAD_STATE`` and ``evaluate_unit_health``.  Empty when the
    #: property was not asked for, which is never a retirement.
    load_state: str = ""


@dataclass(frozen=True, slots=True)
class UnitHealthSnapshot:
    """Every service unit on the host, judged by restarts and failed state.

    worker-01 2026-09-02..14: `ollama.service` restarted every 3 s for twelve
    days (315 891 restarts) and `voyn-canary.service` every 15 s for eighteen
    (70 305) while every monitor stayed green, because each probe asked
    `is-active` of its own hand-picked units and a `Restart=always` unit is
    `activating` again by the time anyone looks. NRestarts is the number that
    cannot be hidden. Template instances (per-connection launcher units)
    collapse to one `template@*.service` in both listings -- crash loops carry
    the highest NRestarts among the instances, failed units their count -- so
    a hundred dead connections are one finding, not a hundred.

    A unit this host no longer HAS is reported separately (`retired_units`)
    and is not a finding: see `evaluate_unit_health`.
    """

    crash_loops: tuple[tuple[str, int], ...]
    failed_units: tuple[tuple[str, int], ...]
    #: Units whose last run latched `failed` and whose unit file this host no
    #: longer has -- the only reason PID 1 still holds them. Measured and
    #: reported, deliberately not a failure.
    retired_units: tuple[tuple[str, int], ...] = ()
    error: str | None = None


#: `systemctl show --property=LoadState` for a name systemd has no unit file
#: for. Such a unit is kept in PID 1's table only to remember that its last
#: run failed; see `evaluate_unit_health`.
RETIRED_LOAD_STATE = "not-found"

#: How many units a finding's detail names before it says `+N more`: the
#: identity is the class, the listing is for the reader, and an unbounded
#: listing of instance names is the thing the template collapse exists to stop.
UNIT_LISTING_CAP = 20


@dataclass(frozen=True, slots=True)
class DeployLagSnapshot:
    """What production reports versus the branch head it should be running.

    worker-01 2026-09-13 14:48 .. 09-14 18:49: six merged PRs sat behind a
    refused promotion for 28 hours while test and preprod advanced. A green
    branch says nothing about what customers run; only comparing the two
    SHAs does. The clock is the age of the OLDEST undeployed commit as the
    BRANCH gained it -- not the head's age, which every new merge resets, so
    a busy branch could stay a day behind and never report, and not the
    commit object's own date either (worker-01 2026-10-29, monitor_finding
    #38959: a feature branch written days earlier and merged minutes ago
    landed commits whose committer dates were already past the grace period,
    so the probe opened `deploy_lag` the instant any such PR merged and
    stayed red until production matched the head exactly -- the promotion
    pipeline was healthy throughout). What the branch gained, and when, is
    its first-parent chain: see `_first_parent_chain_times`. That oldest
    undeployed commit is allowed a grace period (CI plus the merge queue
    plus the promotion tick) before its absence is a finding -- a budget
    that can only start once the code is ON the branch. A red branch older
    than the grace period is a finding too, deliberately: a branch that
    stays red that long is a delivery outage.
    """

    repo: str
    branch: str
    branch_head: str | None
    deployed_sha: str | None
    undeployed_commits: int | None
    lag_seconds: float | None
    grace_seconds: float
    error: str | None = None

    @property
    def lagging(self) -> bool:
        if self.error is not None or self.branch_head is None or self.deployed_sha is None:
            return False
        if not self.undeployed_commits:
            return False
        return (self.lag_seconds or 0.0) > self.grace_seconds


@dataclass(frozen=True, slots=True)
class MonitorReport:
    ok: bool
    active_workers: int
    discovered_workers: int
    queue: QueueSnapshot | None
    prometheus_ready: bool
    failures: tuple[str, ...]
    pr_window: PrWindowSnapshot | None = None
    source_clone: SourceCloneSnapshot | None = None
    unit_health: UnitHealthSnapshot | None = None
    deploy_lag: DeployLagSnapshot | None = None


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


def window_label_names() -> frozenset[str]:
    """The labels the reconciler owns, taken from the reconciler itself.

    Imported rather than restated: a monitor holding its own copy of these
    strings goes green the moment the labels are renamed, which is exactly
    when it is needed most.
    """
    from command_center.orchestrator.review_merge import PrWindowConfig

    cfg = PrWindowConfig()
    return frozenset({cfg.label_active, cfg.label_waiting, cfg.label_blocked})


def pr_identity(url: str) -> str | None:
    """`owner/repo/pull/number` for a PR url, or None.

    Both sides of the comparison -- the `pr` evidence a task recorded and the
    url `gh pr list` reports -- are reduced to this, so a scheme, host-case or
    trailing-slash difference between them cannot silently empty the
    intersection and report a healthy fleet with nothing in view.
    """
    parts = [part for part in str(url).strip().lower().split("/") if part]
    if len(parts) < 4 or parts[-2] != "pull" or not parts[-1].isdigit():
        return None
    return "/".join(parts[-4:])


def unlabelled_evidence_prs(
    prs: list[dict[str, Any]],
    evidence: frozenset[str],
    *,
    now: float,
    labels: frozenset[str],
    grace_seconds: float,
) -> tuple[tuple[int, int], ...]:
    """Open fleet PRs the reconciler has left without a window label.

    Scoped to PRs that carry `pr` evidence: those are the ones the fleet
    opened and the ones whose gated workflows the fleet is waiting on. An
    unrelated PR on the same repo is somebody else's business.

    `grace_seconds` is measured from `createdAt`, not from the last label
    write -- there is no "when should this have been labelled" timestamp to
    read, and a PR that has been open for longer than several tick intervals
    with no label at all is the symptom either way. A PR younger than the
    grace period is simply not yet evidence of anything.
    """
    found: list[tuple[int, int]] = []
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        identity = pr_identity(pr.get("url") or "")
        if identity is None or identity not in evidence:
            continue
        carried = {
            str(label.get("name") or "")
            for label in (pr.get("labels") or [])
            if isinstance(label, dict)
        }
        if carried & labels:
            continue
        created = _parse_iso8601(str(pr.get("createdAt") or ""))
        if created is None:
            # An unparseable timestamp is not a licence to accuse the
            # reconciler; the PR is reported by the next tick that can read it.
            continue
        age = now - created
        if age > grace_seconds:
            found.append((int(pr.get("number") or 0), int(age)))
    return tuple(sorted(found))


def _parse_iso8601(value: str) -> float | None:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # An offsetless timestamp is UTC, not the monitor host's local time: a
    # naive `.timestamp()` would make the same PR's age differ by the TZ
    # offset from host to host, and near the grace boundary that is the
    # difference between a finding and silence.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _open_prs(repo_path: str, scan_limit: int) -> list[dict[str, Any]]:
    """One `gh pr list` call, oldest-created first.

    Deliberately one request with no per-PR detail lookups: this shares a
    GitHub GraphQL quota with the review, merge and window ticks themselves,
    and a monitor that exhausts the quota manufactures the outage it is
    watching for (VOYN-W0-AICC-GH-GRAPHQL-QUOTA-EXHAUSTED-BY-TICKS). Ascending
    creation order means a repo with more open PRs than `scan_limit` keeps the
    OLDEST -- the ones a missing label has hurt longest -- in view.
    """
    completed = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--search",
            "sort:created-asc",
            "--limit",
            str(max(scan_limit, 1)),
            "--json",
            "number,url,createdAt,labels",
        ],
        cwd=repo_path,
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"gh pr list failed: {(completed.stderr or '').strip()[:120]}"
        )
    listed = json.loads(completed.stdout or "[]")
    if not isinstance(listed, list):
        raise RuntimeError("gh pr list returned no array")
    return listed


def read_pr_evidence() -> frozenset[str]:
    """Every PR the backlog has recorded as `pr` evidence, as identities."""
    from command_center.db import pool
    from command_center.db.config import load_config

    pool.open_pool(load_config())
    try:
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT DISTINCT value FROM backlog_evidence WHERE kind = 'pr'")
            rows = cur.fetchall()
    finally:
        pool.close_pool()
    return frozenset(
        identity
        for identity in (pr_identity(row[0]) for row in rows)
        if identity is not None
    )


def read_pr_window_snapshot(
    repo_path: str, *, grace_seconds: float, scan_limit: int, now: float | None = None
) -> PrWindowSnapshot:
    """Measure the reconciler by its effect on GitHub, never raising.

    A probe failure travels in `error` rather than out of this function: it
    is a finding of its own class, and losing the queue and worker
    measurements of the same tick to it would be trading one blind spot for
    a bigger one.
    """
    try:
        labels = window_label_names()
        prs = _open_prs(repo_path, scan_limit)
        evidence = read_pr_evidence()
    except Exception as exc:  # noqa: BLE001 - see the docstring
        return PrWindowSnapshot(
            unlabelled=(),
            open_prs=0,
            evidence_prs=0,
            grace_seconds=grace_seconds,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )
    listed = [pr for pr in prs if isinstance(pr, dict)]
    fleet = [
        pr for pr in listed if (pr_identity(pr.get("url") or "") or "") in evidence
    ]
    return PrWindowSnapshot(
        unlabelled=unlabelled_evidence_prs(
            listed,
            evidence,
            now=time.time() if now is None else now,
            labels=labels,
            grace_seconds=grace_seconds,
        ),
        open_prs=len(listed),
        evidence_prs=len(fleet),
        grace_seconds=grace_seconds,
    )


def _git_stdout(repo_path: str, args: list[str], *, timeout: int = 30) -> str:
    completed = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        check=False,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            (completed.stderr or completed.stdout or "git failed").strip()[:160]
        )
    return completed.stdout.strip()


def read_source_clone_snapshot(repo_path: str) -> SourceCloneSnapshot:
    """Measure source-clone freshness, never raising."""
    try:
        local_head = _git_stdout(repo_path, ["rev-parse", "HEAD"])
        remote_output = _git_stdout(
            repo_path, ["ls-remote", "origin", "HEAD"], timeout=60
        )
        remote_head = remote_output.split(None, 1)[0] if remote_output else ""
        if not remote_head:
            raise RuntimeError("origin HEAD was empty")
        return SourceCloneSnapshot(
            path=repo_path,
            local_head=local_head,
            remote_head=remote_head,
            stale=local_head != remote_head,
        )
    except Exception as exc:  # noqa: BLE001 - see read_pr_window_snapshot
        return SourceCloneSnapshot(
            path=repo_path,
            local_head=None,
            remote_head=None,
            stale=True,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )


def parse_unit_show(output: str) -> dict[str, UnitState]:
    """Parse blank-line separated `systemctl show` blocks into unit states."""
    units: dict[str, UnitState] = {}
    for block in output.strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip()
        unit = fields.get("Id")
        if not unit:
            continue
        try:
            restarts = int(fields.get("NRestarts", "0") or 0)
        except ValueError:
            restarts = 0
        units[unit] = UnitState(
            restarts=restarts,
            active_state=fields.get("ActiveState", ""),
            load_state=fields.get("LoadState", ""),
        )
    return units


def parse_service_names(output: str) -> list[str]:
    """Service unit names from `systemctl list-units` output.

    Tolerates the header, the legend footer and the `*`/`●` state marker a
    non-`--plain` systemd prefixes to failed rows: the row is kept by the
    first token that ends in `.service`, never by column position, so an
    older systemd cannot make the probe skip exactly the units it exists to
    find.
    """
    names: set[str] = set()
    for line in output.splitlines():
        tokens = line.split()
        for token in tokens[:2]:
            if token.endswith(".service"):
                names.add(token)
                break
    return sorted(names)


def _template_name(unit: str) -> str:
    """`aicc-agent-launcher@2079-27636-984.service` -> `aicc-agent-launcher@*.service`."""
    name, at, rest = unit.partition("@")
    if not at:
        return unit
    suffix = rest[rest.rfind(".") :] if "." in rest else ""
    return f"{name}@*{suffix}"


def evaluate_unit_health(
    units: dict[str, UnitState], *, crash_loop_restarts: int
) -> UnitHealthSnapshot:
    """Split the host's units into faults and residue.

    systemd's `failed` is a LATCH, not a measurement: it records how a unit's
    last run ended and it survives the condition indefinitely. For a unit this
    host still has, that latch is actionable -- the next run clears it, and a
    unit that keeps failing keeps it set, which is the fault this probe exists
    to see. For a unit this host no longer HAS it is residue: there is no unit
    file, so it can never run again, can never reach a successful state, and
    only a privileged `systemctl reset-failed` will ever clear it. Nothing to
    restart, nothing to repair, nothing measured about the present.

    Those two cases are indistinguishable in `ActiveState` alone, which is why
    this probe asks for `LoadState` as well. They are not hypothetical: the
    staged isolated-lane rollout retires `voyn-aicc-worker.service`,
    `voyn-aicc-worker-2.service` and `aicc-worker.service` by stopping and
    *disabling* them, and disabling removes the symlink that was their
    fragment -- so after a rollout those units are deliberately gone
    (`RETIRED_LEGACY_UNITS` in ops/aicc_install_transaction.py says so in as
    many words). A stop that reached its timeout latched `failed` on the way
    out, and `verify_legacy_units_retired` accepted that as drained. systemd
    garbage-collects a fragment-less unit the moment it is inactive and
    unreferenced, so the ONLY retired units still in PID 1's table are exactly
    the ones latched `failed` -- and this probe counted them from the day it
    was added (worker-01, monitor_finding 2051 and its three successors). No
    repair could clear that finding, because the units it named no longer
    existed on the host.

    Residue is not silenced, it is reclassified: `retired_units` carries it
    into the journalled measurement so an operator can still see what is
    there. Nor does this blind the probe. A unit file that goes missing while
    the service matters is not quiet -- the lanes are `active_workers`, the
    monitor's other classes cover the ticks -- and a unit file that is present
    but broken loads as `error`/`bad-setting`, not `not-found`, so it stays a
    fault here.

    NRestarts is residue on the same unit for the same reason (a frozen
    counter on something that cannot restart), and is excluded with it: were
    it not, clearing the `failed_units` finding would merely re-raise the same
    corpses as `crash_loop`.
    """
    if crash_loop_restarts < 1:
        raise ValueError("crash_loop_restarts must be at least 1")
    loops: dict[str, int] = {}
    failed: dict[str, int] = {}
    retired: dict[str, int] = {}
    for unit, state in units.items():
        key = _template_name(unit)
        if state.load_state == RETIRED_LOAD_STATE:
            # Only the latched ones are residue worth reporting. A
            # fragment-less unit that is merely inactive is a dangling
            # dependency reference -- `Wants=prometheus.service` on a host
            # without Prometheus, `After=postgresql@16-main.service` on a
            # host whose database is elsewhere -- which is not a unit at all
            # and has never been a finding. Counted like failed_units (one
            # entry per template, instances summed), because the launcher's
            # per-connection corpses reach this branch too once the template
            # file is removed.
            if state.active_state == "failed":
                retired[key] = retired.get(key, 0) + 1
            continue
        if state.restarts >= crash_loop_restarts:
            loops[key] = max(loops.get(key, 0), state.restarts)
        if state.active_state == "failed":
            failed[key] = failed.get(key, 0) + 1
    return UnitHealthSnapshot(
        crash_loops=tuple(sorted(loops.items())),
        failed_units=tuple(sorted(failed.items())),
        retired_units=tuple(sorted(retired.items())),
    )


def _unit_listing(entries: tuple[tuple[str, int], ...]) -> str:
    shown = ",".join(f"{unit}={count}" for unit, count in entries[:UNIT_LISTING_CAP])
    hidden = len(entries) - UNIT_LISTING_CAP
    return f"{shown},+{hidden}_more" if hidden > 0 else shown


def read_unit_health_snapshot(*, crash_loop_restarts: int) -> UnitHealthSnapshot:
    """Two unprivileged systemctl calls for the whole host, never raising."""
    try:
        listed = subprocess.run(
            [
                "systemctl",
                "list-units",
                "--type=service",
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
        names = parse_service_names(listed.stdout)
        if not names:
            raise RuntimeError("systemctl listed no service units")
        shown = subprocess.run(
            [
                "systemctl",
                "show",
                # LoadState is not decoration: without it every unit's
                # `load_state` is empty, the residue branch in
                # `evaluate_unit_health` never fires, and the retired corpses
                # are counted as faults again. A unit test asserts this argv.
                "--property=Id,LoadState,NRestarts,ActiveState",
                "--no-pager",
                "--",
                *names,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        units = parse_unit_show(shown.stdout)
        # A unit that `show` silently dropped is a unit the probe did not
        # judge; a partial answer is a failed measurement, not a clean host.
        if set(units) != set(names):
            raise RuntimeError(
                f"systemctl show returned {len(units)} of {len(names)} units"
            )
        return evaluate_unit_health(units, crash_loop_restarts=crash_loop_restarts)
    except Exception as exc:  # noqa: BLE001 - see read_pr_window_snapshot
        return UnitHealthSnapshot(
            crash_loops=(),
            failed_units=(),
            error=f"{type(exc).__name__}: {exc}"[:200],
        )


def _fetch_json(url: str, timeout: int = 5) -> dict[str, Any]:
    """GET a small loopback/LAN JSON document with the same URL hygiene as
    the Prometheus probe (no credentials in the URL, http(s) only)."""
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RuntimeError("version url must be a plain http(s) url")
    client = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    connection = client(parsed.hostname, parsed.port, timeout=timeout)
    try:
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        connection.request("GET", target)
        response = connection.getresponse()
        body = response.read(64 * 1024).decode("utf-8", errors="replace")
        if response.status != 200:
            raise RuntimeError(f"version url returned {response.status}")
        document = json.loads(body)
    finally:
        connection.close()
    if not isinstance(document, dict):
        raise TypeError("version url returned no object")
    return document


def _gh_api_json(path: str) -> Any:
    completed = subprocess.run(
        ["gh", "api", path],
        capture_output=True,
        check=False,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"gh api failed: {(completed.stderr or '').strip()[:120]}")
    return json.loads(completed.stdout or "{}")


def _committer_time(commit: Any) -> float | None:
    if not isinstance(commit, dict):
        return None
    inner = commit.get("commit")
    if not isinstance(inner, dict):
        return None
    committer = inner.get("committer")
    if not isinstance(committer, dict):
        return None
    return _parse_iso8601(str(committer.get("date") or ""))


def _first_parent_chain_times(commits: list[Any], head: str) -> list[float]:
    """Committer dates of the undeployed commits that LANDED on the branch:
    its first-parent chain, walked from the head down to the first parent
    outside the compared range.

    A commit object's committer date is when someone wrote it, which is when
    the branch gained it only for commits pushed, squashed or rebased onto
    it. A merge commit carries its whole feature branch at the branch's own
    ages, so dating the range by the oldest commit OBJECT in it charges the
    promotion pipeline for everything that happened before the code was
    mergeable at all. The first-parent chain is exactly what the branch
    itself gained, each entry dated when it was gained (GitHub stamps the
    merge/squash/rebase commit at merge time), so the oldest date on it is
    when the branch first held something production does not run.

    Returns [] when the chain cannot be walked end to end -- a compare page
    truncated at its 250-commit cap (which keeps the OLDEST commits, so the
    head is not in the payload at all), a payload without `parents`, a
    cycle, or a commit without a date -- so the caller falls back to dating
    the commit objects rather than trusting a partial walk.
    """
    by_sha: dict[str, dict[str, Any]] = {}
    for commit in commits:
        if isinstance(commit, dict):
            sha = str(commit.get("sha") or "")
            if sha:
                by_sha[sha] = commit
    times: list[float] = []
    sha, seen = head, set()
    while sha in by_sha and sha not in seen:
        seen.add(sha)
        commit = by_sha[sha]
        when = _committer_time(commit)
        if when is None:
            return []
        times.append(when)
        parents = commit.get("parents")
        first = parents[0] if isinstance(parents, list) and parents else None
        sha = str(first.get("sha") or "") if isinstance(first, dict) else ""
    # A complete walk leaves the range through a real first parent (the merge
    # base side). Anything else -- an empty `parents`, a cycle, a head the
    # truncated page never carried -- is not a chain to date the lag from.
    if not times or not sha or sha in by_sha:
        return []
    return times


def read_deploy_lag_snapshot(
    repo: str, version_url: str, *, branch: str, grace_seconds: float
) -> DeployLagSnapshot:
    """One GET of what runs, one `gh api` for the branch head, and -- only
    when they differ -- one `gh api` compare to count the undeployed commits
    and date the oldest one by when the branch gained it. Never raises.

    `gh` authenticates from the unit's GH_CONFIG_DIR (the fleet's GitHub App
    store on the worker), never from a token in arguments or logs.
    """
    try:
        deployed = _fetch_json(version_url)
        deployed_sha = str(deployed.get("release_sha") or "").strip()
        if not deployed_sha:
            raise RuntimeError("version document carries no release_sha")
        commit = _gh_api_json(f"repos/{repo}/commits/{branch}")
        head = str(commit.get("sha") or "").strip() if isinstance(commit, dict) else ""
        if not head:
            raise RuntimeError("gh api returned no branch head sha")
        if deployed_sha == head:
            return DeployLagSnapshot(
                repo=repo,
                branch=branch,
                branch_head=head,
                deployed_sha=deployed_sha,
                undeployed_commits=0,
                lag_seconds=0.0,
                grace_seconds=grace_seconds,
            )
        compared = _gh_api_json(f"repos/{repo}/compare/{deployed_sha}...{head}")
        if not isinstance(compared, dict):
            raise TypeError("gh api compare returned no object")
        status = str(compared.get("status") or "")
        commits = compared.get("commits")
        if not isinstance(commits, list):
            raise TypeError("gh api compare returned no commit list")
        if status in {"identical", "behind"}:
            # Production runs the head or something newer than it (a rolled
            # forward hotfix, a branch swap): nothing is waiting to deploy.
            undeployed, lag = 0, 0.0
        else:
            # The clock is when the BRANCH gained its oldest undeployed
            # commit, not when that commit was written; the commit objects
            # are the fallback for a range this probe cannot walk.
            times = _first_parent_chain_times(commits, head) or [
                t for t in (_committer_time(c) for c in commits) if t is not None
            ]
            if not times:
                raise RuntimeError("gh api compare listed commits without dates")
            total = compared.get("total_commits")
            # `total_commits` counts the whole range; `commits` stops at one
            # page, so a long stall must not report a smaller backlog.
            undeployed = (
                int(total)
                if isinstance(total, int) and not isinstance(total, bool) and total > 0
                else len([c for c in commits if isinstance(c, dict)])
            )
            lag = max(time.time() - min(times), 0.0)
        return DeployLagSnapshot(
            repo=repo,
            branch=branch,
            branch_head=head,
            deployed_sha=deployed_sha,
            undeployed_commits=undeployed,
            lag_seconds=lag,
            grace_seconds=grace_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - see read_pr_window_snapshot
        return DeployLagSnapshot(
            repo=repo,
            branch=branch,
            branch_head=None,
            deployed_sha=None,
            undeployed_commits=None,
            lag_seconds=None,
            grace_seconds=grace_seconds,
            error=f"{type(exc).__name__}: {exc}"[:200],
        )


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
    except _PROMETHEUS_ERRORS:
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
    pr_window: PrWindowSnapshot | None = None,
    source_clone: SourceCloneSnapshot | None = None,
    unit_health: UnitHealthSnapshot | None = None,
    deploy_lag: DeployLagSnapshot | None = None,
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
        # Quota/spend refusals are counted below under their own class; the
        # same death must not also raise dead_letter_growth (live 2026-09-15:
        # every hour with a Codex-quota preflight death opened a growth
        # finding on control-01 -- one cause, two tasks).
        unexplained_dead = max(queue.recent_dead - queue.recent_quota_dead, 0)
        if unexplained_dead > max_recent_dead:
            failures.append(f"dead_letter_growth:{unexplained_dead}>{max_recent_dead}")
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
        #
        # WHAT MAY CORROBORATE IT IS THE UNOFFERED CLOCK, AND ONLY THAT ONE
        # (monitor_finding #14016). The floor is the whole reason this check is
        # allowed to fire inside the window, and it is derived from ONE of the
        # two starvation clocks: `CLAIM_POLL_CEILING_SECONDS` is how long a
        # free lane may take to notice due work (`WorkerConfig`'s saturated
        # idle backoff, 30-60s), so past it every free lane has polled at
        # least once and work still sitting there really was passed over.
        #
        # It says NOTHING about a lapsed claim, and applying it to one made
        # this probe red at a healthy fleet. No claimer clears a lapse --
        # `queue_claim` cannot even see the row, it is still `claimed` -- only
        # `aicc-queue-reaper.timer` does, and the only threshold in this
        # function that describes the reaper is `--max-stalled-seconds`, which
        # is exactly why `lapsed_claim_age_seconds` is weighed against it
        # above, unconditionally and unbounded by the fleet clock. Below that
        # window a lapse is the reaper WORKING, not the pipeline stalling: the
        # timer is `OnUnitActiveSec=1min` with `AccuracySec=15s`, a failed tick
        # is "a failed tick, not an incident" by that unit's own comment, and
        # 0040/0041 defer a contended or freshly renewed row to the next tick
        # on purpose. Two such minutes are ordinary, and 120s is past a floor
        # built for 60s.
        #
        # MEASURED, at the deployed numbers, on the state this branch spent
        # three commits teaching the probe to read as healthy -- two lanes 70
        # minutes into legitimate attempts (`voyn-aicc-worker@.service` allows
        # one 3660s and `--max-claim-seconds` 5400s), the two surplus
        # dispatched items due behind them, and lane B's lease 120 seconds
        # past its deadline while lane B is still running:
        #
        #     claimed=2 attended=1 lapse_age=120 due_age=7200 fleet_idle=4200
        #         -> throughput_stalled:0_succeeded_in_1h
        #
        # Every clause of that is the fleet doing its job. `recent_succeeded`
        # is 0 because both attempts are longer than an hour, which this
        # function's own comment calls ordinary; occupancy is full, so the
        # due-ready clock is correctly excused (#13366) and the lapse is all
        # that is left to corroborate with; and the lapse clears on the
        # reaper's next minute. `queue_stalled` stays green throughout --
        # which is the point: the finding this probe was sent to fix is
        # healthy, and the probe still could not report `ok`.
        #
        # WHAT THIS GIVES UP is detection LATENCY on one shape and no
        # coverage: a fleet that is wedged at full occupancy with nothing
        # succeeding is now reported when its lapse passes the stall window
        # rather than the poll ceiling. That is the latency
        # `--max-stalled-seconds` exists to declare, it is the same latency
        # every other starvation class here has, and the clock that reports it
        # is the unconditional one. The case this check was written for is
        # untouched: lanes spinning on refusals fail fast, `queue_fail`
        # requeues, occupancy drops between spins and the requeued items come
        # back as due-ready work no lane took -- the unoffered clock exactly.
        unoffered_age = ready_due_starved if spare_capacity else None
        if (
            queue.recent_succeeded is not None
            and queue.recent_succeeded == 0
            and unoffered_age is not None
            and CLAIM_POLL_CEILING_SECONDS < unoffered_age
            # Still mutually exclusive with `queue_stalled`, and still keyed on
            # the WHOLE starved age rather than on the unoffered one: a lapse
            # past the window has already minted that failure, and one
            # condition must not mint two findings and two planner tasks.
            and starved_age is not None
            and starved_age <= max_stalled_seconds
        ):
            failures.append("throughput_stalled:0_succeeded_in_1h")

    if pr_window is not None:
        if pr_window.error is not None:
            failures.append(f"pr_window_probe_failed:{pr_window.error}")
        elif pr_window.unlabelled:
            # One finding for the reconciler, not one per PR: the identity is
            # the code before the first ':' (`finding_key`), and the PR
            # numbers ride in the detail. The oldest first, because that age
            # is how long the fleet has been opening PRs that get no CI.
            oldest = max(age for _number, age in pr_window.unlabelled)
            numbers = ",".join(str(number) for number, _age in pr_window.unlabelled)
            failures.append(
                f"pr_window_unlabelled:{len(pr_window.unlabelled)}_prs"
                f"_oldest_{oldest}s>{int(pr_window.grace_seconds)}s:{numbers}"
            )

    if source_clone is not None:
        if source_clone.error is not None:
            failures.append(f"source_clone_probe_failed:{source_clone.error}")
        elif source_clone.stale:
            local = (source_clone.local_head or "unknown")[:12]
            remote = (source_clone.remote_head or "unknown")[:12]
            failures.append(f"source_clone_stale:{local}!={remote}")

    if unit_health is not None:
        if unit_health.error is not None:
            failures.append(f"unit_health_probe_failed:{unit_health.error}")
        else:
            # One finding per class, every unit in the detail: the identity
            # is the code before the first ':' (`finding_key`).
            if unit_health.crash_loops:
                failures.append(
                    f"crash_loop:{len(unit_health.crash_loops)}"
                    f":{_unit_listing(unit_health.crash_loops)}"
                )
            if unit_health.failed_units:
                failures.append(
                    f"failed_units:{len(unit_health.failed_units)}"
                    f":{_unit_listing(unit_health.failed_units)}"
                )

    if deploy_lag is not None:
        if deploy_lag.error is not None:
            failures.append(f"deploy_lag_probe_failed:{deploy_lag.error}")
        elif deploy_lag.lagging:
            failures.append(
                f"deploy_lag:{deploy_lag.repo}:{(deploy_lag.deployed_sha or '')[:8]}"
                f"!={(deploy_lag.branch_head or '')[:8]}"
                f"_{deploy_lag.undeployed_commits}_commits"
                f"_{int(deploy_lag.lag_seconds or 0)}s>{int(deploy_lag.grace_seconds)}s"
            )

    return MonitorReport(
        ok=not failures,
        active_workers=active_workers,
        discovered_workers=len(worker_states),
        queue=queue,
        prometheus_ready=prometheus_ready,
        failures=tuple(failures),
        pr_window=pr_window,
        source_clone=source_clone,
        unit_health=unit_health,
        deploy_lag=deploy_lag,
    )


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse and cross-validate: a half-configured deploy-lag probe is a
    usage error argparse reports, not a monitoring failure that pages."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if bool(args.deploy_lag_repo) != bool(args.deploy_lag_version_url):
        parser.error("--deploy-lag-repo and --deploy-lag-version-url must be given together")
    return args


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
        "--pr-window-repo",
        default=os.environ.get("AICC_PR_WINDOW_REPO", ""),
        metavar="PATH",
        help=(
            "Directory to run one `gh pr list` in, to check that the PR "
            "review-window reconciler is actually labelling open fleet PRs. "
            "Empty (the default) skips the probe, so a host without gh or "
            "without the backlog database is unaffected. Defaults to "
            "$AICC_PR_WINDOW_REPO, which is how the control unit turns it on."
        ),
    )
    parser.add_argument(
        "--pr-window-grace-seconds",
        type=float,
        default=900,
        help=(
            "How long an open PR carrying `pr` evidence may go without a "
            "window label before it is a finding. Default 15 minutes: three "
            "intervals of the five-minute tick."
        ),
    )
    parser.add_argument(
        "--pr-window-scan-limit",
        type=int,
        default=200,
        help="Open PRs the single `gh pr list` asks for, oldest-created first.",
    )
    parser.add_argument(
        "--source-clone-repo",
        default=os.environ.get("AICC_SOURCE_CLONE_REPO", ""),
        metavar="PATH",
        help=(
            "Local source clone used by isolated read-only reviews. When set, "
            "the monitor compares its HEAD to origin/HEAD via git ls-remote "
            "and fails if the clone is stale or cannot be measured."
        ),
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
    parser.add_argument(
        "--unit-health",
        action="store_true",
        help="Read every service unit on the host: NRestarts crash loops and failed units.",
    )
    parser.add_argument(
        "--crash-loop-restarts",
        type=_positive_int,
        default=5,
        help="NRestarts at or above which a unit is a crash loop (default 5, minimum 1).",
    )
    parser.add_argument(
        "--deploy-lag-repo",
        default=None,
        help="GitHub owner/name whose branch head production must run (off by default).",
    )
    parser.add_argument(
        "--deploy-lag-version-url",
        default=None,
        help="URL of the deployed service's /version document carrying release_sha.",
    )
    parser.add_argument("--deploy-lag-branch", default="main")
    parser.add_argument(
        "--deploy-lag-grace-seconds",
        type=float,
        default=2700.0,
        help="How old the branch head may be before its absence from production is a finding.",
    )
    return parser


def finding_key(failure: str) -> str:
    """The stable identity of a failure: its code before the first ':'
    (`active_workers:2<4` -> `active_workers`), truncated like the column."""
    return failure.split(":", 1)[0].strip()[:200] or failure[:200]


def record_findings(source: str, failures: tuple[str, ...], detail: dict[str, Any]) -> None:
    """Reconcile this source's open findings with what this tick measured,
    through the SECURITY DEFINER functions of migrations 0021 and 0038
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
    if report.deploy_lag is not None:
        # `asdict` serialises fields only; the verdict is a property.
        payload["deploy_lag"]["lagging"] = report.deploy_lag.lagging
    return payload


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        workers = {} if args.skip_workers else discover_worker_units()
        queue = None if args.skip_queue else read_queue_snapshot(args.queue)
        metrics_ready = prometheus_is_ready(args.prometheus_url)
        pr_window = (
            read_pr_window_snapshot(
                args.pr_window_repo,
                grace_seconds=args.pr_window_grace_seconds,
                scan_limit=args.pr_window_scan_limit,
            )
            if args.pr_window_repo
            else None
        )
        source_clone = (
            read_source_clone_snapshot(args.source_clone_repo)
            if args.source_clone_repo
            else None
        )
        unit_health = (
            read_unit_health_snapshot(crash_loop_restarts=args.crash_loop_restarts)
            if args.unit_health
            else None
        )
        deploy_lag = (
            read_deploy_lag_snapshot(
                args.deploy_lag_repo,
                args.deploy_lag_version_url,
                branch=args.deploy_lag_branch,
                grace_seconds=args.deploy_lag_grace_seconds,
            )
            if args.deploy_lag_repo
            else None
        )
        report = evaluate(
            workers,
            queue,
            minimum_active_workers=args.minimum_active_workers,
            max_stalled_seconds=args.max_stalled_seconds,
            prometheus_ready=metrics_ready,
            max_recent_dead=args.max_recent_dead,
            max_claim_seconds=args.max_claim_seconds,
            claim_capacity=args.claim_capacity,
            pr_window=pr_window,
            source_clone=source_clone,
            unit_health=unit_health,
            deploy_lag=deploy_lag,
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
