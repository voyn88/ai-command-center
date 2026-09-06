# SRV-05 Linux runtime platform: what's proven on real systemd

`VOYN-W0-AICC-SRV-05` ships the worker as a systemd unit
(`deploy/systemd/aicc-worker.service`, ported to the preprod lane template as
`voyn-aicc-worker@.service`). This records which of the runtime guarantees that
deployment leans on are mechanically proven against a real kernel and a real
systemd — not asserted in a comment, not asserted in a design doc — and which
one is not, because it needs root or a hardening exception on a host this
repo's test suite cannot assume it has.

The proof lives in
[`tests/ops/test_worker_systemd_runtime_platform.py`](../../tests/ops/test_worker_systemd_runtime_platform.py):
every test below spawns a disposable `systemd-run --user` transient unit (or,
for PDEATHSIG, a plain `fork()`), asserts the real outcome, and tears itself
down. Nothing here is installed as a system unit and nothing needs root — this
is deliberately the same non-privileged shape the worker itself runs under.
The suite self-skips (`skip_without_live_systemd`) unless
`systemd-run --user --wait -- /bin/true` actually succeeds on the host running
it, so it stays inert on a CI runner or container without a reachable
`systemd --user` session instead of failing there.

## Proven (12 of 13)

| # | Property | What the test proves |
|---|----------|------------------------|
| 1 | `systemd-analyze verify` | Both `aicc-worker.service` and `voyn-aicc-worker@.service` parse cleanly under the real systemd on the host (`rc=0`). `ExecStart`/`ExecStartPre`/`ExecReload` are stubbed to `/bin/true` first — the real paths under `/opt/aicc` are an install-time fact this dev host doesn't have, not a unit-syntax one. |
| 2 | `Restart=always` + `StartLimitBurst` | A unit that exits `0` every time still restarts on schedule and still trips `start-limit-hit` once `StartLimitBurst` is spent. A clean `exit(0)` is not an escape hatch from restart pacing — which is exactly why the worker can't take itself out of the fleet by exiting successfully. |
| 3 | `KillMode=control-group` vs `KillMode=process` | A unit that detaches a grandchild (`setsid`) and is then stopped: under `control-group` the grandchild dies with the rest of the cgroup; under `process` it survives, orphaned under the user manager. The worker's units don't set `KillMode=` explicitly, so they get systemd's `control-group` default — this proves that default actually does the cgroup-wide kill on this kernel. |
| 4 | `PR_SET_PDEATHSIG` | A process that registers `PR_SET_PDEATHSIG(SIGKILL)` against its parent is killed the instant that parent dies, even though only the parent was ever signalled. No systemd involved — pure kernel `prctl()` semantics. |
| 5 | `MemoryMax` | A run that stays under the declared cgroup ceiling completes normally; one that crosses it is killed by the kernel OOM killer and systemd reports `Result=oom-kill` — a kill, not a graceful refusal. |
| 6 | `TasksMax` | A cgroup task cap is enforced with `EAGAIN` on the `fork()` that would exceed it. |
| 7 | `LimitAS` (`RLIMIT_AS`) | An `mmap()` that would push the process's address space past the limit fails with `ENOMEM`; the same allocation succeeds under a generous limit. This is specifically a Linux claim — the same mechanism does not reliably cap a process's address space on macOS. |
| 8 | seccomp (`SystemCallFilter`) | Calling `mount(2)` as a non-root process without a filter fails gracefully with `EPERM` (DAC alone). Adding `SystemCallFilter=~mount ...` on the same call instead kills the process with `SIGSYS`. The filter is a harder boundary than the permission check underneath it: it terminates instead of returning an error the process could otherwise handle. |
| 9 | journald trusted fields | `_UID`, `_SYSTEMD_UNIT`, `_SYSTEMD_INVOCATION_ID` etc. come from the kernel/systemd side of the journal socket, not from parsing the message body — a message that merely *contains* the text `_UID=0` does not forge the trusted `_UID` field. |
| 10 | `StateDirectory` + `StateDirectoryMode` | The mode `aicc-worker.service` actually declares (`StateDirectoryMode=0700`, read from the unit file rather than hardcoded in the test) is the mode that lands on the created directory — the platform default is `0755`, so this is not a no-op assertion. |
| 11 | `Type=notify` + `WatchdogSec` | A handler that sends `READY=1` once and then stops pinging the watchdog is aborted (`SIGABRT`) once `WatchdogSec` elapses, and systemd reports `Result=watchdog`. This is the exact failure mode the worker's heartbeat thread (`command_center/worker/daemon.py`) exists to avoid triggering. |
| 12 | Cross-host failover consistency | A worker `SIGKILL`ed on a real second host over SSH has its claimed queue item picked up by *exactly one* other real host, against a shared Postgres database both hosts reach — not simulated, not mocked. The negative control comes first: while the remote holder is alive and inside its visibility window, a second host claiming the same queue gets `no_work`. Then: `SIGKILL` the remote holder, let the visibility window lapse, `queue_reap()` from the local host, and confirm the local host — and only the local host — reclaims the item; the killed host's original claim token cannot retroactively complete it (`attempt_expired`), because it's the queue's bookkeeping that's authoritative, not either host's memory of what it once held. See [`tests/ops/crosshost/test_queue_claim_crosshost.py`](../../tests/ops/crosshost/test_queue_claim_crosshost.py). |

Run the single-host suite directly on a host with a real `systemd --user` session:

```
pytest tests/ops/test_worker_systemd_runtime_platform.py -v
```

Run the cross-host suite (property 12) with a second real host and a shared
Postgres reachable from both:

```
AICC_CROSSHOST_SSH_TARGET=voyn-worker-01 \
AICC_CROSSHOST_PG_ADMIN_DSN=postgresql://... \
pytest tests/ops/crosshost/ -v
```

It self-skips (`remote_host` fixture) unless
`ssh $AICC_CROSSHOST_SSH_TARGET systemd-run --user --wait -- /bin/true`
actually succeeds and `AICC_CROSSHOST_PG_ADMIN_DSN` is set — the same
non-mockable bar the single-host suite sets for its own host.

## Not proven here (1 of 13): network-partition arbitration

When a worker is cut off from the queue, the queue's lease-expiry decision —
not the partitioned worker's local judgment — must be what determines
whether its claim is still valid, and the partitioned worker must not
publish a result after that. `tests/db/test_queue_claim.py`'s
`test_a_partitioned_owner_is_alive_and_still_refused` already proves the
queue-side half of this (a still-running, still-communicating owner is
refused once its lease lapses) on one host. What's missing is the other
half: an owner with real, total network connectivity loss while the rest of
its host — and the test process observing it — keeps working, which needs a
second host and a genuine partition, not a paused or idle process standing
in for one.

Two real constraints block that here, both confirmed by direct measurement
rather than assumed:

- `IPAddressDeny=` on a `systemd --user` unit is a silent no-op without root
  (`systemctl --user` reports "unit configures an IP firewall, but not
  running as root" and traffic is unaffected).
- Unprivileged network namespaces (`unshare --net --user`) are blocked by
  `kernel.apparmor_restrict_unprivileged_userns=1` on both hosts this repo
  has access to.

`tests/ops/crosshost/test_queue_claim_crosshost.py` carries the test for
this (`test_network_partition_cannot_out_argue_the_queues_lease_expiry`)
gated behind a live capability probe
(`skip_without_root_network_isolation`) rather than a hardcoded skip, so it
self-activates — and must then actually be implemented against the isolation
primitive — the moment either constraint lifts on a host with the SSH
partner this suite needs. Tracked as follow-up requiring root or a hardening
exception on both hosts.
