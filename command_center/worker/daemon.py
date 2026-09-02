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
  work: the beat thread renews at a third of the visibility window and raises
  a stop flag the moment the database says ``attempt_superseded``.
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
    "Handler",
    "HandlerOutcome",
    "MIN_VISIBILITY_SECONDS",
    "WorkerConfig",
    "WorkerDaemon",
]


@dataclass(frozen=True, slots=True)
class HandlerOutcome:
    ok: bool
    result: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    retryable: bool = True


class Handler(Protocol):
    """One payload executor. It receives the payload, a ``lease_lost`` event
    it must honour (when set, the lease is gone and any further effect the
    handler produces is unaccountable), and ``attempt_no`` — the queue's own
    delivery count for this item. The attempt number is an explicit
    parameter rather than a smuggled payload key because it is DELIVERY
    metadata: the executor-cascade (BO-S2a) selects its link from it, and a
    contract a handler can see is one a test can pin."""

    def __call__(
        self,
        payload: dict[str, Any],
        lease_lost: threading.Event,
        attempt_no: int,
    ) -> HandlerOutcome: ...


# The database will clamp anything from 1s up (`work_attempt_visibility_sane`,
# `queue_claim`'s `least(greatest(..., 1), 3600)`), but 1s is a correctness
# floor, not a cost-aware one. Measured host->DB round trips for this daemon's
# own claim->complete pair (remote worker, n=19): min 60.9 / p50 66.5 / p95
# 101.1 / max 135.0 ms, and the reaper's redelivery window runs 97-297 ms wide
# against its own polling period. A worker doing nothing but reporting already
# spends up to ~13.5% of a 1-second window on that report alone, and the
# heartbeat loop's own 1-second floor (`_heartbeat_loop` below) cannot renew a
# lease that short in time to matter. Requiring the window to be at least ten
# times the measured worst-case redelivery tail (0.297s) keeps that unavoidable
# reporting cost under ~10% and gives the heartbeat real headroom above its
# floor.
MIN_VISIBILITY_SECONDS = 3


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    queue: str = "execution"
    visibility_seconds: int = 300
    # Idle polling: jittered exponential backoff. The floor keeps an idle
    # fleet from hammering the database; the cap keeps a newly enqueued item
    # from waiting long on a quiet host.
    idle_min_seconds: float = 1.0
    idle_max_seconds: float = 30.0

    def __post_init__(self) -> None:
        if self.visibility_seconds < MIN_VISIBILITY_SECONDS:
            raise ValueError(
                f"visibility_seconds={self.visibility_seconds} is below the "
                f"{MIN_VISIBILITY_SECONDS}s reporting-cost floor -- a remote "
                "worker's own claim/complete round trip (measured up to 135ms, "
                "redelivery tail up to 297ms) would eat an unsafe fraction of "
                "the window, or outrun the heartbeat entirely"
            )


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
                logger.exception("credential reload failed")
                self._notify("STATUS=aicc-reload-failed")
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
                with self._claim_gate_lock:
                    if self._drain.is_set() or self._drain_closed.is_set():
                        continue
                    # STATUS travels with every claim-path ping so a status
                    # written by a dying drain cycle can never stick.
                    self._notify("WATCHDOG=1\nSTATUS=aicc-ready")
                    claimed = self._store.claim(
                        self._config.queue,
                        visibility_seconds=self._config.visibility_seconds,
                    )
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
        lease_lost = threading.Event()
        beat_stop = threading.Event()
        beat = threading.Thread(
            target=self._heartbeat_loop,
            args=(work, lease_lost, beat_stop),
            name=f"heartbeat-{work.attempt_id}",
            daemon=True,
        )
        beat.start()
        try:
            outcome = self._dispatch(work, lease_lost)
        finally:
            beat_stop.set()
            beat.join(timeout=5)

        if lease_lost.is_set():
            # The database already gave this attempt to someone else (or will,
            # at reap). Reporting anything would be refused as a stale owner —
            # and must be: writing a result after losing the lease is exactly
            # the lost-update this protocol exists to prevent.
            logger.warning(
                "attempt %s: lease lost mid-execution; outcome discarded",
                work.attempt_id,
            )
            return
        if outcome.ok:
            accepted = self._store.complete(work, outcome.result)
        else:
            accepted = self._store.fail(
                work, reason=outcome.reason, retryable=outcome.retryable
            )
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

    def _heartbeat_loop(
        self,
        work: ClaimedWork,
        lease_lost: threading.Event,
        beat_stop: threading.Event,
    ) -> None:
        # A third of the window: two consecutive beats may fail (a restarting
        # PostgreSQL, a network blip) before the lease actually lapses. The
        # watchdog cap can shorten the wait — an extra lease renewal is
        # harmless, a missed watchdog deadline during a long run is a restart.
        interval = max(self._config.visibility_seconds / 3.0, 1.0)
        watchdog = sdnotify.watchdog_interval_seconds()
        if watchdog is not None:
            interval = min(interval, max(watchdog, 1.0))
        consecutive_errors = 0
        while not beat_stop.wait(interval):
            # Fed even when the database is unreachable: the watchdog answers
            # "is the process alive", not "is PostgreSQL up" — restarting the
            # unit cures a wedged process and cures nothing about a DB outage,
            # which the lease-lapse logic below already handles.
            self._notify("WATCHDOG=1")
            try:
                alive = self._store.heartbeat(work)
            except Exception:
                consecutive_errors += 1
                logger.exception("heartbeat error for attempt %s", work.attempt_id)
                # Errors are not refusals, but they are not free either: after
                # a full visibility window without one successful beat the
                # lease has provably lapsed on the server, whatever the reason
                # here — and the handler must stop before its outcome becomes
                # a stale write. Review found the interleaving this closes:
                # DB down through the lapse, recovered before the handler
                # finished, result refused as stale with no trace.
                if consecutive_errors * interval >= self._config.visibility_seconds:
                    lease_lost.set()
                    return
                continue
            consecutive_errors = 0
            if not alive:
                lease_lost.set()
                return
