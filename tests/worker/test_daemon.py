"""The daemon's loop, driven by a fake store — no database, and only the
lost-lease test waits on a real beat interval (~1s).

What these tests deliberately do NOT cover: the SQL protocol itself, which is
already proven by tests/db/test_queue_claim.py against real PostgreSQL, and
the store wrapper's SQL, which the integration test covers. Here the store is
a script of answers, so each test pins one piece of loop behaviour and fails
for exactly one reason.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from command_center.db.work_queue_store import ClaimedWork, QueueRefusal
from command_center.worker.daemon import (
    HandlerOutcome,
    WorkerConfig,
    WorkerDaemon,
)


def _work(payload: dict, attempt_id: str = "wat-1") -> ClaimedWork:
    return ClaimedWork(
        work_item_id="wki-10",
        attempt_id=attempt_id,
        attempt_no=1,
        visible_until="2026-01-01T00:00:00+00:00",
        payload=payload,
        claim_token="token-plain",
    )


class ScriptedStore:
    """Answers claims from a script; records every protocol call."""

    def __init__(self, answers: list) -> None:
        self.answers = list(answers)
        self.calls: list[tuple] = []
        self.heartbeat_alive = True

    def claim(self, queue, *, visibility_seconds):
        self.calls.append(("claim", queue, visibility_seconds))
        if not self.answers:
            return QueueRefusal(reason="no_work")
        return self.answers.pop(0)

    def heartbeat(self, work):
        self.calls.append(("heartbeat", work.attempt_id))
        return self.heartbeat_alive

    def complete(self, work, result):
        self.calls.append(("complete", work.attempt_id, result))
        return True

    def fail(self, work, *, reason, retryable):
        self.calls.append(("fail", work.attempt_id, reason, retryable))
        return True

    def fail_lease_wait(self, work, *, reason):
        self.calls.append(("fail_lease_wait", work.attempt_id, reason))
        return True


def _run_until_idle(daemon: WorkerDaemon, store: ScriptedStore) -> None:
    """Run the loop until the script is exhausted, then stop it via the
    injected sleep — the daemon idles only when there is no work, so the
    first idle sleep is the natural end of a scripted run."""
    # sleep is called with the idle backoff; use it as the stop trigger
    daemon._sleep = lambda _t: daemon.request_stop()  # type: ignore[method-assign]
    daemon.run_forever()


def test_a_claimed_item_is_dispatched_and_completed() -> None:
    store = ScriptedStore([_work({"kind": "echo", "x": 1})])
    outcomes = []

    def echo(payload, lease_lost, attempt_no=1):
        outcomes.append(payload)
        return HandlerOutcome(ok=True, result={"echoed": payload["x"]})

    daemon = WorkerDaemon(store, {"echo": echo}, WorkerConfig(visibility_seconds=3))
    _run_until_idle(daemon, store)

    assert outcomes == [{"kind": "echo", "x": 1}]
    assert ("complete", "wat-1", {"echoed": 1}) in store.calls


def test_a_failing_handler_reports_fail_not_complete() -> None:
    store = ScriptedStore([_work({"kind": "boom"})])

    def boom(payload, lease_lost, attempt_no=1):
        return HandlerOutcome(ok=False, reason="did not work", retryable=True)

    daemon = WorkerDaemon(store, {"boom": boom}, WorkerConfig(visibility_seconds=3))
    _run_until_idle(daemon, store)

    assert ("fail", "wat-1", "did not work", True) in store.calls
    assert not any(c[0] == "complete" for c in store.calls)


def test_a_lease_wait_failure_routes_to_the_lease_wait_store_method() -> None:
    """VOYN-W0-AICC-PUBLISH-LEASE-CONTENTION-BURNS-ATTEMPT: a publish that
    lost the writer-lease race to a sibling lane names no fault in this
    item's own work. Reporting it through the ordinary `fail(retryable=True)`
    path would still count it against `max_attempts` (the queue already
    advanced `attempt_count` at claim), which is exactly what dead-lettered
    finished work live on 2026-09-06. `lease_wait=True` must route to the
    store's separate refund-and-bound path instead."""
    store = ScriptedStore([_work({"kind": "publish"})])

    def contended(payload, lease_lost, attempt_no=1):
        return HandlerOutcome(
            ok=False, reason="publish failed: lease_unavailable: held by x", lease_wait=True
        )

    daemon = WorkerDaemon(store, {"publish": contended}, WorkerConfig(visibility_seconds=3))
    _run_until_idle(daemon, store)

    assert ("fail_lease_wait", "wat-1", "publish failed: lease_unavailable: held by x") in (
        store.calls
    )
    assert not any(c[0] == "fail" for c in store.calls)


def test_a_raising_handler_is_a_retryable_failure() -> None:
    store = ScriptedStore([_work({"kind": "raise"})])

    def raiser(payload, lease_lost, attempt_no=1):
        raise RuntimeError("crashed")

    daemon = WorkerDaemon(store, {"raise": raiser}, WorkerConfig(visibility_seconds=3))
    _run_until_idle(daemon, store)

    fails = [c for c in store.calls if c[0] == "fail"]
    assert len(fails) == 1 and fails[0][3] is True  # retryable


def test_an_unknown_payload_kind_is_a_non_retryable_failure() -> None:
    """A payload nobody can execute will not become executable on retry;
    retrying it burns the attempt budget on the way to the same dead letter."""
    store = ScriptedStore([_work({"kind": "martian"})])
    daemon = WorkerDaemon(store, {}, WorkerConfig(visibility_seconds=3))
    _run_until_idle(daemon, store)

    fails = [c for c in store.calls if c[0] == "fail"]
    assert len(fails) == 1
    assert fails[0][3] is False  # not retryable
    assert "martian" in fails[0][2]


def test_a_lost_lease_discards_the_outcome() -> None:
    """After the database has given the attempt to someone else, reporting a
    result would be exactly the lost-update the protocol exists to prevent —
    so the daemon must report NOTHING."""
    store = ScriptedStore([_work({"kind": "slow"})])
    store.heartbeat_alive = False  # first beat discovers the lease is gone

    def slow(payload, lease_lost, attempt_no=1):
        # Wait until the heartbeat thread notices; then finish "successfully".
        assert lease_lost.wait(timeout=10), "heartbeat never signalled loss"
        return HandlerOutcome(ok=True, result={"too": "late"})

    daemon = WorkerDaemon(store, {"slow": slow}, WorkerConfig(visibility_seconds=3))
    _run_until_idle(daemon, store)

    assert not any(c[0] == "complete" for c in store.calls)
    assert not any(c[0] == "fail" for c in store.calls)


def test_sigterm_finishes_the_item_in_hand_and_claims_no_more() -> None:
    store = ScriptedStore(
        [_work({"kind": "echo"}), _work({"kind": "echo"}, attempt_id="wat-2")]
    )
    seen = []

    def echo(payload, lease_lost, attempt_no=1):
        seen.append(payload)
        return HandlerOutcome(ok=True, result={})

    daemon = WorkerDaemon(store, {"echo": echo}, WorkerConfig(visibility_seconds=3))

    original_execute = daemon._execute

    def execute_then_stop(work):
        original_execute(work)
        daemon.request_stop()  # the signal arrives while item 1 is in hand

    daemon._execute = execute_then_stop  # type: ignore[method-assign]
    daemon.run_forever()

    assert len(seen) == 1, "the second item must not be claimed after stop"
    assert ("complete", "wat-1", {}) in store.calls


def test_credential_hot_reload_does_not_wait_for_or_signal_the_running_job() -> None:
    """Pool generations change beside a 3600-second handler. The handler is
    neither stopped nor duplicated, while its heartbeat can move to the new
    pool on its next checkout."""

    import threading

    store = ScriptedStore([_work({"kind": "slow"})])
    events: list[str] = []
    pings: list[str] = []
    daemon: WorkerDaemon
    handler_started = threading.Event()
    handler_release = threading.Event()
    reload_done = threading.Event()

    def handler(payload, lease_lost, attempt_no=1):
        events.append("handler")
        handler_started.set()
        assert handler_release.wait(timeout=5)
        events.append("handler-finished")
        return HandlerOutcome(ok=True, result={})

    def reload_credentials() -> None:
        assert not handler_release.is_set(), "test must reload while job is active"
        events.append("reload")
        reload_done.set()

    def sleep(_seconds: float) -> None:
        daemon.request_stop()

    daemon = WorkerDaemon(
        store,
        {"slow": handler},
        WorkerConfig(visibility_seconds=3),
        sleep=sleep,
        notify=pings.append,
        reload_credentials=reload_credentials,
    )
    worker = threading.Thread(target=daemon.run_forever)
    worker.start()
    assert handler_started.wait(timeout=5)
    daemon.request_drain()
    deadline = time.monotonic() + 5
    while "STATUS=aicc-drained" not in pings and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "STATUS=aicc-drained" in pings
    daemon.request_reload()
    assert reload_done.wait(timeout=5)
    handler_release.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert events == ["handler", "reload", "handler-finished"]
    assert pings.count("READY=1") == 2
    assert "STATUS=aicc-drained" in pings
    assert ("complete", "wat-1", {}) in store.calls


def test_drain_ack_is_after_atomic_claim_gate_close() -> None:
    """A SIGUSR1-equivalent racing a blocking claim cannot ACK early."""
    import threading

    entered = threading.Event()
    release = threading.Event()
    calls = [0]
    notices: list[str] = []

    class BlockingStore(ScriptedStore):
        def claim(self, queue, *, visibility_seconds):
            calls[0] += 1
            entered.set()
            assert release.wait(timeout=5)
            return QueueRefusal(reason="no_work")

    store = BlockingStore([])
    daemon = WorkerDaemon(
        store,
        {},
        WorkerConfig(visibility_seconds=3, idle_min_seconds=0.01),
        notify=notices.append,
    )
    worker = threading.Thread(target=daemon.run_forever)
    worker.start()
    assert entered.wait(timeout=5)

    daemon.request_drain()
    time.sleep(0.05)
    assert "STATUS=aicc-drained" not in notices
    release.set()
    deadline = time.monotonic() + 5
    while "STATUS=aicc-drained" not in notices and time.monotonic() < deadline:
        time.sleep(0.01)
    assert "STATUS=aicc-drained" in notices
    time.sleep(0.05)
    assert calls[0] == 1, "no claim may begin after the drain ACK"
    daemon.request_stop()
    worker.join(timeout=5)
    assert not worker.is_alive()


class _FileLeaseStore:
    """Minimal cross-process lease used by the SIGTERM/SIGKILL regression."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def claim(self, queue, *, visibility_seconds):
        if (self.root / "done").exists():
            return QueueRefusal(reason="no_work")
        lease = self.root / "lease"
        now = time.monotonic()
        if lease.exists() and float(lease.read_text()) > now:
            return QueueRefusal(reason="no_work")
        attempts = self.root / "attempts"
        attempt_no = int(attempts.read_text()) + 1 if attempts.exists() else 1
        self._publish(attempts, str(attempt_no))
        attempt_id = f"attempt-{attempt_no}"
        self._publish(lease, str(now + float(visibility_seconds)))
        self._publish(self.root / "claimed", attempt_id)
        return ClaimedWork(
            work_item_id="job-3600",
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            visible_until="bounded-by-test-clock",
            payload={"kind": "long"},
            claim_token=f"token-{attempt_no}",
        )

    def heartbeat(self, work):
        self._publish(self.root / "lease", str(time.monotonic() + 1.0))
        return True

    def _publish(self, path, text):
        # Atomic rename: a SIGKILL landing mid-write must never leave an
        # empty/half file for the parent process's cross-process read
        # (review finding on c4001c4).
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_text(text)
        os.replace(temporary, path)

    def complete(self, work, result):
        self._publish(self.root / "done", work.attempt_id)
        return True

    def fail(self, work, *, reason, retryable):
        return False


def test_real_sigterm_boundary_then_bounded_sigkill_allows_lease_redelivery(
    tmp_path: Path,
) -> None:
    """Scaled systemd lifecycle: 3600s job survives TERM, then lease retries."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - assertions are in the supervising parent
        # Any exception escaping this block would let the CHILD continue the
        # pytest session -- duplicated run, interleaved capture, misleading
        # parent failure (independent-review finding on f7515b5). The child
        # only ever exits through os._exit.
        try:
            daemon = WorkerDaemon(
                _FileLeaseStore(tmp_path),
                {
                    "long": lambda payload, lost, attempt=1: (
                        (tmp_path / "handler-entered").write_text("1"),
                        time.sleep(3600),
                        HandlerOutcome(ok=True),
                    )[-1]
                },
                WorkerConfig(visibility_seconds=1, idle_min_seconds=0.01),
                notify=lambda _state: None,
            )
            daemon.install_signal_handlers()
            daemon.run_forever()
        except BaseException:
            os._exit(1)
        os._exit(0)

    reaped = False
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "claimed").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert (tmp_path / "claimed").read_text() == "attempt-1"
        while not (tmp_path / "handler-entered").exists() and (
            time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert (tmp_path / "handler-entered").exists(), "job never started"

        os.kill(pid, signal.SIGTERM)
        time.sleep(0.2)
        waited, _status = os.waitpid(pid, os.WNOHANG)
        # If the child died here it was ALSO reaped -- the finally must not
        # SIGKILL a recycled PID on this failure path (review on f7515b5).
        reaped = waited == pid
        assert waited == 0, "SIGTERM must let the in-hand 3600s job continue"

        kill_started = time.monotonic()
        os.kill(pid, signal.SIGKILL)
        waited, status = os.waitpid(pid, 0)
        reaped = True
        assert waited == pid and os.WIFSIGNALED(status)
        assert os.WTERMSIG(status) == signal.SIGKILL
        assert time.monotonic() - kill_started < 1.0

        # The killed owner cannot report. Once its bounded visibility lease
        # expires, the same item is safely delivered as attempt 2.
        # The child publishes the lease via atomic rename (write to a temp
        # name, os.replace) so a SIGKILL can never leave a half-written file
        # for this read (review finding on c4001c4).
        expiry = float((tmp_path / "lease").read_text())
        time.sleep(max(0.0, expiry - time.monotonic()) + 0.05)
        redelivered = _FileLeaseStore(tmp_path).claim("execution", visibility_seconds=1)
        assert isinstance(redelivered, ClaimedWork)
        assert redelivered.work_item_id == "job-3600"
        assert redelivered.attempt_no == 2
    finally:
        try:
            # Signalling an already-reaped PID is a PID-reuse hazard against
            # an unrelated process (review finding on c4001c4).
            if not reaped:
                os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            pass


def test_idle_backoff_grows_and_resets_on_work(monkeypatch) -> None:
    store = ScriptedStore(
        [QueueRefusal("no_work"), QueueRefusal("no_work"), _work({"kind": "echo"})]
    )
    sleeps: list[float] = []
    daemon = WorkerDaemon(
        store,
        {"echo": lambda p, e, a=1: HandlerOutcome(ok=True)},
        WorkerConfig(visibility_seconds=3, idle_min_seconds=1.0, idle_max_seconds=8.0),
    )
    monkeypatch.setattr(
        "command_center.worker.daemon.random",
        type("R", (), {"uniform": staticmethod(lambda a, b: 0.0)}),
    )

    def fake_sleep(t):
        sleeps.append(t)
        if len(sleeps) >= 4:  # two idles, work, then the post-script idle
            daemon.request_stop()

    daemon._sleep = fake_sleep  # type: ignore[method-assign]
    daemon.run_forever()

    assert sleeps[0] == 1.0 and sleeps[1] == 2.0, "backoff must grow while idle"
    # after real work the next idle starts from the floor again
    assert sleeps[2] == 1.0


def test_a_refused_report_is_logged_not_swallowed(caplog) -> None:
    """Review found the interleaving: DB down through the lease lapse,
    recovered before the handler finished — the report is refused as a stale
    owner, and the first version neither logged it nor knew. A daemon that
    silently loses an outcome re-runs side effects on retry with no trace."""
    import logging

    store = ScriptedStore([_work({"kind": "echo"})])

    def refuse_complete(work, result):
        store.calls.append(("complete", work.attempt_id, result))
        return False  # stale owner

    store.complete = refuse_complete  # type: ignore[method-assign]
    daemon = WorkerDaemon(
        store,
        {"echo": lambda p, e, a=1: HandlerOutcome(ok=True, result={})},
        WorkerConfig(visibility_seconds=3),
    )
    with caplog.at_level(logging.WARNING):
        _run_until_idle(daemon, store)
    assert any("report refused as stale owner" in r.message for r in caplog.records)


def test_a_raising_report_write_does_not_kill_the_daemon(caplog) -> None:
    """The handler admitted a real outcome (`HandlerOutcome(ok=True, ...)`),
    but persisting it raised -- a dropped connection, a driver that cannot
    encode the result, any DB hiccup mid-write. That must not propagate out
    of `run_forever` and kill the whole daemon over the one attempt it was
    reporting: every other item still waiting in the queue would die with it.
    The lease is left to lapse on its own; the next claim proves the daemon
    survived and kept working."""
    import logging

    store = ScriptedStore(
        [
            _work({"kind": "echo"}, attempt_id="wat-1"),
            _work({"kind": "echo"}, attempt_id="wat-2"),
        ]
    )

    def raising_complete(work, result):
        store.calls.append(("complete", work.attempt_id, result))
        if work.attempt_id == "wat-1":
            raise RuntimeError("connection reset")
        return True

    store.complete = raising_complete  # type: ignore[method-assign]
    daemon = WorkerDaemon(
        store,
        {"echo": lambda p, e, a=1: HandlerOutcome(ok=True, result={})},
        WorkerConfig(visibility_seconds=3),
    )
    with caplog.at_level(logging.ERROR):
        _run_until_idle(daemon, store)  # must return normally, not raise

    completes = [c for c in store.calls if c[0] == "complete"]
    assert [c[1] for c in completes] == ["wat-1", "wat-2"]
    assert any("writing the outcome raised" in r.message for r in caplog.records)


def test_a_non_object_payload_dead_letters_instead_of_killing_the_daemon() -> None:
    """queue_enqueue accepts any jsonb; a list payload used to raise
    AttributeError out of run_forever and kill the process over one item."""
    store = ScriptedStore([_work(["not", "an", "object"])])  # type: ignore[arg-type]
    daemon = WorkerDaemon(store, {}, WorkerConfig(visibility_seconds=3))
    _run_until_idle(daemon, store)

    fails = [c for c in store.calls if c[0] == "fail"]
    assert len(fails) == 1 and fails[0][3] is False
    assert "list" in fails[0][2]


def test_persistent_heartbeat_errors_stop_the_work() -> None:
    """Errors are not refusals, but after a full visibility window without one
    successful beat the lease has provably lapsed server-side — the handler
    must stop before its outcome becomes a stale write."""
    store = ScriptedStore([_work({"kind": "slow"})])

    def broken_heartbeat(work):
        store.calls.append(("heartbeat", work.attempt_id))
        raise ConnectionError("db unreachable")

    store.heartbeat = broken_heartbeat  # type: ignore[method-assign]

    def slow(payload, lease_lost, attempt_no=1):
        assert lease_lost.wait(timeout=30), "errors alone never signalled loss"
        return HandlerOutcome(ok=True, result={"too": "late"})

    daemon = WorkerDaemon(store, {"slow": slow}, WorkerConfig(visibility_seconds=3))
    _run_until_idle(daemon, store)
    assert not any(c[0] == "complete" for c in store.calls)


# -- the systemd watchdog seam (SRV-06) --------------------------------------


def test_the_claim_loop_feeds_the_watchdog_between_claims() -> None:
    """READY once at startup, a WATCHDOG ping before every claim, STOPPING at
    exit — the exact three states aicc-worker.service (Type=notify,
    WatchdogSec) supervises on."""
    store = ScriptedStore([_work({"kind": "echo"})])
    pings: list[str] = []
    daemon = WorkerDaemon(
        store,
        {"echo": lambda p, e, a=1: HandlerOutcome(ok=True, result={})},
        WorkerConfig(visibility_seconds=3),
        notify=pings.append,
    )
    _run_until_idle(daemon, store)

    assert pings[0] == "READY=1", "readiness must precede the first claim"
    assert pings[-1] == "STOPPING=1", "a clean exit must announce itself"
    watchdog = [p for p in pings if p == "WATCHDOG=1" or p.startswith("WATCHDOG=1\n")]
    claims = [c for c in store.calls if c[0] == "claim"]
    assert len(watchdog) == len(claims), "one ping per loop iteration"


def test_the_heartbeat_thread_feeds_the_watchdog_during_a_long_run() -> None:
    """While a handler blocks the claim loop, the ONLY thing still pinging is
    the heartbeat thread — pinned by THREAD IDENTITY, not by counting: review
    killed a counting version of this test (mutant C) because the claim loop
    itself supplies a third ping after the handler returns. The assertion
    holds even while the database refuses the beat (a DB outage is not a
    process wedge)."""
    import threading

    store = ScriptedStore([_work({"kind": "slow"})])
    store.heartbeat_alive = False  # the DB says the lease is gone
    ping_from_beat_thread = threading.Event()
    handler_running = threading.Event()

    def notify(state: str) -> None:
        if (
            state == "WATCHDOG=1"
            and handler_running.is_set()
            and threading.current_thread() is not threading.main_thread()
        ):
            ping_from_beat_thread.set()

    def slow(payload, lease_lost, attempt_no=1):
        handler_running.set()
        assert lease_lost.wait(timeout=10), "heartbeat never signalled loss"
        return HandlerOutcome(ok=True, result={"too": "late"})

    daemon = WorkerDaemon(
        store, {"slow": slow}, WorkerConfig(visibility_seconds=3), notify=notify
    )
    _run_until_idle(daemon, store)
    # visibility 3s -> beat interval 1s: while the handler held the main
    # thread, a WATCHDOG ping arrived from a thread that was not it.
    assert ping_from_beat_thread.is_set()


def test_the_watchdog_budget_caps_the_idle_sleep(monkeypatch) -> None:
    """WatchdogSec shorter than the idle backoff must shorten the sleep, not
    let a healthy idle worker miss its deadline and be shot by systemd."""
    monkeypatch.setenv("WATCHDOG_USEC", "8000000")  # 8s budget -> 4s interval
    import os

    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    store = ScriptedStore([QueueRefusal("no_work")] * 3)
    sleeps: list[float] = []
    daemon = WorkerDaemon(
        store,
        {},
        WorkerConfig(
            visibility_seconds=3, idle_min_seconds=16.0, idle_max_seconds=64.0
        ),
        notify=lambda _s: None,
    )

    def fake_sleep(t):
        sleeps.append(t)
        if len(sleeps) >= 3:
            daemon.request_stop()

    daemon._sleep = fake_sleep  # type: ignore[method-assign]
    daemon.run_forever()

    assert sleeps and all(t <= 4.0 for t in sleeps), sleeps


def test_the_watchdog_budget_caps_the_refusal_sleep(monkeypatch) -> None:
    """Mutant D2: the OTHER refusal branch (claim refused for a protocol
    reason, not no_work) sleeps idle_max — that sleep must be capped by the
    watchdog budget too, or a healthy worker parked on a refusal is shot."""
    import os

    monkeypatch.setenv("WATCHDOG_USEC", "8000000")  # 8s budget -> 4s interval
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    store = ScriptedStore([QueueRefusal("claim_refused")])
    sleeps: list[float] = []
    daemon = WorkerDaemon(
        store,
        {},
        WorkerConfig(
            visibility_seconds=3, idle_min_seconds=16.0, idle_max_seconds=64.0
        ),
        notify=lambda _s: None,
    )

    def fake_sleep(t):
        sleeps.append(t)
        if len(sleeps) >= 2:
            daemon.request_stop()

    daemon._sleep = fake_sleep  # type: ignore[method-assign]
    daemon.run_forever()

    assert sleeps and all(t <= 4.0 for t in sleeps), sleeps


def test_the_watchdog_cap_keeps_the_beat_alive_under_a_long_visibility(
    monkeypatch,
) -> None:
    """Mutant E: with visibility_seconds=3600 the beat interval is 900s
    (`beat_interval_seconds`, which the cadence fix moved from 1200s), and the
    watchdog cap on that interval is the ONLY thing that keeps a healthy
    long-lease worker pinging inside its budget. Without the cap the first
    beat (and first in-run ping) would arrive a quarter-hour late — here,
    never within the 10s bound."""
    import os
    import threading

    monkeypatch.setenv("WATCHDOG_USEC", "2000000")  # 2s budget -> 1s interval
    monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
    store = ScriptedStore([_work({"kind": "slow"})])
    beat_seen = threading.Event()
    original_heartbeat = store.heartbeat

    def observed_heartbeat(work):
        beat_seen.set()
        return original_heartbeat(work)

    store.heartbeat = observed_heartbeat  # type: ignore[method-assign]

    def slow(payload, lease_lost, attempt_no=1):
        assert beat_seen.wait(timeout=10), (
            "no heartbeat within the watchdog budget: the uncapped interval "
            "would have parked the beat thread for a quarter of the window"
        )
        return HandlerOutcome(ok=True, result={})

    daemon = WorkerDaemon(
        store,
        {"slow": slow},
        WorkerConfig(visibility_seconds=3600),
        notify=lambda _s: None,
    )
    _run_until_idle(daemon, store)
    assert beat_seen.is_set()


def test_the_daemon_speaks_real_sd_notify_datagrams(monkeypatch) -> None:
    """Mutant F (integration): no injected notifier — the daemon's default
    path must put real READY/WATCHDOG/STOPPING datagrams on the socket
    systemd names via NOTIFY_SOCKET."""
    import os
    import socket
    import tempfile

    path = os.path.join(tempfile.mkdtemp(prefix="sdn"), "n.sock")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    sock.bind(path)
    sock.setblocking(False)
    monkeypatch.setenv("NOTIFY_SOCKET", path)
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    try:
        store = ScriptedStore([_work({"kind": "echo"})])
        daemon = WorkerDaemon(
            store,
            {"echo": lambda p, e, a=1: HandlerOutcome(ok=True, result={})},
            WorkerConfig(visibility_seconds=3),
        )
        _run_until_idle(daemon, store)

        frames = []
        while True:
            try:
                frames.append(sock.recv(64))
            except BlockingIOError:
                break
    finally:
        sock.close()

    assert frames[0] == b"READY=1"
    assert frames[-1] == b"STOPPING=1"
    assert any(
        frame == b"WATCHDOG=1" or frame.startswith(b"WATCHDOG=1\n") for frame in frames
    )


def test_a_raising_claim_does_not_kill_the_lane(caplog) -> None:
    """THE AMPLIFIER (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED).

    `claim()` is the one protocol call the loop makes on EVERY iteration, and
    it was the only one with no guard around it -- while a raising report
    write, a raising handler and a non-object payload had each been closed
    after the same failure. It is the worst place to lack one: the exception
    fires before any item is in hand, so it does not cost one attempt, it
    costs every attempt. It leaves `run_forever`, the lane exits, systemd
    restarts it, and the next claim raises on the same row.

    That is how monitor_finding #2840 became a fleet-wide outage rather than
    one stuck item: a refunded lease wait left `attempt_no` taken, so
    `queue_claim` raised a unique violation -- on the OLDEST DUE ROW, which
    every lane selects first. Migration 0025 removed that cause; this removes
    the amplifier, which is the half the monitor actually measures. Lanes that
    are up but claiming nothing stop the fleet clock, and `control-01:queue`
    reports `queue_stalled` with no exit reachable by fleet action, because
    restarting a crash-looping lane only restarts the crash.

    The second claim is the assertion: it can only happen if the first one's
    exception did not leave the loop.
    """
    import logging

    store = ScriptedStore([_work({"kind": "echo"}, attempt_id="wat-2")])
    raised: list[str] = []
    real_claim = store.claim

    def raising_claim(queue, *, visibility_seconds):
        if not raised:
            raised.append(queue)
            # What a head-of-line poisoned row actually raised.
            raise RuntimeError(
                'duplicate key value violates unique constraint '
                '"idx_work_attempt_item_no"'
            )
        return real_claim(queue, visibility_seconds=visibility_seconds)

    store.claim = raising_claim  # type: ignore[method-assign]
    daemon = WorkerDaemon(
        store,
        {"echo": lambda p, e, a=1: HandlerOutcome(ok=True, result={})},
        WorkerConfig(visibility_seconds=3),
    )

    # `_run_until_idle` stops on the FIRST sleep, and the backoff after a
    # raising claim is a sleep -- so it would end the run before proving
    # anything. Stop on the second instead: one for the backoff, and the
    # daemon must reach a claim in between.
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 2:
            daemon.request_stop()

    daemon._sleep = sleep  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR):
        daemon.run_forever()  # must return normally, not raise

    # The lane survived and went back to claiming: the item behind the
    # poisoned row was served, which is the whole point.
    assert raised == ["execution"]
    assert ("complete", "wat-2", {}) in store.calls
    assert any("claim raised" in r.message for r in caplog.records)
    # Backed off rather than spun: the same interval every other unclaimable
    # answer uses, because none of them is cured by asking again faster.
    assert sleeps[0] == WorkerConfig().idle_max_seconds


def test_a_raising_claim_does_not_hold_the_drain_gate_through_its_backoff() -> None:
    """The backoff must sit OUTSIDE `_claim_gate_lock`. The drain coordinator
    takes that same lock to emit the `aicc-drained` ACK the credential rotator
    waits on, so sleeping under it would stall a rotation behind a database
    fault that has nothing to do with it -- trading a dead lane for a wedged
    one.

    Measured by taking the lock from the sleep itself: if the loop still held
    it, this could not acquire it.
    """
    store = ScriptedStore([])

    def always_raising_claim(queue, *, visibility_seconds):
        raise ConnectionError("server closed the connection unexpectedly")

    store.claim = always_raising_claim  # type: ignore[method-assign]
    daemon = WorkerDaemon(store, {}, WorkerConfig(visibility_seconds=3))

    gate_was_free: list[bool] = []

    def sleep(_seconds: float) -> None:
        acquired = daemon._claim_gate_lock.acquire(blocking=False)
        gate_was_free.append(acquired)
        if acquired:
            daemon._claim_gate_lock.release()
        daemon.request_stop()

    daemon._sleep = sleep  # type: ignore[method-assign]
    daemon.run_forever()

    assert gate_was_free == [True], "the claim gate stayed locked through the backoff"


def test_a_failed_credential_reload_does_not_strand_the_drained_lane() -> None:
    """A drained lane's ONLY route back to claiming is a successful reload.

    `request_drain()` (SIGUSR1) closes the claim gate and nothing reopens it
    except `_credential_reload_loop` clearing the flags after
    `reload_credentials()` returns. The request is cleared BEFORE the attempt
    -- deliberately, so a SIGHUP arriving mid-reload is coalesced rather than
    dropped -- so a reload that RAISES consumed the request and left nothing
    to retry it. The lane then stays drained for the life of the process:
    up, feeding the systemd watchdog from the draining branch (so no restart
    is ever triggered), holding a working pool, and claiming nothing.

    That is a `queue_stalled` with no exit reachable by fleet action, which
    is the failure class every fix on this branch has been closing -- the
    fleet clock stops, due ready work is attended by nobody, and restarting
    is not something anything on the host will do for a unit that looks
    healthy. The reload must degrade the way the claim and report paths
    already do: log it, back off, keep trying.
    """
    import threading

    attempts = []
    notices: list[str] = []
    fail_until = 2

    def reload_credentials() -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) <= fail_until:
            raise RuntimeError("pool rebuild refused: connection reset")

    store = ScriptedStore([])
    daemon = WorkerDaemon(
        store,
        {},
        # `idle_max_seconds` doubles as the reload retry interval, exactly as
        # it does for the claim and refusal paths.
        WorkerConfig(visibility_seconds=3, idle_min_seconds=0.01, idle_max_seconds=0.01),
        notify=notices.append,
        reload_credentials=reload_credentials,
    )
    worker = threading.Thread(target=daemon.run_forever)
    worker.start()
    try:
        daemon.request_drain()
        deadline = time.monotonic() + 5
        while "STATUS=aicc-drained" not in notices and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "STATUS=aicc-drained" in notices

        claims_while_drained = sum(call[0] == "claim" for call in store.calls)
        daemon.request_reload()

        # The lane comes back on its own: the failed reloads are retried, and
        # the first one that succeeds reopens the gate.
        deadline = time.monotonic() + 5
        while "STATUS=aicc-ready" not in notices[-3:] and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(attempts) > fail_until, (
            f"a failed reload was never retried: {attempts}"
        )
        assert "STATUS=aicc-reload-failed" in notices

        deadline = time.monotonic() + 5
        while (
            sum(call[0] == "claim" for call in store.calls) <= claims_while_drained
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert sum(call[0] == "claim" for call in store.calls) > claims_while_drained, (
            "the lane never resumed claiming after the reload finally succeeded"
        )
    finally:
        daemon.request_stop()
        worker.join(timeout=5)
    assert not worker.is_alive()


def test_a_failing_credential_reload_keeps_the_claim_gate_shut() -> None:
    """Retrying must not become failing open.

    The drain is the rotator's instruction to stop claiming while the
    credential it issued is retired; a lane that reopened the gate because
    its reload kept failing would claim under exactly the credential the
    rotation is invalidating. So the retry loop reopens the gate only on a
    reload that actually SUCCEEDED -- while the reload keeps failing the lane
    stays drained and claims nothing, which is the safe half of this
    behaviour and the half a naive "clear the flags in a finally" would lose.
    """
    import threading

    notices: list[str] = []
    attempts = []

    def reload_credentials() -> None:
        attempts.append(1)
        raise RuntimeError("credential file unreadable")

    store = ScriptedStore([])
    daemon = WorkerDaemon(
        store,
        {},
        WorkerConfig(visibility_seconds=3, idle_min_seconds=0.01, idle_max_seconds=0.01),
        notify=notices.append,
        reload_credentials=reload_credentials,
    )
    worker = threading.Thread(target=daemon.run_forever)
    worker.start()
    try:
        daemon.request_drain()
        deadline = time.monotonic() + 5
        while "STATUS=aicc-drained" not in notices and time.monotonic() < deadline:
            time.sleep(0.01)
        assert "STATUS=aicc-drained" in notices
        claims_at_drain = sum(call[0] == "claim" for call in store.calls)

        daemon.request_reload()
        deadline = time.monotonic() + 3
        while len(attempts) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(attempts) >= 3, f"the reload was not retried: {attempts}"

        assert sum(call[0] == "claim" for call in store.calls) == claims_at_drain, (
            "a lane whose reload keeps failing must not claim again"
        )
        assert "READY=1" not in notices[1:], "a failed reload must not re-arm READY"
    finally:
        daemon.request_stop()
        worker.join(timeout=5)
    assert not worker.is_alive()


# ---------------------------------------------------------------------------
# VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED (monitor_finding #13420): the lease
# had no renewal margin at all.
#
# `_heartbeat_loop` waited `visibility_seconds / 3` between beats under a
# comment promising that "two consecutive beats may fail (a restarting
# PostgreSQL, a network blip) before the lease actually lapses". Three beats
# at a third of the window land at V/3, 2V/3 and V EXACTLY -- so after two
# failures the beat that has to succeed arrives ON the deadline, and
# `_queue_owns` refuses it (`claim_expired`, a `<=` test). The delivered
# tolerance was ONE.
#
# These pin the tolerance itself rather than the divisor, so a future change
# to either number has to keep the promise or go red.
# `tests/db/test_infra_monitor_queue_snapshot.py` proves the same property end
# to end against a real server -- the real `queue_heartbeat` keeping the real
# lease, then the probe reading the lane it leaves behind; these need no
# server and so run in every gate on every machine -- the lesson
# `tests/db/test_reap_bound.py` was written for.


def test_the_lease_survives_the_beats_the_daemon_says_may_fail() -> None:
    """THE REGRESSION, as arithmetic over the window the deployment runs and
    every other window the clamps do not reach.

    A successful beat at T renews the lease to T + V. The beat that must
    succeed is the one after the tolerated failures, at T + (F + 1) * I, and
    it has to land while the lease is still LIVE -- `visible_until <= now()`
    is refused, so landing exactly on the deadline is landing late.
    """
    from command_center.worker.daemon import (
        TOLERATED_FAILED_BEATS as tolerated,
    )
    from command_center.worker.daemon import (
        beat_interval_seconds,
    )

    for window in (8, 12, 60, WorkerConfig().visibility_seconds, 600, 3600):
        interval = beat_interval_seconds(window)
        first_required_beat = (tolerated + 1) * interval
        assert first_required_beat < window, (
            f"visibility_seconds={window}: the beat after {tolerated} failures "
            f"lands at {first_required_beat}s against a {window}s lease"
        )
        # ... and with a whole beat to spare, because every term drifts the
        # same way: `beat_stop.wait(interval)` restarts after the PREVIOUS
        # call returned, so each cycle costs the round trip too.
        assert first_required_beat + interval <= window, (
            f"visibility_seconds={window}: no margin left after "
            f"{tolerated} failed beats"
        )


def test_the_configured_window_is_one_the_server_will_actually_grant() -> None:
    """The arithmetic above is about the window the CALLER ASKS FOR; the lease
    is the one the SERVER GRANTS. `queue_claim` clamps the request to
    ``[1, 3600]`` (`least(greatest(coalesce(p_visibility_seconds, 60), 1),
    3600)`), so a configured window above the ceiling would size the beat to a
    lease that does not exist -- 7200s asked for, 3600s kept, beats at 1800s,
    and the tolerance this module just bought back is gone again without a
    line of it changing.

    Nothing can reach that today: `command_center.worker.__main__` builds the
    daemon with the default config and exposes no knob. This pins the reason
    it is safe, so a knob added later lands here instead of in the fleet."""
    assert 1 <= WorkerConfig().visibility_seconds <= 3600


def test_the_deployed_window_beats_often_enough_to_survive_a_tunnel_restart() -> None:
    """The same property at the number the fleet actually runs, stated
    concretely so the regression reads as a fleet fact and not as algebra.

    `voyn-aicc-worker@.service` declares `Requires=voyn-aicc-pgtunnel.service`
    and the credential rotation restarts that tunnel on its own timer, so a
    blip costing two beats is a scheduled event, not a coincidence.
    """
    from command_center.worker.daemon import beat_interval_seconds

    window = WorkerConfig().visibility_seconds
    interval = beat_interval_seconds(window)
    beats_before_expiry = [n * interval for n in (1, 2, 3)]

    assert max(beats_before_expiry) < window, (
        f"beats at {beats_before_expiry} against a {window}s lease"
    )


def test_the_watchdog_cap_can_only_shorten_the_beat() -> None:
    """The cap exists so a long window cannot park the beat thread past the
    systemd watchdog deadline. It must never LENGTHEN the interval, which
    would silently spend the tolerance this module just bought back."""
    from command_center.worker.daemon import beat_interval_seconds

    window = 3600
    uncapped = beat_interval_seconds(window)

    assert beat_interval_seconds(window, 1.0) == 1.0
    assert beat_interval_seconds(window, uncapped * 10) == uncapped
    # The floor is the same clamp `queue_claim` puts on the window itself.
    assert beat_interval_seconds(1, 0.0) == 1.0
    assert beat_interval_seconds(0.5) == 1.0


def test_a_blip_of_the_tolerated_length_does_not_stop_the_work() -> None:
    """The loop itself, against a store that keeps the lease the way the
    server keeps it.

    `queue_heartbeat` renews `visible_until` to `now() + visibility_seconds`
    and `_queue_owns` refuses a beat that arrives when `visible_until <=
    now()` -- landing ON the deadline is landing late. This fake enforces
    exactly those two rules and nothing else, so the beat that recovers from
    the blip is judged by the clock rather than by the fake's goodwill. Under
    `visibility_seconds / 3` the third beat lands at the deadline, the store
    refuses it, and the handler's run is thrown away.

    A blip of `TOLERATED_FAILED_BEATS` beats is what a
    `voyn-aicc-pgtunnel.service` restart does to a lane, and the credential
    rotation restarts that tunnel on its own timer.
    """
    import threading

    from command_center.worker.daemon import TOLERATED_FAILED_BEATS

    window = 8.0
    store = ScriptedStore([_work({"kind": "slow"})])
    beats: list[str] = []
    recovered = threading.Event()
    claimed_at = time.monotonic()
    visible_until = [claimed_at + window]

    def leased_heartbeat(work):
        if len(beats) < TOLERATED_FAILED_BEATS:
            beats.append("raised")
            raise ConnectionError("tunnel restarting")
        now = time.monotonic()
        if visible_until[0] <= now:
            # `_queue_owns`: 'claim_expired'. The attempt is forfeit even
            # though nobody has taken it yet.
            beats.append("refused")
            recovered.set()
            return False
        visible_until[0] = now + window
        beats.append("ok")
        recovered.set()
        return True

    store.heartbeat = leased_heartbeat  # type: ignore[method-assign]

    def slow(payload, lease_lost, attempt_no=1):
        assert recovered.wait(timeout=window * 3), f"no beat recovered: {beats}"
        assert not lease_lost.is_set(), (
            f"{TOLERATED_FAILED_BEATS} failed beats gave the lease up: {beats}"
        )
        return HandlerOutcome(ok=True, result={"ok": True})

    daemon = WorkerDaemon(store, {"slow": slow}, WorkerConfig(visibility_seconds=window))
    _run_until_idle(daemon, store)

    assert beats[:TOLERATED_FAILED_BEATS] == ["raised"] * TOLERATED_FAILED_BEATS
    assert "refused" not in beats, f"the recovery beat arrived too late: {beats}"
    assert any(call[0] == "complete" for call in store.calls), (
        "the attempt was thrown away by a blip it was sized to survive"
    )


# ---------------------------------------------------------------------------
# VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED (monitor_finding #14016).
#
# TWO LEASES COVER ONE ATTEMPT and only one of them forbids the report. The
# queue's visibility lease is this daemon's; the repository WRITER lease is
# held by `worker.handlers` through `writer_lease.hold(repository, lease_cfg,
# lease_lost)` -- handed THIS loop's event on purpose, so that one cancellation
# path serves both ("a renewal failure sets the SAME `lease_lost` event this
# function already wires into `run_claude_code` as `cancel_event`").
#
# Sharing the cancellation channel is right. Sharing it with "do not report"
# was not: `_execute` read any set event as "the database already gave this
# attempt to someone else", which is true of exactly one of the two senders.
# ---------------------------------------------------------------------------


def test_a_run_cancelled_by_the_writer_lease_is_reported_not_discarded() -> None:
    """THE REGRESSION. The writer lease goes; the queue lease is live and
    renewing. `queue_fail` would be ACCEPTED, so it must be sent.

    Discarding it instead left the item `claimed` under a lease nothing renews
    any more -- and `visible_until` is still in the FUTURE, so for the rest of
    that window `control-01:queue` reads the row as an ATTENDED claim (the
    measurement's own words for "a lane doing its job") while `_execute` has
    returned and the loop has gone back to claiming. That inflates
    `queue.claimed`, which is what `--claim-capacity` is weighed against
    (monitor_finding #13366), so `evaluate` excuses due ready work as
    backpressure behind occupancy that does not exist -- the fail-OPEN
    direction a fail-closed monitor must never have.

    The blip is one the fleet runs itself: `voyn-lease` reaches its authority
    over `/etc/aicc/lease.env`'s DSN, the credential rotation restarts that
    tunnel on its own timer, and `writer_lease._renew_loop` gives up on the
    FIRST failed renewal where the beat loop survives
    `TOLERATED_FAILED_BEATS`.
    """
    store = ScriptedStore([_work({"kind": "slow"})])
    store.heartbeat_alive = True  # the QUEUE lease is healthy throughout

    def slow(payload, lease_lost, attempt_no=1):
        # What `writer_lease._renew_loop` does on a failed renewal: set the
        # event the handler was handed, which cancels the agent.
        lease_lost.set()
        return HandlerOutcome(
            ok=False, reason="writer lease renewal failed", retryable=True
        )

    daemon = WorkerDaemon(store, {"slow": slow}, WorkerConfig(visibility_seconds=3))
    _run_until_idle(daemon, store)

    failures = [call for call in store.calls if call[0] == "fail"]
    assert failures == [("fail", "wat-1", "writer lease renewal failed", True)], (
        "a decided, writable outcome was discarded; the item stays `claimed` "
        f"for a whole visibility window: {store.calls}"
    )
    # And no heartbeat ever said the queue lease was gone, which is the whole
    # point -- nothing about THIS lease changed.
    assert store.heartbeat_alive is True


def test_a_cancelled_run_is_never_completed_even_if_the_handler_says_so() -> None:
    """Fail closed over the handler's own verdict, and only here.

    Reporting a cancelled run is correct; reporting it as SUCCESS is not. A
    cancelled agent's workspace is whatever a forcibly terminated process left
    behind, and `queue_complete` is terminal and carries a result. The daemon
    must not take a handler's word for that write -- `worker.handlers` already
    returns a retryable failure on this path, and a handler that honours
    `lease_lost` by returning anyway must not be able to ack the item.
    """
    store = ScriptedStore([_work({"kind": "slow"})])

    def slow(payload, lease_lost, attempt_no=1):
        lease_lost.set()
        return HandlerOutcome(ok=True, result={"published": "maybe"})

    daemon = WorkerDaemon(store, {"slow": slow}, WorkerConfig(visibility_seconds=3))
    _run_until_idle(daemon, store)

    assert not any(call[0] == "complete" for call in store.calls), store.calls
    failures = [call for call in store.calls if call[0] == "fail"]
    assert len(failures) == 1, store.calls
    assert failures[0][3] is True, "a cancelled run must come back for retry"
    assert "after the handler reported success" in failures[0][2]


def test_a_lost_queue_lease_is_recorded_before_the_handler_is_released() -> None:
    """The ordering the distinction rests on, pinned where it lives.

    `_execute` tells the two senders apart by reading a SECOND event that only
    the beat loop sets. `cancel` is what the handler waits on, so the moment
    it is set the handler may return and `_execute` may read that second
    event -- which must therefore already be true, or a lost QUEUE lease
    would be mistaken for a lost writer lease and reported anyway, which is
    the lost update the protocol exists to prevent.

    `WorkerDaemon._lose_queue_lease` is that ordering and nothing else, so
    this asserts it directly rather than racing two threads for it.
    """
    import threading

    order: list[str] = []
    queue_lease_lost = threading.Event()

    class RecordingCancel(threading.Event):
        def set(self) -> None:
            # What the handler would see at the instant it is released.
            order.append("queue_lease_lost" if queue_lease_lost.is_set() else "cancel")
            super().set()

    WorkerDaemon._lose_queue_lease(queue_lease_lost, RecordingCancel())

    assert order == ["queue_lease_lost"], (
        "the handler can be released before the daemon knows which lease went"
    )
    assert queue_lease_lost.is_set()


def test_a_lost_queue_lease_still_discards_the_outcome() -> None:
    """The half that must not change, restated against the new split: when the
    BEAT LOOP is the one that gives up, the attempt is already somebody else's
    (or the reaper's) and `_queue_owns` would refuse the write. Nothing is
    reported -- not even the retryable failure a writer-lease cancellation now
    gets."""
    store = ScriptedStore([_work({"kind": "slow"})])
    store.heartbeat_alive = False  # the first beat discovers the lease is gone

    def slow(payload, lease_lost, attempt_no=1):
        assert lease_lost.wait(timeout=10), "heartbeat never signalled loss"
        return HandlerOutcome(
            ok=False, reason="stopped on a lost lease", retryable=True
        )

    daemon = WorkerDaemon(store, {"slow": slow}, WorkerConfig(visibility_seconds=3))
    _run_until_idle(daemon, store)

    assert not any(call[0] == "complete" for call in store.calls), store.calls
    assert not any(call[0] == "fail" for call in store.calls), store.calls
    assert not any(call[0] == "fail_lease_wait" for call in store.calls), store.calls


# ---------------------------------------------------------------------------
# VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED: the beat loop's tolerance was a
# COUNT OF BEATS wearing a clock's clothes.
#
# `_heartbeat_loop` gave up on the queue lease when
# `consecutive_errors * interval >= visibility_seconds`. That product is
# elapsed time only if a heartbeat that cannot reach the server fails in
# roughly nothing -- and the failure the tolerance exists for does the
# opposite. The beat crosses `voyn-aicc-pgtunnel.service`, which the credential
# rotation restarts on its own timer; a severed forwarded socket BLACK-HOLES
# the call, and nothing client-side bounds the read (`connect_timeout` bounds
# connecting, `statement_timeout` is enforced by the server that is no longer
# reachable). Each failed cycle therefore cost `interval + block` while the
# counter still demanded `F + 2` of them.
#
# The cost of overrunning is the lane executing an item it no longer owns,
# which is the probe's occupancy lied to from a third direction (after
# monitor_finding #13366 and #14016): `aicc-queue-reaper.timer` hands the item
# back, the fleet holds two lanes' worth of work, `work_item` records one
# claim, `spare_capacity` goes true and `queue_stalled` opens against a fleet
# with no free lane. `tests/db/test_infra_monitor_queue_snapshot.py` measures
# that half against a real server; these need none and so run in every gate.
#
# They drive the loop through the INJECTED clock rather than waiting the
# window out, because the property is about the deployed 300s window and a
# test that has to sleep through it would have to pick a different one.
# ---------------------------------------------------------------------------


class _FakeClock:
    """A monotonic clock a test advances, and a `beat_stop.wait` that advances
    it by the beat interval -- so the loop's own pacing moves time exactly as
    it does in production, and only the blocked call has to be scripted."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _beat_until_lost(
    window: int, block: float, beats: int = 40
) -> tuple[float, list[float]]:
    """Run the real `_heartbeat_loop` against a heartbeat that hangs for
    `block` seconds of clock time and then fails, for ever.

    Returns ``(when the queue lease was declared lost, when each beat was
    attempted)`` on the injected clock. `beats` bounds the run so a loop that
    never gives up fails as a timeout-free assertion instead of hanging.
    """
    import threading

    clock = _FakeClock()
    attempts: list[float] = []
    lost_at: list[float] = []
    queue_lease_lost = threading.Event()
    cancel = threading.Event()
    beat_stop = threading.Event()

    class HangingStore:
        def heartbeat(self, work):
            attempts.append(clock.now)
            if len(attempts) >= beats:
                beat_stop.set()
            # The call does not answer; it parks on a dead socket and then
            # reports the failure it has been sitting on all along.
            clock.advance(block)
            raise ConnectionError("tunnel black-holed")

    class _WaitForTheBeat:
        """`beat_stop.wait(interval)`: real pacing on the fake clock."""

        def __init__(self) -> None:
            self.calls = 0

        def wait(self, seconds: float) -> bool:
            if beat_stop.is_set():
                return True
            clock.advance(seconds)
            return False

        def is_set(self) -> bool:
            return beat_stop.is_set()

    daemon = WorkerDaemon(
        HangingStore(),
        {},
        WorkerConfig(visibility_seconds=window),
        monotonic=clock,
    )
    original_set = cancel.set

    def record_and_set() -> None:
        lost_at.append(clock.now)
        original_set()

    cancel.set = record_and_set  # type: ignore[method-assign]
    daemon._heartbeat_loop(
        _work({"kind": "slow"}), queue_lease_lost, cancel, _WaitForTheBeat()
    )
    assert queue_lease_lost.is_set(), (
        f"the loop never gave the lease up in {len(attempts)} beats "
        f"across {clock.now}s of a {window}s lease: {attempts}"
    )
    assert lost_at, "the handler was never cancelled"
    return lost_at[0], attempts


def test_the_lease_is_given_up_within_its_window_however_slowly_a_beat_fails() -> None:
    """THE REGRESSION. The loop must stop the handler within one visibility
    window of the last beat the server accepted -- no matter how long each
    failing beat takes to admit it.

    A fast failure was always handled: four cycles of `interval` are exactly
    one window at `beat_interval_seconds`. The black-holed call was not, and
    it is the one the tolerance is for.
    """
    window = WorkerConfig().visibility_seconds

    for block in (0.0, 1.0, 10.0, 120.0, 900.0):
        lost_at, attempts = _beat_until_lost(window, block)
        # ONE WINDOW PLUS AT MOST THE ONE CALL THE LOOP WAS PARKED IN, which
        # is the whole of what this thread can promise: while it is inside
        # `store.heartbeat` it cannot act, and bounding that needs a deadline
        # on the TRANSPORT (libpq `tcp_user_timeout` and friends) rather than
        # on this loop. The point is that the bound is now the WINDOW plus one
        # call instead of `TOLERATED_FAILED_BEATS + 2` of them.
        assert lost_at <= window + block, (
            f"a beat that hangs {block}s let the handler run "
            f"{lost_at - window:.0f}s past a {window}s lease, more than the "
            f"one call it was parked in (beats attempted at {attempts})"
        )
        # Not early either: an attempt taken away with time left on its lease
        # costs the whole run (migration 0029), so the loop may only give up
        # once the lapse is PROVEN -- which is at the window, not before it.
        assert lost_at >= window, (
            f"a beat that hangs {block}s gave the lease up at {lost_at}s, "
            f"ahead of its {window}s deadline"
        )


def test_counting_beats_is_not_measuring_the_window() -> None:
    """The defect stated as the arithmetic it was, so the fix cannot quietly
    revert to it.

    `consecutive_errors * interval >= visibility_seconds` needs
    `TOLERATED_FAILED_BEATS + 2` failures at the derived cadence. Each costs
    `interval` PLUS however long the call sat there, so the old test fired at
    `(F + 2) * (interval + block)` -- four times the window at the 120s block
    `lease_client`'s own subprocess timeout allows, and the overrun grows
    without bound as the block does.
    """
    from command_center.worker.daemon import (
        TOLERATED_FAILED_BEATS,
        beat_interval_seconds,
    )

    window = WorkerConfig().visibility_seconds
    interval = beat_interval_seconds(window)
    required_failures = TOLERATED_FAILED_BEATS + 2

    for block in (10.0, 120.0):
        counted = required_failures * (interval + block)
        assert counted > window, (
            "this test no longer describes the old defect: counting "
            f"{required_failures} failures at a {block}s block gives "
            f"{counted}s, which is inside the {window}s window"
        )
        measured, _attempts = _beat_until_lost(window, block)
        assert measured < counted, (
            f"the loop still fires on the beat COUNT ({counted}s) rather than "
            f"on the window ({window}s): it gave up at {measured}s"
        )


def test_a_beat_the_server_accepted_moves_the_deadline() -> None:
    """The other half: the window is measured from the last beat the SERVER
    accepted, so a fleet whose database comes back keeps its attempt.

    One failure, one success, then silence -- the successful beat has to
    re-date the deadline, or the next failure would be judged against a window
    that started before it and the lease would be given up with most of it
    left.
    """
    import threading

    from command_center.worker.daemon import beat_interval_seconds

    window = 300
    clock = _FakeClock()
    interval = beat_interval_seconds(window)
    answers = [None, True]  # None raises; True is `queue_heartbeat` ok
    lost_at: list[float] = []

    class FlakyStore:
        def heartbeat(self, work):
            if answers:
                answer = answers.pop(0)
                if answer is None:
                    raise ConnectionError("one blip")
                return answer
            raise ConnectionError("gone for good")

    class _WaitForTheBeat:
        def wait(self, seconds: float) -> bool:
            clock.advance(seconds)
            return False

        def is_set(self) -> bool:
            return False

    cancel = threading.Event()
    original_set = cancel.set

    def record_and_set() -> None:
        lost_at.append(clock.now)
        original_set()

    cancel.set = record_and_set  # type: ignore[method-assign]
    daemon = WorkerDaemon(
        FlakyStore(), {}, WorkerConfig(visibility_seconds=window), monotonic=clock
    )
    daemon._heartbeat_loop(
        _work({"kind": "slow"}), threading.Event(), cancel, _WaitForTheBeat()
    )

    # The successful beat landed at 2 * interval and renewed the lease there,
    # so the lapse is proven only a whole window after THAT -- not a window
    # after the claim.
    accepted_at = 2 * interval
    assert lost_at and lost_at[0] >= accepted_at + window, (
        f"the lease was given up at {lost_at}s; the beat the server accepted "
        f"at {accepted_at}s renews it to {accepted_at + window}s"
    )
