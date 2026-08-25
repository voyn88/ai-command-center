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

import hashlib
import logging
import random
import signal
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from command_center.db.work_queue_store import (
    ClaimedWork,
    QueueRefusal,
    WorkQueueStore,
)
from command_center.observability import WorkerTelemetry
from command_center.worker import sdnotify

logger = logging.getLogger(__name__)

__all__ = ["Handler", "HandlerOutcome", "WorkerConfig", "WorkerDaemon"]


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
        config: WorkerConfig = WorkerConfig(),
        *,
        sleep: Callable[[float], None] | None = None,
        notify: Callable[[str], object] | None = None,
        telemetry: WorkerTelemetry | None = None,
    ) -> None:
        self._store = store
        self._handlers = dict(handlers)
        self._config = config
        self._stop = threading.Event()
        # Injectable so tests drive time instead of waiting through it.
        self._sleep = sleep if sleep is not None else self._stop.wait
        # Injectable so tests observe pings; the default is a no-op outside
        # systemd (sd_notify returns quietly without NOTIFY_SOCKET).
        self._notify = notify if notify is not None else sdnotify.sd_notify
        self._telemetry = telemetry

    # -- lifecycle ------------------------------------------------------------

    def install_signal_handlers(self) -> None:
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, self._on_signal)

    def _on_signal(self, signum: int, _frame: Any) -> None:
        logger.info("signal %s: finishing the item in hand, claiming no more", signum)
        self._stop.set()

    def request_stop(self) -> None:
        self._stop.set()

    # -- the loop -------------------------------------------------------------

    def run_forever(self) -> None:
        self._notify("READY=1")
        # Read once: systemd sets WATCHDOG_USEC at spawn and never changes it
        # for a running unit. Every sleep below is capped by it, so the ping
        # cadence adapts to whatever WatchdogSec the unit declares instead of
        # baking the unit file's number into the code.
        watchdog = sdnotify.watchdog_interval_seconds()
        cap = watchdog if watchdog is not None else float("inf")
        idle = self._config.idle_min_seconds
        while not self._stop.is_set():
            self._notify("WATCHDOG=1")
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
        # STOPPING=1 tells systemd the exit it is about to observe is ours,
        # not a crash — TimeoutStopSec pacing instead of watchdog action.
        self._notify("STOPPING=1")

    def _execute(self, work: ClaimedWork) -> None:
        if self._telemetry is not None:
            payload = work.payload if isinstance(work.payload, dict) else {}
            task = (
                payload.get("backlog_task_id")
                or payload.get("task_id")
                or work.work_item_id
            )
            sha = payload.get("sha") or payload.get("head_sha") or "unknown"
            self._telemetry.start(
                task=task,
                sha=sha,
                attempt=work.attempt_id,
            )
        trace_id = hashlib.sha256(work.attempt_id.encode()).hexdigest()[:32]
        logger.info(
            "trace_start trace_id=%s attempt=%s task=%s sha=%s",
            trace_id,
            work.attempt_id,
            self._telemetry.task if self._telemetry else work.work_item_id,
            self._telemetry.sha if self._telemetry else "unknown",
        )
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
            if self._telemetry is not None:
                self._telemetry.finish(succeeded=False)
            logger.info(
                "trace_end trace_id=%s attempt=%s outcome=lease_lost",
                trace_id,
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
        if self._telemetry is not None:
            result = outcome.result if isinstance(outcome.result, dict) else {}
            self._telemetry.finish(
                succeeded=outcome.ok and accepted,
                sha=result.get("head_sha") or result.get("sha"),
            )
        logger.info(
            "trace_end trace_id=%s attempt=%s outcome=%s",
            trace_id,
            work.attempt_id,
            "succeeded" if outcome.ok and accepted else "failed",
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
        except Exception as error:  # noqa: BLE001 -- the boundary of the daemon
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
            except Exception:  # noqa: BLE001 -- transient DB errors must not kill the beat
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
