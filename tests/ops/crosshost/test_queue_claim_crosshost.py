"""Cross-host proof for SRV-05 properties 12-13 -- the two runtime-platform
guarantees a single machine cannot exercise honestly: that a lease abandoned
by a `SIGKILL`ed worker on one real host is picked up by exactly one other
real host, and that a worker cut off from the queue cannot out-argue the
queue's own lease-expiry decision.

Property 12 is proven the same way the single-host suite
(`tests/ops/test_worker_systemd_runtime_platform.py`) proves the other
eleven: a disposable `systemd-run --user` transient unit and a real signal --
except the unit runs on `AICC_CROSSHOST_SSH_TARGET` (a second real host
reached over SSH), and the lease it holds lives in a Postgres database
(`AICC_CROSSHOST_PG_ADMIN_DSN`) both hosts reach, not a mock. This is the
genuinely cross-host dimension `tests/db/test_queue_claim.py`'s single-host
`_worker_hosts()` cannot exercise: that suite simulates "another host" with a
distinct Postgres role on the *same* kernel; this one uses a distinct kernel,
distinct clock, and a real SSH-bridged process tree.

Property 13 (network-partition arbitration) uses a userspace TCP forwarder
on the remote host as the partition primitive: the holder dials Postgres
through `127.0.0.1:<proxy>`, and stopping that forwarder leaves the holder
process alive with total loss of reachability to the queue while the rest of
its host -- and this host's direct path to the same database -- keep working.
That is deliberate. `IPAddressDeny=` on a `--user` unit is a silent no-op
without root (confirmed live: "unit configures an IP firewall, but not
running as root"), and unprivileged network namespaces are blocked on both
hosts by `kernel.apparmor_restrict_unprivileged_userns=1`. A paused or idle
process standing in for a partition is already covered on one host by
`tests/db/test_queue_claim.py`'s
`test_a_partitioned_owner_is_alive_and_still_refused`; this test needs the
holder still running and still trying to publish after real connectivity loss.
"""

from __future__ import annotations

import json
import socket
import time
import uuid

import pytest

from tests.ops.crosshost._driver import (
    dsn_host_port,
    kill_remote_unit,
    proxy_dsn,
    remote_unit_journal_json,
    remote_unit_name,
    remote_unit_show,
    run_probe_local,
    run_probe_remote,
    stage_probe_remote,
    start_remote_tcp_proxy,
    start_remote_transient_unit,
    stop_and_forget_remote_unit,
    unstage_probe_remote,
)

pytestmark = [pytest.mark.serial]

LOCAL_LABEL = socket.gethostname().split(".")[0]
VISIBILITY_SECONDS = 5


def _queue_name() -> str:
    return f"crosshost-verify-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def remote_claim_holder(remote_host):
    """Start a claim-and-hold probe on `remote_host` as a disposable transient
    `--user` unit; yields a helper to wait for its claim JSON. Always stopped
    and unstaged on teardown, matching `transient_unit`'s cleanup guarantee in
    the single-host suite."""
    remote_path = stage_probe_remote(remote_host)
    units: list[str] = []

    def _start(
        dsn: str,
        queue: str,
        *,
        hold_seconds: float = 60,
        extra_args: list[str] | None = None,
        unit_tag: str = "holder",
    ) -> str:
        unit = remote_unit_name(unit_tag)
        units.append(unit)
        argv = [
            "python3",
            remote_path,
            "claim",
            "--dsn",
            dsn,
            "--host-label",
            remote_host,
            "--queue",
            queue,
            "--visibility",
            str(VISIBILITY_SECONDS),
            "--hold-seconds",
            str(hold_seconds),
        ]
        if extra_args:
            argv.extend(extra_args)
        result = start_remote_transient_unit(remote_host, unit, [], argv)
        assert result.returncode == 0, f"systemd-run on {remote_host} failed: {result.stderr}"
        return unit

    def _await_phase_json(unit: str, phase: str, timeout: float = 15) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for entry in remote_unit_journal_json(remote_host, unit):
                message = entry.get("MESSAGE", "")
                if not message.startswith("{"):
                    continue
                payload = json.loads(message)
                # Pre-phase field: older probe builds only emitted claim JSON.
                if payload.get("phase", "claim") == phase:
                    return payload
            time.sleep(0.3)
        raise AssertionError(
            f"{unit} on {remote_host} never printed a {phase!r} result"
        )

    yield _start, _await_phase_json, remote_path

    for unit in units:
        stop_and_forget_remote_unit(remote_host, unit)
    unstage_probe_remote(remote_host, remote_path)


# --- 12a. Negative control: while the remote host's claim is alive and inside
# its visibility window, a second host claiming the same queue gets nothing.
# Without this, the positive case below could pass for the wrong reason (e.g.
# a claim that never actually excludes anyone).
def test_no_reclaim_while_the_remote_owner_is_alive(queue_dsn, remote_host, remote_claim_holder):
    queue = _queue_name()
    enqueued = run_probe_local(queue_dsn, LOCAL_LABEL, "enqueue", "--queue", queue, "--key", "only")
    assert enqueued["work_item_id"]

    start, await_phase, _remote_path = remote_claim_holder
    unit = start(queue_dsn, queue, hold_seconds=VISIBILITY_SECONDS + 5)
    remote_claim = await_phase(unit, "claim")
    assert remote_claim["ok"] is True
    assert remote_claim["work_item_id"] == enqueued["work_item_id"]

    local_attempt = run_probe_local(queue_dsn, LOCAL_LABEL, "claim", "--queue", queue)
    assert local_attempt["ok"] is False
    assert local_attempt["reason"] == "no_work"


# --- 12b. The positive case: SIGKILL the remote host's holder, let the
# visibility window lapse, reap from THIS host, and confirm exactly one other
# host -- this one -- picks the abandoned item back up. Then confirm the
# SIGKILLed host's original claim cannot retroactively complete it: the queue's
# bookkeeping, not either host's memory of what it once held, is authoritative.
def test_sigkill_on_remote_host_is_recovered_by_exactly_one_other_host(
    queue_dsn, remote_host, remote_claim_holder
):
    queue = _queue_name()
    enqueued = run_probe_local(queue_dsn, LOCAL_LABEL, "enqueue", "--queue", queue, "--key", "only")
    work_item_id = enqueued["work_item_id"]

    start, await_phase, _remote_path = remote_claim_holder
    unit = start(queue_dsn, queue, hold_seconds=60)
    remote_claim = await_phase(unit, "claim")
    assert remote_claim["ok"] is True
    stale_attempt_id = remote_claim["attempt_id"]
    stale_token = remote_claim["token"]

    kill_remote_unit(remote_host, unit)

    # Give SIGKILL time to land before asserting on it -- a live poll, not a
    # fixed sleep guess, mirroring the local suite's deadline-poll pattern.
    deadline = time.monotonic() + 5
    killed = False
    while time.monotonic() < deadline:
        state = remote_unit_show(remote_host, unit, "ActiveState", "SubState")
        if state.get("ActiveState") == "failed" or state.get("SubState") in ("failed", "dead"):
            killed = True
            break
        time.sleep(0.2)
    assert killed, f"{unit} on {remote_host} did not report as killed"

    time.sleep(VISIBILITY_SECONDS + 1)
    reaped = run_probe_local(queue_dsn, LOCAL_LABEL, "reap")
    assert reaped["reaped"] >= 1

    reclaimed = run_probe_local(queue_dsn, LOCAL_LABEL, "claim", "--queue", queue)
    assert reclaimed["ok"] is True
    assert reclaimed["work_item_id"] == work_item_id
    assert reclaimed["attempt_id"] != stale_attempt_id

    completed = run_probe_local(
        queue_dsn, LOCAL_LABEL, "complete",
        "--attempt-id", reclaimed["attempt_id"], "--token", reclaimed["token"],
    )
    assert completed["ok"] is True

    stale_completion = run_probe_remote(
        remote_host, queue_dsn, remote_host, "complete",
        "--attempt-id", stale_attempt_id, "--token", stale_token,
    )
    assert stale_completion["ok"] is False
    assert stale_completion["reason"] == "attempt_expired"

    inspected = run_probe_local(queue_dsn, LOCAL_LABEL, "inspect", "--work-item-id", work_item_id)
    assert inspected["state"] == "succeeded"
    assert inspected["attempt_count"] == 2


# --- 13. Network-partition arbitration: a still-running owner cut off from
# the queue cannot out-argue the queue's lease-expiry decision, and must not
# publish a result after the lease has moved on. Partition primitive is a
# userspace TCP forwarder on the remote host (see module docstring) -- stop
# the forwarder, leave the holder alive, let visibility lapse, reclaim from
# THIS host, and confirm the cut-off owner's later complete is refused.
def test_network_partition_cannot_out_argue_the_queues_lease_expiry(
    queue_dsn, remote_host, remote_claim_holder
):
    queue = _queue_name()
    enqueued = run_probe_local(
        queue_dsn, LOCAL_LABEL, "enqueue", "--queue", queue, "--key", "partitioned"
    )
    work_item_id = enqueued["work_item_id"]

    start, await_phase, remote_path = remote_claim_holder
    upstream_host, upstream_port = dsn_host_port(queue_dsn)
    proxy_unit, listen_port = start_remote_tcp_proxy(
        remote_host, remote_path, upstream_host, upstream_port
    )
    # Ensure the proxy unit is cleaned up even if the holder fixture's unit
    # list does not know about it (it is started outside `_start`).
    try:
        holder_dsn = proxy_dsn(queue_dsn, listen_port)
        # Hold long enough that the partition + visibility + reclaim all
        # finish before the cut-off owner attempts its complete.
        unit = start(
            holder_dsn,
            queue,
            hold_seconds=VISIBILITY_SECONDS + 10,
            extra_args=["--then-complete"],
        )
        remote_claim = await_phase(unit, "claim")
        assert remote_claim["ok"] is True
        assert remote_claim["work_item_id"] == work_item_id
        stale_attempt_id = remote_claim["attempt_id"]
        stale_token = remote_claim["token"]

        # Negative control: while the proxied claim is alive, this host gets
        # nothing -- same exclusion property 12a proves for an unproxied claim.
        blocked = run_probe_local(queue_dsn, LOCAL_LABEL, "claim", "--queue", queue)
        assert blocked["ok"] is False
        assert blocked["reason"] == "no_work"

        # The partition: holder keeps running, its path to the queue dies.
        partition_at = time.monotonic()
        kill_remote_unit(remote_host, proxy_unit, signal_name="SIGTERM")
        stop_and_forget_remote_unit(remote_host, proxy_unit)

        time.sleep(VISIBILITY_SECONDS + 1)
        reaped = run_probe_local(queue_dsn, LOCAL_LABEL, "reap")
        assert reaped["reaped"] >= 1

        reclaimed = run_probe_local(queue_dsn, LOCAL_LABEL, "claim", "--queue", queue)
        assert reclaimed["ok"] is True
        assert reclaimed["work_item_id"] == work_item_id
        assert reclaimed["attempt_id"] != stale_attempt_id

        completed = run_probe_local(
            queue_dsn,
            LOCAL_LABEL,
            "complete",
            "--attempt-id",
            reclaimed["attempt_id"],
            "--token",
            reclaimed["token"],
        )
        assert completed["ok"] is True
        decided_in = time.monotonic() - partition_at
        # Visibility is 5s; reclaim+complete add a little. Bound it so a
        # hung or consent-waiting path cannot silently pass.
        assert decided_in < VISIBILITY_SECONDS + 5, (
            f"queue took {decided_in:.2f}s to decide after partition; "
            "expected visibility-bound arbitration without the cut-off host's consent"
        )

        # Direct path (not through the dead proxy): the cut-off owner's
        # original token is refused by the queue's bookkeeping.
        stale_completion = run_probe_remote(
            remote_host,
            queue_dsn,
            remote_host,
            "complete",
            "--attempt-id",
            stale_attempt_id,
            "--token",
            stale_token,
            "--result",
            '{"ok": true, "from": "stale_direct"}',
        )
        assert stale_completion["ok"] is False
        assert stale_completion["reason"] == "attempt_expired"

        # The holder itself also tried to publish after the hold; that
        # attempt must not have succeeded (transport failure under partition
        # is fine; a successful complete would be the failure mode).
        holder_complete = await_phase(unit, "complete", timeout=VISIBILITY_SECONDS + 15)
        assert holder_complete["ok"] is False

        inspected = run_probe_local(
            queue_dsn, LOCAL_LABEL, "inspect", "--work-item-id", work_item_id
        )
        assert inspected["state"] == "succeeded"
        assert inspected["attempt_count"] == 2
        assert inspected["result_id"], "exactly one result, from the non-partitioned host"
    finally:
        stop_and_forget_remote_unit(remote_host, proxy_unit)
