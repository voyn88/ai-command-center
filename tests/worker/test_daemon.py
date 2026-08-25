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
    """Mutant E: with visibility_seconds=3600 the beat interval is 1200s, and
    the watchdog cap on that interval is the ONLY thing that keeps a healthy
    long-lease worker pinging inside its budget. Without the cap the first
    beat (and first in-run ping) would arrive 20 minutes late — here, never
    within the 10s bound."""
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
            "would have parked the beat thread for visibility/3 seconds"
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
