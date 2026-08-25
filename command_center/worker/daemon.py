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
* **The watchdog is fed by a thread that cannot block** (SRV-06, revised by
  VOYN-W0-AICC-WATCHDOG-KILLS-REAL-RUNS). The claim loop pings between
  claims but blocks for the whole of a handler's run, so a second thread
  pings during exactly that run. That thread does two things — ping, and
  subtract two clock readings — and in particular never touches PostgreSQL.
  It used to: the ping rode the *lease* beat, which shares its thread with a
  database round trip and returns the moment the lease is declared lost. So
  a hung socket to the database, or one superseded attempt, silenced this
  process's liveness while a perfectly healthy agent session was running,
  and systemd shot the whole unit at ``WatchdogSec`` — mid-session, every
  time, for runs measured in tens of minutes. Liveness now answers "is this
  process alive", which is the only question the watchdog asks; the lease
  answers its own question on its own thread.
* **The ticker owns the lease deadline, because the beat cannot time its own
  hang.** A beat blocked inside the driver counts no seconds; the ticker
  does, and raises ``lease_lost`` once a full visibility window has passed
  with no renewal the database accepted — so a handler whose lease has
  provably lapsed still stops before its effects become a stale write.
* **A handler that hangs forever is not the watchdog's to kill.** Its ticker
  keeps pinging, so systemd will not restart us for it. That is deliberate:
  the runner's own ``timeout_seconds`` bounds a stuck run, and the
  alternative — shooting the process at ``WatchdogSec`` — is precisely the
  failure this design exists to stop. A wedged *claim loop* is still caught:
  between claims the loop is the only pinger, and no ticker exists then.
"""

from __future__ import annotations

import logging
import random
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from command_center.db.work_queue_store import (
    ClaimedWork,
    QueueRefusal,
    WorkQueueStore,
)
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


@dataclass(slots=True)
class _LeaseClock:
    """The monotonic time of the last renewal the database *accepted*, handed
    from the beat thread that writes it to the ticker thread that times it.
    Mutable and lock-free on purpose: one float, one writer, and an
    assignment the GIL already makes atomic."""

    renewed_at: float


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
        lease_lost = threading.Event()
        dispatch_over = threading.Event()
        lease = _LeaseClock(renewed_at=time.monotonic())
        beat = threading.Thread(
            target=self._heartbeat_loop,
            args=(work, lease, lease_lost, dispatch_over),
            name=f"heartbeat-{work.attempt_id}",
            daemon=True,
        )
        # The ticker covers exactly the window the claim loop cannot ping
        # through, and it is stopped by the handler RETURNING — not by the
        # lease, not by the database. A handler still winding down after
        # `lease_lost` is a live process doing accountable work (unwinding,
        # closing a subprocess), and letting systemd shoot it there is how
        # this daemon used to lose half-finished agent sessions.
        ticker = threading.Thread(
            target=self._watchdog_loop,
            args=(work, lease, lease_lost, dispatch_over),
            name=f"watchdog-{work.attempt_id}",
            daemon=True,
        )
        beat.start()
        ticker.start()
        try:
            outcome = self._dispatch(work, lease_lost)
        finally:
            dispatch_over.set()
            beat.join(timeout=5)
            ticker.join(timeout=5)

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
        except Exception as error:  # noqa: BLE001 -- the boundary of the daemon
            logger.exception("handler for %r raised", kind)
            return HandlerOutcome(ok=False, reason=repr(error), retryable=True)

    def _tick_interval(self) -> float:
        """The cadence both per-run threads wake on: a third of the visibility
        window, shortened when systemd's deadline is the tighter one.

        One number for both, because the two threads check each other's work —
        the ticker times the beat's silence, so it must not wake more slowly
        than the beat renews, or a lapsed lease would go unnoticed for a whole
        extra tick.
        """
        interval = max(self._config.visibility_seconds / 3.0, 1.0)
        watchdog = sdnotify.watchdog_interval_seconds()
        if watchdog is not None:
            # An extra lease renewal is harmless; a missed watchdog deadline
            # during a long run is systemd killing a live agent session.
            interval = min(interval, max(watchdog, 1.0))
        return interval

    def _watchdog_loop(
        self,
        work: ClaimedWork,
        lease: _LeaseClock,
        lease_lost: threading.Event,
        dispatch_over: threading.Event,
    ) -> None:
        """Liveness for the length of a handler's run, and the lease's own
        deadline. Nothing in here can block: a ping and a subtraction, no
        database call, no handler state. That is the entire point — see the
        module docstring for the runs this cost us."""
        interval = self._tick_interval()
        while not dispatch_over.wait(interval):
            self._notify("WATCHDOG=1")
            if lease_lost.is_set():
                continue
            stale_for = time.monotonic() - lease.renewed_at
            if stale_for >= self._config.visibility_seconds:
                # A full visibility window with no renewal the database
                # accepted: the lease has provably lapsed on the server,
                # whatever the local reason — a beat erroring every tick, or
                # one still blocked inside the driver. The handler must stop
                # before its outcome becomes a stale write. The beat cannot
                # raise this itself: a thread wedged in a socket read counts
                # no seconds, and that hang is exactly the case that used to
                # end with systemd shooting the process instead.
                logger.warning(
                    "attempt %s: no lease renewal accepted for %.0fs "
                    "(visibility %ss); declaring the lease lost",
                    work.attempt_id,
                    stale_for,
                    self._config.visibility_seconds,
                )
                lease_lost.set()

    def _heartbeat_loop(
        self,
        work: ClaimedWork,
        lease: _LeaseClock,
        lease_lost: threading.Event,
        dispatch_over: threading.Event,
    ) -> None:
        # A third of the window: two consecutive beats may fail (a restarting
        # PostgreSQL, a network blip) before the lease actually lapses.
        interval = self._tick_interval()
        while not dispatch_over.wait(interval):
            if lease_lost.is_set():
                # Renewing a lease we no longer hold writes nothing and asks
                # the database a question the ticker already answered.
                return
            try:
                alive = self._store.heartbeat(work)
            except Exception:  # noqa: BLE001 -- transient DB errors must not kill the beat
                logger.exception("heartbeat error for attempt %s", work.attempt_id)
                # Errors are not refusals, and timing them is not this
                # thread's job — the ticker holds the deadline, and holds it
                # for a hung beat as well as a loud one.
                continue
            if not alive:
                lease_lost.set()
                return
            lease.renewed_at = time.monotonic()
