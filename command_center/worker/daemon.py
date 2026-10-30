"""The claim-execute-report loop, built to be killed at any moment.

Design decisions, each traceable to the shipped substrate:

* **Identity is the connection.** The claimant is ``session_user`` by trigger,
  so the daemon carries no identity of its own — it is whatever per-host
  ``aicc_w_*`` role its DSN authenticates as. Enrolment is not this daemon's
  job: redeeming a ticket is an ``aicc_app`` privilege, deliberately denied to
  workers, so a worker host is enrolled by the operator or control plane
  before this service first starts.
* **The heartbeat runs beside the handler, not inside it.** A handler that
  blocks must not silence the heartbeat, and a lapsed lease must stop the
  work: the beat thread renews often enough to survive
  ``TOLERATED_FAILED_BEATS`` consecutive failures (see
  ``beat_interval_seconds``) and raises a stop flag the moment the database
  says ``attempt_superseded``.
* **The tolerance is a deadline, not a number of beats.** How many renewals
  may fail is a consequence of the cadence; what decides that the attempt is
  gone is a clock against the window the server granted. Counting beats
  instead made the lapse arrive late by however long each failed beat took to
  admit it — and a beat that cannot reach a restarted
  ``voyn-aicc-pgtunnel.service`` does not fail fast, it black-holes — which
  left a lane executing an item ``aicc-queue-reaper.timer`` had already
  handed back (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED; see
  ``_heartbeat_loop``).
* **And the deadline is watched where the database cannot reach it.** A rule
  is only as good as the thread that evaluates it: the deadline above lived
  at the top of the beat loop, so the black-holed call *also* postponed the
  verdict on itself, and the lapse arrived one unbounded block late. The rule
  now has one definition (``_LeaseDeadline``) and two readers, one of which
  (``_lease_deadline_loop``) touches no store at all and so cannot be delayed
  by any database fault (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED,
  monitor_finding #41237).
* **Cancelling the handler and refusing to report are different decisions.**
  Two leases cover one attempt — this queue lease, and the repository writer
  lease ``worker.handlers`` holds through the run — and either one going must
  stop the handler. Only the QUEUE one makes the outcome unwritable, so
  ``_execute`` keeps them on separate events and discards an outcome for that
  reason alone; a run cancelled by the writer lease is still reported, which
  is what returns the item to the queue instead of leaving it ``claimed``
  under a lease nothing renews (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED).
* **And only the SERVER can say the write is refused.** An unanswered beat is
  not a refused beat: ``store.heartbeat`` is one autocommit statement, so a
  call black-holed on a severed tunnel may have reached the server, run and
  *committed*, with only the reply lost — which leaves the server's lease up
  to one beat interval beyond the deadline this lane can prove. So the
  deadline raises ``lease_unproven`` (stop the work, the fail-closed half) and
  ``queue_heartbeat`` answering ``ok = false`` raises ``queue_lease_lost``
  (discard the outcome, because ``_queue_owns`` has already refused it).
  Discarding on the clock instead left the item ``claimed`` under a lease
  nothing renews and threw away a ``queue_fail`` the database would have
  accepted (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, ``monitor_finding``
  #41318).
* **Shutdown finishes the item in hand.** SIGTERM stops *claiming*; the
  current attempt runs to completion inside systemd's stop timeout. A second
  signal — or the timeout's SIGKILL — abandons it, and the lease expiry plus
  the control plane's reaper make that abandonment safe by construction.
* **Auth failure means stop, not retry.** A refused connection may mean the
  credential was rotated out from under us (`enroll_rotate_self` is
  first-writer-wins); hammering the server with a dead secret is
  indistinguishable from an attack, so the daemon exits non-zero and leaves
  restart pacing to systemd.
* **The watchdog is fed from both threads, because each covers the other's
  blind spot** (SRV-06). The claim loop pings between claims but blocks for
  the whole of a handler's run; the heartbeat thread pings during exactly
  that run but exists only while an item is held. Together every healthy
  state pings within one heartbeat interval, so a wedged process — a handler
  that hangs *and* takes the beat thread with it, a claim loop stuck in a
  driver call — misses two pings and systemd restarts the unit. Restart is
  safe by the same construction as SIGKILL: lease expiry plus the reaper.
"""

from __future__ import annotations

import logging
import random
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from command_center.db.work_queue_store import (
    ClaimedWork,
    QueueRefusal,
    WorkQueueStore,
)
from command_center.worker import sdnotify

logger = logging.getLogger(__name__)

__all__ = [
    "TOLERATED_FAILED_BEATS",
    "Handler",
    "HandlerOutcome",
    "WorkerConfig",
    "WorkerDaemon",
    "beat_interval_seconds",
]

#: How many CONSECUTIVE heartbeats may fail without the lease lapsing.
#:
#: Two, and it is not a preference: it is the number the deployment was
#: already built around, written down in three places that each assumed the
#: code delivered it and none of which measured whether it did. This module's
#: own comment said "two consecutive beats may fail (a restarting PostgreSQL,
#: a network blip) before the lease actually lapses"; `aicc-worker.service`
#: sized `WatchdogSec` as "two heartbeat intervals plus slack"; ADR-0011
#: repeated it. The code delivered ONE
#: (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED) -- see `beat_interval_seconds`,
#: which now derives the cadence from this number instead of restating it.
#:
#: The fleet meets the case on a schedule it runs itself:
#: `voyn-aicc-worker@.service` declares `Requires=voyn-aicc-pgtunnel.service`,
#: and the credential rotation restarts that tunnel on its own timer.
TOLERATED_FAILED_BEATS = 2


def beat_interval_seconds(
    visibility_seconds: float, watchdog_seconds: float | None = None
) -> float:
    """How long to wait between lease renewals, so that
    ``TOLERATED_FAILED_BEATS`` consecutive failures really are survivable.

    THE ARITHMETIC, because it was wrong by exactly one beat and nothing
    measured it. A successful beat at ``T`` sets the lease to ``T + V``
    (``queue_heartbeat`` renews by the attempt's own ``visibility_seconds``,
    from the row -- a beat cannot lengthen its own lease). The beats that
    follow fall at ``T + I``, ``T + 2I``, ...  So the first beat that MUST
    succeed is number ``F + 1``, at ``T + (F + 1) * I``, and the lease
    survives only if that lands while it is still live::

        (F + 1) * I  <  V        the tolerance itself
        (F + 2) * I  <= V        with a whole beat of margin

    ``V / 3`` satisfies NEITHER at ``F = 2``: ``3 * (V/3)`` is ``V`` exactly,
    so the third beat lands ON the deadline and `_queue_owns` refuses it
    (``claim_expired``, which is ``<=``, not ``<``). MEASURED against a real
    PostgreSQL 16 server through the real `queue_claim`/`queue_heartbeat` at
    ``visibility_seconds=12``: one failed beat held the lease, two lost it.
    The comment above that line had promised two for as long as it existed.

    The margin is a whole interval rather than an epsilon because every term
    here drifts in the same direction. ``beat_stop.wait(interval)`` measures
    from when the PREVIOUS call RETURNED, so each cycle costs ``I`` plus a
    round trip plus whatever the scheduler adds, and the error is cumulative;
    the server's clock is what decides; and the penalty for arriving late is
    not a retry but the whole attempt (`daemon._execute` discards the outcome,
    the reaper hands the item to another lane, and `queue_claim` has already
    charged `attempt_count` for the delivery that was taken away).

    ``watchdog_seconds`` only ever SHORTENS the wait, and is the reason this
    returns a bound rather than a formula: a long visibility window would
    otherwise park the beat thread past the systemd watchdog deadline during
    exactly the run the ping is meant to cover. The one-second floor is the
    same clamp `queue_claim` applies to the window it is derived from.

    ``visibility_seconds`` is the window the CALLER ASKED FOR, and the whole
    arithmetic above assumes it is the one the server GRANTED. That holds for
    every window this daemon can be run with: `queue_claim` clamps to
    ``[1, 3600]``, `WorkerConfig.visibility_seconds` is 300, and
    `command_center.worker.__main__` exposes no knob to change it. An operator
    knob above 3600 would re-open exactly the gap this function was written to
    close -- the beat sized to a 7200s window against a lease the server keeps
    at 3600 -- so the window would have to be read back from the claim (the
    granted deadline is on `ClaimedWork.visible_until`) rather than from the
    config, and the caller's number would stop being the subject.
    """
    interval = max(visibility_seconds / (TOLERATED_FAILED_BEATS + 2), 1.0)
    if watchdog_seconds is not None:
        interval = min(interval, max(watchdog_seconds, 1.0))
    return interval


class _LeaseDeadline:
    """How much of THIS attempt's queue lease is left, as one rule in one
    place -- and, deliberately, as a plain clock with no I/O in it, so that
    the fact it states can be read by a thread the database cannot park.

    The rule itself is `daemon._heartbeat_loop`'s and is unchanged: a window
    of ``visibility_seconds`` from the last beat the SERVER accepted, by a
    MONOTONIC clock (the quantity is an elapsed duration, and a wall clock
    stepped by NTP would move the lease with it), dated from when that beat
    ANSWERED rather than from when it was sent (the server grants the renewal
    somewhere inside the round trip, so the answer's arrival is at or after
    the grant -- which makes `remaining` PROVE the lapse instead of estimating
    it, and an attempt taken away with time left on its lease costs the whole
    run; see migration 0029).

    WHY IT IS AN OBJECT RATHER THAN A LOCAL
    (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, monitor_finding #41237). It was
    a `granted_at` local in the beat loop, which made the beat thread the only
    thread that could read it -- and that thread is parked inside
    `store.heartbeat` for exactly the failure the deadline exists for. One
    definition, two readers: the beat loop, which reads it to decide whether
    to send another beat, and `_lease_deadline_loop`, which reads it to decide
    that this lane can no longer prove the attempt is its own, and can never
    be blocked on the database while doing so.

    AND WHAT IT PROVES IS A LAPSE, NOT A REFUSAL (monitor_finding #41318).
    "The last beat the SERVER ACCEPTED" is, from here, "the last beat this
    LANE SAW accepted", and the two differ by exactly the beats whose answers
    were lost: `store.heartbeat` is one autocommit statement, so a call
    black-holed on a severed tunnel may have reached the server, run and
    COMMITTED. So ``remaining <= 0`` means "this lane cannot show the attempt
    is still its own" -- enough to stop the work, and not a verdict on whether
    a report would be accepted. Only `_queue_owns` decides that; see
    `WorkerDaemon._execute`.
    """

    __slots__ = ("_window", "_monotonic", "_lock", "_granted_at")

    def __init__(self, window: float, monotonic: Callable[[], float]) -> None:
        self._window = float(window)
        self._monotonic = monotonic
        # Plain assignment of a float is atomic under the GIL, but the lock is
        # cheap and the invariant -- a reader never sees a half-written grant
        # -- is the one this type exists to hold across threads.
        self._lock = threading.Lock()
        # `queue_claim` has just granted a window, and this is constructed
        # before the attempt's threads start, so construction is a
        # conservative stand-in for that grant: the real one is no later.
        self._granted_at = monotonic()

    def renew(self, answered_at: float | None = None) -> None:
        """Re-date the lease to when a beat the server ACCEPTED answered.

        ``answered_at`` is passed by the beat loop as the instant the call
        returned, so the timestamp is the answer's and not that of whatever
        the loop did next.
        """
        with self._lock:
            self._granted_at = (
                self._monotonic() if answered_at is None else answered_at
            )

    def remaining(self) -> float:
        """Seconds of lease left. ``<= 0`` means the attempt is no longer
        ours and anything it goes on to do is unaccountable."""
        with self._lock:
            return self._window - (self._monotonic() - self._granted_at)


@dataclass(frozen=True, slots=True)
class HandlerOutcome:
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    retryable: bool = True
    # True only for a refusal that names no fault in the work itself -- a
    # writer-lease race lost to a sibling lane, not a broken commit or a bad
    # payload (VOYN-W0-AICC-PUBLISH-LEASE-CONTENTION-BURNS-ATTEMPT: a
    # publish that lost that race used to fail through the ordinary
    # `retryable=True` path, which spends this item's `max_attempts` on
    # contention it had no part in causing and can dead-letter already
    # -finished work). Routes to `queue_fail_lease_wait` instead of
    # `queue_fail`: it refunds the attempt `queue_claim` already spent for
    # this delivery and counts the wait against its own bounded budget
    # instead. Meaningless when `ok` is True and implies `retryable` --
    # there is no such thing as a non-retryable lease wait.
    lease_wait: bool = False


class Handler(Protocol):
    """One payload executor. It receives the payload, a ``lease_lost`` event
    it must honour (when set, one of its leases is gone and any further effect the
    handler produces is unaccountable), and ``attempt_no`` — the queue's own
    delivery count for this item. The attempt number is an explicit
    parameter rather than a smuggled payload key because it is DELIVERY
    metadata: the executor-cascade (BO-S2a) selects its link from it, and a
    contract a handler can see is one a test can pin.

    ``lease_lost`` IS A CANCELLATION SIGNAL AND NOT A STATEMENT ABOUT WHICH
    lease went, deliberately: ``worker.handlers`` passes this same event into
    ``writer_lease.hold`` so a lost repository writer lease cancels the agent
    through one path rather than two. Whichever lease it was, the handler's
    duty is identical -- stop, and return a retryable failure. Which lease it
    was decides something else entirely, and it is the DAEMON's to decide:
    only a lost QUEUE lease makes the outcome unwritable (see ``_execute``).
    A handler must therefore not read this event as "the queue will redeliver
    me anyway"; the daemon reports what the handler returns whenever the
    queue lease is still live."""

    def __call__(
        self,
        payload: dict[str, Any],
        lease_lost: threading.Event,
        attempt_no: int,
    ) -> HandlerOutcome: ...


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    queue: str = "execution"
    visibility_seconds: int = 300
    # Idle polling: jittered exponential backoff. The floor keeps an idle
    # fleet from hammering the database; the cap keeps a newly enqueued item
    # from waiting long on a quiet host.
    idle_min_seconds: float = 1.0
    idle_max_seconds: float = 30.0


class WorkerDaemon:
    def __init__(
        self,
        store: WorkQueueStore,
        handlers: dict[str, Handler],
        config: WorkerConfig | None = None,
        *,
        sleep: Callable[[float], None] | None = None,
        notify: Callable[[str], object] | None = None,
        reload_credentials: Callable[[], None] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._handlers = dict(handlers)
        self._config = config if config is not None else WorkerConfig()
        self._stop = threading.Event()
        self._drain = threading.Event()
        self._drain_closed = threading.Event()
        self._drain_wakeup = threading.Event()
        self._drain_stop = threading.Event()
        # The coordinator and the claim loop meet at this lock.  The claim
        # loop holds it from its final drain check through claim(), so a drain
        # ACK cannot race ahead of an already-starting database claim.
        self._claim_gate_lock = threading.Lock()
        self._reload_requested = threading.Event()
        self._reload_stop = threading.Event()
        # Injectable so tests drive time instead of waiting through it.
        self._sleep = sleep if sleep is not None else self._stop.wait
        # Injectable so tests observe pings; the default is a no-op outside
        # systemd (sd_notify returns quietly without NOTIFY_SOCKET).
        self._notify = notify if notify is not None else sdnotify.sd_notify
        self._reload_credentials = reload_credentials or (lambda: None)
        # Injectable for the same reason `sleep` is: `_heartbeat_loop`'s
        # tolerance is a span of real time against the lease window, and a
        # test that has to WAIT that span out can only measure the deployed
        # window by not using it. MONOTONIC on purpose -- the quantity is an
        # elapsed duration, and a wall clock stepped by NTP or by the
        # credential rotation's own host would move the lease deadline with it.
        self._monotonic = monotonic if monotonic is not None else time.monotonic

    # -- lifecycle ------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self._on_signal)
        signal.signal(signal.SIGUSR1, self._on_signal)
        signal.signal(signal.SIGHUP, self._on_signal)

    def _on_signal(self, signum: int, _frame: Any) -> None:
        if signum == signal.SIGUSR1:
            logger.info("signal %s: draining; claiming no more", signum)
            self.request_drain()
            return
        if signum == signal.SIGHUP:
            logger.info("signal %s: credential reload requested", signum)
            self.request_reload()
            return
        logger.info("signal %s: finishing the item in hand, claiming no more", signum)
        self._stop.set()

    def request_stop(self) -> None:
        self._stop.set()

    def request_drain(self) -> None:
        self._drain.set()
        # Signal handlers must never block on a lock the interrupted main
        # thread may already hold inside claim().  A dedicated coordinator
        # closes the gate and emits the ACK only after it owns the same lock.
        self._drain_wakeup.set()

    def request_reload(self) -> None:
        self._notify(f"RELOADING=1\nMONOTONIC_USEC={time.monotonic_ns() // 1_000}")
        self._reload_requested.set()

    def _drain_coordinator_loop(self) -> None:
        while not self._drain_stop.is_set():
            if not self._drain_wakeup.wait(0.25):
                continue
            self._drain_wakeup.clear()
            if self._drain_stop.is_set():
                return
            with self._claim_gate_lock:
                if self._drain.is_set() and not self._drain_closed.is_set():
                    self._drain_closed.set()
                    # This is the protocol ACK consumed by the rotator.  At
                    # this point claim() is not running and cannot begin until
                    # the lock is released, after which the closed flag is
                    # checked again before any claim.
                    self._notify("STATUS=aicc-drained")

    def _credential_reload_loop(self) -> None:
        while not self._reload_stop.is_set():
            if not self._reload_requested.wait(0.25):
                continue
            if self._reload_stop.is_set():
                return
            # Clear BEFORE reloading so a SIGHUP arriving mid-reload is
            # coalesced into a follow-up pass instead of silently dropped
            # (review finding on c4001c4).
            self._reload_requested.clear()
            try:
                self._reload_credentials()
            except Exception:  # reload failure is surfaced to systemd by no READY
                # A RAISING RELOAD USED TO STRAND THE LANE FOR THE LIFE OF THE
                # PROCESS. The clear above consumed the request, and this
                # branch dropped it -- so nothing was left to retry, and the
                # only thing that reopens the claim gate is the successful
                # reload below. A lane drained by SIGUSR1 then never claimed
                # again: up, holding a working pool (`replace_pool` builds and
                # authenticates the replacement BEFORE detaching the old one,
                # so a failed rebuild leaves the previous pool intact), and
                # feeding the systemd watchdog from `run_forever`'s draining
                # branch -- so the unit looks healthy and nothing on the host
                # ever restarts it.
                #
                # That is the shape `control-01:queue` reports and the reason
                # it could not be cleared by fleet action: lanes that are up
                # but claiming nothing stop the fleet clock
                # (`fleet_idle_seconds`), due ready work is attended by
                # nobody, and `queue_stalled` opens with no exit any fleet
                # action reaches. It is also the likeliest reload to fail --
                # the rebuild runs against a credential the rotator has just
                # changed, over a tunnel the same rotation restarts.
                #
                # So the reload degrades the way the claim and report paths
                # already do: log it, back off, keep trying. `idle_max_seconds`
                # is the interval every other "cannot proceed yet" answer uses,
                # because none of them is cured by asking again faster.
                #
                # THE GATE IS NOT REOPENED HERE, and that is the point: the
                # drain is the rotator's instruction to stop claiming while
                # the credential it issued is retired, so a lane that resumed
                # because its reload kept failing would claim under exactly
                # the credential being invalidated. It stays shut until a
                # reload actually succeeds -- which now happens on its own,
                # with no operator and no second SIGHUP, the moment whatever
                # broke the rebuild is over.
                logger.exception(
                    "credential reload failed; retrying after a backoff "
                    "(the claim gate stays shut until one succeeds)"
                )
                self._notify("STATUS=aicc-reload-failed")
                # Interruptible, so a stop during the backoff is still prompt.
                if self._reload_stop.wait(self._config.idle_max_seconds):
                    return
                self._reload_requested.set()
                continue
            with self._claim_gate_lock:
                self._drain.clear()
                self._drain_closed.clear()
                self._notify("READY=1")
                self._notify("STATUS=aicc-ready")

    # -- the loop -------------------------------------------------------------

    def run_forever(self) -> None:
        self._notify("READY=1")
        self._notify("STATUS=aicc-ready")
        # Read once: systemd sets WATCHDOG_USEC at spawn and never changes it
        # for a running unit. Every sleep below is capped by it, so the ping
        # cadence adapts to whatever WatchdogSec the unit declares instead of
        # baking the unit file's number into the code.
        watchdog = sdnotify.watchdog_interval_seconds()
        cap = watchdog if watchdog is not None else float("inf")
        idle = self._config.idle_min_seconds
        reload_thread = threading.Thread(
            target=self._credential_reload_loop,
            name="credential-reload",
            daemon=True,
        )
        drain_thread = threading.Thread(
            target=self._drain_coordinator_loop,
            name="claim-gate-drain",
            daemon=True,
        )
        reload_thread.start()
        drain_thread.start()
        try:
            while not self._stop.is_set():
                # Both the flag read and the notify happen under the gate
                # lock: an unlocked snapshot could interleave with the
                # reload loop's clear-flags-then-READY sequence and stamp a
                # stale aicc-drained STATUS over the fresh aicc-ready
                # (independent-review finding on 61c73e7).
                with self._claim_gate_lock:
                    draining = self._drain.is_set()
                    if draining:
                        status = (
                            "aicc-drained"
                            if self._drain_closed.is_set()
                            else "aicc-drain-requested"
                        )
                        self._notify(f"WATCHDOG=1\nSTATUS={status}")
                if draining:
                    self._sleep(min(self._config.idle_min_seconds, cap))
                    continue
                claim_raised = False
                with self._claim_gate_lock:
                    if self._drain.is_set() or self._drain_closed.is_set():
                        continue
                    # STATUS travels with every claim-path ping so a status
                    # written by a dying drain cycle can never stick.
                    self._notify("WATCHDOG=1\nSTATUS=aicc-ready")
                    try:
                        claimed = self._store.claim(
                            self._config.queue,
                            visibility_seconds=self._config.visibility_seconds,
                        )
                    except Exception:
                        # THE ONE CALL ON EVERY ITERATION, AND THE ONLY ONE
                        # THAT WAS UNGUARDED. `_execute` already catches a
                        # raising report write, `_dispatch` a raising handler,
                        # and a non-object payload dead-letters -- each closed
                        # after the same failure, each for the same reason: one
                        # item's fault must not take the lane down with it.
                        # claim() had no such guard, and it is the worst place
                        # to lack one. It runs before any item is in hand, so
                        # the cost is not one attempt but EVERY attempt: the
                        # exception leaves run_forever, the lane exits, systemd
                        # restarts it, and it raises again on the same claim.
                        # The lane crash-loops and claims nothing.
                        #
                        # That is how a single poisoned row became a fleet-wide
                        # outage (monitor_finding #2840: a refunded lease wait
                        # left `attempt_no` taken, so `queue_claim` raised a
                        # unique violation on the OLDEST DUE ROW -- head-of-
                        # line, so every lane selected it and died). Migration
                        # 0025 removed that cause. It did not remove the
                        # amplifier, and the amplifier is what the monitor
                        # measures: lanes that are up but claiming nothing stop
                        # the fleet clock, and `control-01:queue` reports
                        # `queue_stalled` with no exit reachable by fleet
                        # action, because restarting a lane only restarts the
                        # crash. Any next cause -- a dropped connection, a
                        # PostgreSQL restart, a future migration's refusal that
                        # raises instead of returning -- does it again.
                        #
                        # So the claim path degrades the way the report path
                        # already does: log it, back off, keep claiming. A
                        # transient fault costs one poll; a permanent one
                        # leaves a lane that is UP and visibly claiming
                        # nothing, which the queue probe still catches on the
                        # fleet-idle clock. Neither ends with the lane dead.
                        logger.exception(
                            "claim raised; the lane stays up and retries after "
                            "a backoff"
                        )
                        claim_raised = True
                # Deliberately OUTSIDE the gate lock. The drain coordinator
                # takes that lock to emit the `aicc-drained` ACK the credential
                # rotator waits on, so sleeping under it would stall a rotation
                # behind a database fault that has nothing to do with it.
                if claim_raised:
                    self._sleep(min(self._config.idle_max_seconds, cap))
                    continue
                if isinstance(claimed, QueueRefusal):
                    if claimed.reason == "no_work":
                        self._sleep(min(idle + random.uniform(0, idle), cap))
                        idle = min(idle * 2, self._config.idle_max_seconds)
                        continue
                    # Every other refusal is a protocol-level fact worth a log
                    # line, and none of them is cured by asking again faster.
                    logger.warning("claim refused: %s", claimed.reason)
                    self._sleep(min(self._config.idle_max_seconds, cap))
                    continue
                idle = self._config.idle_min_seconds
                self._execute(claimed)
        finally:
            self._reload_stop.set()
            self._reload_requested.set()
            self._drain_stop.set()
            self._drain_wakeup.set()
            reload_thread.join(timeout=5)
            drain_thread.join(timeout=5)
        # STOPPING=1 tells systemd the exit it is about to observe is ours,
        # not a crash — TimeoutStopSec pacing instead of watchdog action.
        self._notify("STOPPING=1")

    def _execute(self, work: ClaimedWork) -> None:
        # TWO EVENTS, BECAUSE TWO DIFFERENT LEASES CAN BE LOST AND ONLY ONE OF
        # THEM FORBIDS THE REPORT (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED,
        # monitor_finding #14016).
        #
        # `cancel` is what the HANDLER is handed: "stop, any further effect you
        # produce is unaccountable". `queue_lease_lost` is the narrower fact
        # that THIS queue attempt is no longer ours, and it is the only one
        # that may discard an outcome -- see the branch below.
        #
        # They used to be one event, and the handler is the second thing that
        # sets it. `worker.handlers` passes the daemon's event straight into
        # `writer_lease.hold(repository, lease_cfg, lease_lost)`, whose renewal
        # thread sets it when the REPOSITORY WRITER lease is lost -- "a renewal
        # failure sets the SAME `lease_lost` event this function already wires
        # into `run_claude_code` as `cancel_event`", by that call site's own
        # comment, and deliberately so: one cancellation path, not two.
        #
        # Sharing the CANCELLATION channel was right. Sharing it with the
        # daemon's "do not write" decision was not, because the two losses have
        # opposite consequences for the queue:
        #
        #   * the QUEUE lease lapsed -> `_queue_owns` refuses the write, and
        #     must (`claim_expired`/`attempt_superseded`); the item is already
        #     the reaper's to recover.
        #   * the WRITER lease was lost -> the queue lease is LIVE and
        #     renewing. `queue_fail` would be ACCEPTED. Discarding the outcome
        #     instead leaves the item `claimed` under a lease nothing is
        #     renewing any more, for the rest of its visibility window.
        #
        # AND THAT SECOND STATE IS A FAIL-OPEN READING ON A FAIL-CLOSED
        # MONITOR, which is the one direction `control-01:queue` must never
        # have. While the abandoned lease is still in the future the probe
        # classifies the row as an ATTENDED claim -- `work_item.state =
        # 'claimed'` with `work_attempt.state = 'active'` and `visible_until >
        # now()`, which is the measurement's own definition of "a lane doing
        # its job" -- when no lane is holding it at all: `_execute` has
        # returned and the loop has gone back to claiming. So `queue.claimed`
        # counts a lane that is free, and `queue.claimed` is exactly what
        # `--claim-capacity` is weighed against (#13366), so
        # `spare_capacity` goes false and `evaluate` excuses due ready work as
        # backpressure behind occupancy that does not exist. Two such
        # abandonments on a two-lane fleet pin it false while both lanes are
        # free.
        #
        # The rest of the cost is the same one 0029 catalogued for a lease the
        # reaper expired early, and for the same reason -- an attempt taken
        # away at the finish line: the run is lost although its outcome was
        # DECIDED and writable, the handler's side effects re-run under the
        # next delivery, and `queue_claim` has already charged `attempt_count`
        # for it, so on the default `max_attempts` of 3 the third such blip
        # dead-letters healthy work as `visibility_timeout_exhausted`.
        #
        # IT IS A BLIP THE FLEET MEETS ON A SCHEDULE IT RUNS ITSELF, the same
        # one `TOLERATED_FAILED_BEATS` exists for: `voyn-lease` reaches its
        # authority over a DSN (`/etc/aicc/lease.env`), the credential rotation
        # restarts the tunnel on its own timer, and `writer_lease._renew_loop`
        # has no tolerance at all -- it gives up on the FIRST failed renewal
        # (that loop's own comment says so). The beat loop survives two; the
        # writer lease survives none, and before this change the whole attempt
        # paid for it.
        #
        # AND `queue_lease_lost` IS NOT THE SAME FACT AS "THIS LANE CANNOT
        # PROVE THE LEASE IS LIVE" (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED,
        # monitor_finding #41318). That third event is `lease_unproven`, and
        # the distinction it carries is the same one this comment draws
        # between the two LEASES, drawn now between two EPISTEMIC states of
        # ONE lease. `_LeaseDeadline` is dated from the last beat the lane SAW
        # accepted, which is the only honest thing a client can date it from
        # -- and AN UNANSWERED BEAT IS NOT A REFUSED BEAT. `store.heartbeat`
        # is one autocommit `SELECT queue_heartbeat(...)`, so a call
        # black-holed on a severed `voyn-aicc-pgtunnel.service` socket may
        # well have REACHED the server, RUN and COMMITTED, with only the reply
        # lost; `connect_timeout` bounds connecting and `statement_timeout` is
        # enforced by a server that is no longer reachable, so nothing says
        # otherwise. The server's `visible_until` is then up to one beat
        # interval later than this lane's deadline (75s against the deployed
        # 300s window, `beat_interval_seconds` = V/4), and inside that gap the
        # attempt IS still ours by the only authority that decides it --
        # `_queue_owns`.
        #
        # So the deadline is evidence to STOP THE WORK, which is fail-closed
        # and is unchanged. It is not evidence that the WRITE is refused, and
        # the lane used to treat it as both: `queue_lease_lost` was set from
        # the clock and the outcome discarded, which left the item `claimed`
        # under a lease nothing renews -- the phantom attended claim this
        # comment catalogues above, arriving from the lane's own inference
        # instead of from a shared cancellation event. `queue_fail` WOULD have
        # been accepted in that gap, and the rest of the cost is 0029's
        # catalogue: the run is lost although its outcome was decided and
        # writable, the handler's side effects re-run under the next delivery,
        # and `queue_claim` has already charged `attempt_count` -- so on the
        # default `max_attempts` of 3 the third tunnel restart dead-letters
        # healthy work as `visibility_timeout_exhausted`.
        queue_lease_lost = threading.Event()
        lease_unproven = threading.Event()
        cancel = threading.Event()
        beat_stop = threading.Event()
        # THE ATTEMPT'S LEASE CLOCK, OWNED HERE BECAUSE THE ATTEMPT IS OWNED
        # HERE, and read by both of the threads below. Constructed before
        # either starts, so the window is dated from the claim rather than
        # from whenever a thread happened to be scheduled.
        deadline = _LeaseDeadline(self._config.visibility_seconds, self._monotonic)
        beat = threading.Thread(
            target=self._heartbeat_loop,
            args=(work, deadline, lease_unproven, queue_lease_lost, cancel, beat_stop),
            name=f"heartbeat-{work.attempt_id}",
            daemon=True,
        )
        # A SECOND THREAD THAT DOES NOTHING BUT WATCH A CLOCK
        # (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, monitor_finding #41237).
        # The deadline was enforced only at the top of the beat loop, so the
        # one failure it exists for -- a heartbeat black-holed on a severed
        # `voyn-aicc-pgtunnel.service` socket -- was also the one failure that
        # stopped it being enforced: the beat thread cannot test a clock while
        # it is parked inside `store.heartbeat`, and nothing client-side
        # bounds that call. The lapse therefore arrived at `window + block`
        # with `block` unbounded, and every second of that overrun is a lane
        # executing an item `aicc-queue-reaper.timer` has already handed back.
        # This thread touches no store, so no database fault can delay it; see
        # `_lease_deadline_loop`.
        #
        # IT IS HANDED `lease_unproven` AND NOT `queue_lease_lost`, and the
        # signature is the statement (monitor_finding #41318): a thread that
        # holds no connection cannot establish what the SERVER thinks of this
        # attempt, so it must not be able to say so.
        lapse = threading.Thread(
            target=self._lease_deadline_loop,
            args=(work, deadline, lease_unproven, cancel, beat_stop),
            name=f"lease-deadline-{work.attempt_id}",
            daemon=True,
        )
        beat.start()
        lapse.start()
        try:
            outcome = self._dispatch(work, cancel)
        finally:
            # One stop signal for the attempt's whole lease machinery: both
            # threads wait on it, so both wake the moment the handler is done.
            beat_stop.set()
            beat.join(timeout=5)
            lapse.join(timeout=5)

        # THE THREE STATES OF ONE ATTEMPT, IN THE ORDER THEIR CONSEQUENCES
        # DIFFER (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, monitor_finding
        # #41318). Only the first of them may discard an outcome.
        if queue_lease_lost.is_set():
            # THE SERVER SAID SO. `queue_heartbeat` answered `ok = false`,
            # which is `_queue_owns` reporting `attempt_superseded` or
            # `claim_expired` -- the attempt is already somebody else's (or
            # the reaper's). Reporting anything would be refused as a stale
            # owner and must be: writing a result after losing the lease is
            # exactly the lost update this protocol exists to prevent.
            #
            # IT IS THE ONLY FACT THAT ESTABLISHES THAT, and that is why the
            # condition is this event rather than `cancel` or the deadline.
            # The two other senders are inferences: the repository writer
            # lease, which says nothing about the queue lease at all, and this
            # lane's own clock, which says only that it could not SEE a
            # renewal. Discarding on either of those is the phantom attended
            # claim this function's opening comment measures.
            logger.warning(
                "attempt %s: the server refused a beat for this attempt; "
                "outcome discarded",
                work.attempt_id,
            )
            return

        if cancel.is_set():
            # CANCELLED BY SOMETHING THAT DID NOT ESTABLISH THE SERVER'S
            # VERDICT -- the writer lease, or this lane's own lease deadline.
            # Either way `queue_fail` may well be ACCEPTED, so it is SENT and
            # the database decides: the item returns to the queue NOW instead
            # of sitting `claimed` for the rest of a window nothing is
            # renewing, and a refusal is the protocol working (logged below as
            # a stale owner) at the cost of one round trip.
            cause = (
                "a queue lease this lane could not prove was live"
                if lease_unproven.is_set()
                else "a lost writer lease"
            )
            logger.warning(
                "attempt %s: cancelled by %s; reporting the outcome so the "
                "item returns to the queue instead of waiting out its "
                "visibility window",
                work.attempt_id,
                cause,
            )
            # FAIL CLOSED OVER THE HANDLER'S OWN VERDICT, and only here. A
            # cancelled run's workspace is whatever a forcibly terminated
            # agent left behind, so NEITHER terminal write may be taken from
            # it: not `queue_complete`, which carries a result, and not a
            # non-retryable `queue_fail`, which dead-letters the item on a
            # verdict about work that never finished. `worker.handlers`
            # already returns a retryable failure on this path; this covers a
            # handler that honoured `cancel` by returning its own answer
            # anyway, because a daemon must not take a handler's word for a
            # terminal write. Retryability is covered as well as success
            # because this branch now carries the lapse path too, where a
            # handler deep in its own refusal logic can return
            # `retryable=False` for a reason that has nothing to do with the
            # lease that was just taken away.
            verdict = (
                "reported success"
                if outcome.ok
                else "reported a non-retryable failure"
                if not outcome.retryable
                else ""
            )
            if verdict:
                said = f" ({outcome.reason})" if outcome.reason.strip() else ""
                outcome = HandlerOutcome(
                    ok=False,
                    reason=f"cancelled by {cause} after the handler {verdict}{said}",
                    retryable=True,
                )
        try:
            if outcome.ok:
                accepted = self._store.complete(work, outcome.result)
            elif outcome.lease_wait:
                accepted = self._store.fail_lease_wait(work, reason=outcome.reason)
            else:
                accepted = self._store.fail(
                    work, reason=outcome.reason, retryable=outcome.retryable
                )
        except Exception:
            # The handler successfully decided an outcome -- what it admits --
            # but persisting it raised (a DB hiccup, a dropped connection, a
            # result the driver cannot encode): the same class of failure
            # `_dispatch` already guards for a raising HANDLER. Uncaught here,
            # it would propagate out of `run_forever`'s loop and kill the
            # whole daemon over the one attempt it was reporting, taking every
            # other queued item down with it -- mirroring the non-object-
            # payload crash this module already closed once. The lease is
            # left to lapse on its own (visibility expiry, then the reaper),
            # exactly like the stale-owner refusal below; a later delivery
            # retries.
            logger.exception(
                "attempt %s: writing the outcome raised; the attempt's lease "
                "will lapse and a later delivery will retry",
                work.attempt_id,
            )
            return
        if not accepted:
            # The database refused the report: the lease lapsed between our
            # last successful beat and this write, and the attempt belongs to
            # someone else now. The refusal is the protocol working — but a
            # daemon that does not KNOW it happened re-runs the handler's side
            # effects on retry with no operator-visible trace of why. Review
            # found exactly this interleaving via the heartbeat-error path.
            logger.warning(
                "attempt %s: report refused as stale owner; outcome lost to a "
                "lapsed lease (handler effects may re-run on the next attempt)",
                work.attempt_id,
            )

    def _dispatch(
        self, work: ClaimedWork, lease_lost: threading.Event
    ) -> HandlerOutcome:
        if not isinstance(work.payload, dict):
            # queue_enqueue accepts any jsonb — a list or bare string is
            # producible by the app role, and `.get` on it would raise out of
            # run_forever and kill the daemon over one malformed item.
            return HandlerOutcome(
                ok=False,
                reason=f"payload is {type(work.payload).__name__}, not an object",
                retryable=False,
            )
        kind = str(work.payload.get("kind", ""))
        handler = self._handlers.get(kind)
        if handler is None:
            # Non-retryable by design: a payload nobody can execute will not
            # become executable on the next attempt, and retrying it burns the
            # attempt budget on the way to the same dead letter.
            return HandlerOutcome(
                ok=False,
                reason=f"no handler for payload kind {kind!r}",
                retryable=False,
            )
        try:
            return handler(work.payload, lease_lost, work.attempt_no)
        except Exception as error:
            logger.exception("handler for %r raised", kind)
            return HandlerOutcome(ok=False, reason=repr(error), retryable=True)

    @staticmethod
    def _stop_the_handler(fact: threading.Event, cancel: threading.Event) -> None:
        """Record the narrow fact about THIS attempt, then stop the handler.

        ``fact`` is whichever of ``queue_lease_lost`` (the server refused a
        beat) or ``lease_unproven`` (this lane's own deadline passed) the
        caller is in a position to establish. ``_execute`` reads them to decide
        what may be written, and they are not interchangeable -- see its own
        comment.

        The order is load-bearing and is why this is one function rather than
        two calls at each site. ``cancel`` is what the handler waits on, so
        the moment it is set the handler may return and ``_execute`` may read
        the narrow fact -- which must therefore already be true, or a lost
        QUEUE lease would be mistaken for a lost WRITER lease and a report the
        database is bound to refuse would be written anyway.
        """
        fact.set()
        cancel.set()

    def _lease_deadline_loop(
        self,
        work: ClaimedWork,
        deadline: "_LeaseDeadline",
        lease_unproven: threading.Event,
        cancel: threading.Event,
        beat_stop: threading.Event,
    ) -> None:
        """Stop the handler the moment this lane can no longer prove the lease
        is live, from a thread nothing in the database can delay.

        WHY A WHOLE THREAD FOR ONE SUBTRACTION
        (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, monitor_finding #41237).
        `_heartbeat_loop` already held the right rule and already measured it
        against a clock -- the previous finding (#41136) replaced a count of
        failed beats with exactly that. What it could not do is EVALUATE the
        rule at the deadline, because it evaluates it at the top of its loop
        and the step before that top is `store.heartbeat`. The failure the
        tolerance exists for is a beat that does not come back: the credential
        rotation restarts `voyn-aicc-pgtunnel.service` on its own timer, a
        severed forwarded socket BLACK-HOLES the call, `connect_timeout` (10s)
        bounds only connecting and `statement_timeout` is enforced by a server
        that is no longer reachable. So the thread that owned the deadline was
        parked, without a client-side bound, by the very event the deadline
        was for -- "one window plus the one call it was parked in", which the
        previous fix stated as its own residual and left to a transport
        change. It is not a transport problem. The lapse is a statement about
        a CLOCK, and a clock can be read somewhere the socket cannot reach.

        WHAT THE OVERRUN COSTS, every second of it, is the probe's occupancy.
        The lane keeps executing an item it no longer owns;
        `aicc-queue-reaper.timer` does exactly its job within the minute and
        hands that item back; and now the fleet holds TWO lanes' worth of work
        while `work_item` records ONE claim. `claimed` is what
        `--claim-capacity` is weighed against (monitor_finding #13366), so
        `spare_capacity` goes true against a fleet with no free lane at all,
        the due-ready clock runs in full, and `fleet_idle_seconds` cannot
        bound it because the surviving claim is an hour old. `control-01:
        queue` opens `queue_stalled` and NO FLEET ACTION REACHES IT: the
        lapsed-claim class is empty (the reaper has already recovered the
        item), restarting the lane only arranges for the next tunnel blip to
        do it again, and `queue_redrive` reaches only items already dead.
        `tests/db/test_infra_monitor_queue_snapshot.py::test_a_lane_running_
        past_its_lease_hides_the_fleets_real_occupancy` measures that whole
        chain against a real server. The second lane that claims the
        handed-back item meanwhile re-runs the handler's side effects --
        `git push` and PR creation included -- against a workspace the first
        lane is still writing to.

        IT TOUCHES NO STORE, AND THAT IS THE ENTIRE POINT. No heartbeat, no
        report, no pool checkout: a database that is unreachable, slow, or
        gone cannot add a microsecond to the instant this fires. The only
        calls here are a subtraction on a monotonic clock and
        `threading.Event.wait`.

        AND IT DOES NOT FEED THE WATCHDOG, deliberately. `aicc-worker.service`
        sizes `WatchdogSec` so that a wedged process -- a handler that hangs
        AND takes the beat thread with it -- misses ping after ping and is
        restarted; a thread that pings while doing nothing would answer "is
        the process alive" with "yes" for a process that has stopped being
        useful, and would retire that detector. Pings stay where they prove
        something: the claim loop between claims, the beat thread around a
        call that actually reached the server.

        AND WHAT IT RAISES IS `lease_unproven`, NOT `queue_lease_lost`
        (monitor_finding #41318). The clock is the honest thing to stop the
        WORK on: past the deadline this lane cannot show the attempt is still
        its own, and anything it goes on to produce is unaccountable. It is
        not a verdict on the WRITE. The deadline is dated from the last beat
        the lane SAW accepted, and an unanswered beat is not a refused beat --
        `store.heartbeat` is one autocommit statement, so a black-holed call
        may have reached the server, run and COMMITTED with only the reply
        lost, which leaves the server's `visible_until` up to one beat
        interval beyond this deadline. Inside that gap `queue_fail` is
        ACCEPTED, and the only thing that can say otherwise is `_queue_owns`.
        So this thread stops the handler and `_execute` offers the outcome to
        the server; see `_execute`'s own comment for what discarding it cost
        the probe.
        """
        while True:
            remaining = deadline.remaining()
            if remaining <= 0:
                logger.warning(
                    "attempt %s: a whole visibility window has passed with no "
                    "heartbeat this lane saw the server accept; the handler "
                    "stops now (detected by the deadline watch, so not when "
                    "the beat thread next answers) and the outcome is offered "
                    "to the server, which is the only thing that can refuse it",
                    work.attempt_id,
                )
                self._stop_the_handler(lease_unproven, cancel)
                return
            # Sleeps to the deadline and no further, then re-reads it: a beat
            # the server accepted in the meantime has moved it, and this wakes
            # exactly to find that out. `beat_stop` is `_execute`'s "the
            # handler is done", so an attempt that finishes normally cancels
            # this wait instead of waiting the rest of its lease out.
            if beat_stop.wait(remaining):
                return

    def _heartbeat_loop(
        self,
        work: ClaimedWork,
        deadline: "_LeaseDeadline",
        lease_unproven: threading.Event,
        queue_lease_lost: threading.Event,
        cancel: threading.Event,
        beat_stop: threading.Event,
    ) -> None:
        # The renewal cadence, derived from the tolerance rather than guessed
        # at. See `beat_interval_seconds` for the arithmetic; this line used to
        # be `visibility_seconds / 3.0`, which delivered ZERO of the two failed
        # beats the comment above it promised. The watchdog cap can shorten the
        # wait — an extra lease renewal is harmless, a missed watchdog deadline
        # during a long run is a restart.
        interval = beat_interval_seconds(
            self._config.visibility_seconds, sdnotify.watchdog_interval_seconds()
        )
        while True:
            # HOW MUCH LEASE IS LEFT. The rule has ONE definition
            # (`_LeaseDeadline`) and TWO readers, and this is the one that can
            # be parked: `_lease_deadline_loop` is the other, and it exists
            # because being parked here is what the deadline is for
            # (monitor_finding #41237). The test is kept here as well because
            # a loop that can prove its lease is gone must not go on sending
            # beats for it -- and because this function must still be correct
            # when it is the only thing running.
            #
            # IT USED TO BE A COUNT OF FAILED BEATS
            # (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED). The test was
            # `consecutive_errors * interval >= visibility_seconds` on the
            # error path, which is elapsed time only if a heartbeat that
            # cannot reach the server FAILS FAST. The failure this tolerance
            # exists for does the opposite: the beat crosses
            # `voyn-aicc-pgtunnel.service`, which the credential rotation
            # restarts on its own timer, and a severed forwarded socket
            # BLACK-HOLES the call -- `connect_timeout` (10s) bounds
            # connecting, and `statement_timeout` is enforced by a server
            # that is no longer reachable, so nothing client-side bounds the
            # read. Each failed cycle therefore cost `interval + block` while
            # the counter still demanded `TOLERATED_FAILED_BEATS + 2` of them,
            # and the loop gave the lease up at `(F + 2) * (interval + block)`
            # instead of at `visibility_seconds`. MEASURED through this loop:
            # at a 6s window with a 1.5s block it declared the lease lost at
            # 12.0s, a whole extra window; at the deployed 300s window with
            # the 120s `lease_client` already allows a hung subprocess, 1560s.
            #
            # WHAT THE OVERRUN COSTS IS THE PROBE'S OCCUPANCY, which is the
            # one thing `control-01:queue` cannot survive being lied to
            # about. The lane keeps executing an item it no longer owns;
            # `aicc-queue-reaper.timer` does exactly its job and hands that
            # item back; and now the fleet holds TWO lanes' worth of work
            # while `work_item` records ONE claim. `claimed` is what
            # `--claim-capacity` is weighed against (monitor_finding #13366),
            # so `spare_capacity` goes true, the due-ready clock runs in full
            # against a fleet with no free lane at all, and `fleet_idle_
            # seconds` cannot bound it because the surviving claim is hours
            # old. MEASURED against a real PostgreSQL 16 server through the
            # production snapshot statement -- two lanes 70 minutes into
            # attempts, the two surplus dispatched items due behind them,
            # lane A's lease lapsed and reaped while lane A runs on:
            #
            #     both leases live  claimed=2 attended=2 fleet_idle=4200 -> ok
            #     lane A runs on    claimed=1 attended=1 fleet_idle=1200
            #                                             -> queue_stalled
            #
            # Nothing about the work changed between those two lines, and no
            # fleet action reaches the second: the reaper has already
            # recovered the item, and restarting the lane only arranges for
            # the next tunnel blip to do it again.
            #
            # DATED FROM WHEN A BEAT ANSWERED, NOT FROM WHEN IT WAS SENT. The
            # server grants the renewal somewhere inside the round trip, so
            # the instant the answer arrives is at or after the grant --
            # which makes this test PROVE the lapse rather than estimate it.
            # Timing it from the send would give the lease up to a round trip
            # early, and an attempt taken away with time left on its lease
            # costs the whole run (migration 0029).
            remaining = deadline.remaining()
            if remaining <= 0:
                # `lease_unproven`, NOT `queue_lease_lost` (monitor_finding
                # #41318). This branch reads a CLOCK, and the clock is dated
                # from the last beat THIS LANE SAW accepted -- so what it
                # establishes is that the lane can no longer prove the attempt
                # is its own, which is reason enough to stop the work and is
                # not a verdict on the write. The verdict below is: `alive`
                # false is `_queue_owns` itself answering.
                logger.warning(
                    "attempt %s: a whole visibility window has passed with no "
                    "heartbeat this lane saw the server accept; the handler "
                    "stops and the outcome is offered to the server",
                    work.attempt_id,
                )
                self._stop_the_handler(lease_unproven, cancel)
                return
            # Never sleep PAST the deadline. The cadence is what keeps the
            # lease alive; once less than one interval of it is left, sleeping
            # a whole interval would park this thread -- and the handler --
            # beyond the moment the attempt stopped being ours. Capping the
            # wait is what makes the bound above one window rather than one
            # window plus a sleep.
            if beat_stop.wait(min(interval, remaining)):
                return
            # Fed even when the database is unreachable: the watchdog answers
            # "is the process alive", not "is PostgreSQL up" — restarting the
            # unit cures a wedged process and cures nothing about a DB outage,
            # which the lease-lapse logic above already handles.
            self._notify("WATCHDOG=1")
            try:
                alive = self._store.heartbeat(work)
            except Exception:
                # Errors are not refusals, and they are not a verdict either:
                # the deadline owns that decision, and a blip inside the
                # window costs nothing but this beat. Review found the
                # interleaving the deadline closes: DB down through the lapse,
                # recovered before the handler finished, result refused as
                # stale with no trace.
                logger.exception("heartbeat error for attempt %s", work.attempt_id)
                continue
            # The instant the answer arrived, captured before anything else
            # this loop does, because that instant is the quantity: the server
            # granted the renewal somewhere inside the round trip, so this is
            # at or after the grant.
            answered_at = self._monotonic()
            # Only a beat the SERVER ACCEPTED moves the deadline, and `alive`
            # is how `queue_heartbeat` says so. A refusal is an answer, but it
            # is the answer "this attempt is not yours" -- re-dating the lease
            # on it would push the deadline watch out past a lapse the server
            # has just confirmed.
            if not alive:
                # THE SERVER'S OWN VERDICT, and the only thing in this module
                # that may raise `queue_lease_lost`: `queue_heartbeat` returns
                # `ok = false` only through `_queue_owns`, so this is
                # `attempt_superseded` or `claim_expired` from the authority
                # that decides. `_execute` discards the outcome on it and on
                # nothing else (monitor_finding #41318).
                self._stop_the_handler(queue_lease_lost, cancel)
                return
            deadline.renew(answered_at)
