# SRV-05 verified on real Linux systemd (VOYN-W0-AICC-SRV-05-LINUX-VERIFIED)

The worker service (`command_center/worker/`, `deploy/systemd/*.service`) is
built and unit-tested against `FakeSystemd`/mocked subprocess seams
everywhere except two places: the sd_notify wire protocol
(`tests/worker/test_sdnotify.py`, a real `AF_UNIX` datagram socket) and the
`/proc/<pid>/stat` ancestry read in the writer-lease gate
(`tests/worker/test_handlers.py::test_a_lease_held_by_our_own_supervisor_does_not_block`,
`skipif(not sys.platform.startswith("linux"))`). Both are silently skipped
on the primary macOS dev/CI path. This note records what running the suite
on an actual Linux host with a live systemd (PID 1, `systemd 255`) actually
proves, since a mocked pass and a real one are different claims.

## What ran for real

`pytest tests/worker/ tests/ops/test_aicc_staged_worker_rollout.py
tests/ops/test_aicc_install_transaction.py tests/orchestrator/test_self_deploy.py
tests/ops/test_agent_principal_isolation.py` — **373 passed, 0 skipped** on
this host. In particular:

- The Linux-only lease-ancestry test executed (not skipped): the real
  `/proc/<pid>/stat` parsing in `command_center/worker/writer_lease.py`
  round-trips against this kernel's actual `stat` format.
- `test_sdnotify.py` sent real datagrams over a real `AF_UNIX` socket and
  read the actual bytes back — the same syscalls `command_center/worker/sdnotify.py`
  makes against systemd's real `NOTIFY_SOCKET` in production.

## The 13 shipped unit files, checked with the real `systemd-analyze`

`deploy/systemd/*.service` is 13 files. `systemd-analyze verify` (the real
binary, not a mock) against each, unmodified, from this checkout:

- **4/13 verify clean**: `voyn-aicc-credential-rotation-alert@.service`,
  `voyn-aicc-credential-rotation.service`, `voyn-aicc-pgtunnel.service`,
  `voyn-aicc-self-deploy.service` — their `ExecStart` binaries
  (`/usr/bin/logger`, `/usr/bin/ssh`, `/bin/bash`) exist on any Linux base
  image, so this is a full pass: syntax, directive names/values, and
  resource-control settings all parse and are accepted by systemd itself.
- **9/13 report only a missing `ExecStart` binary** (e.g. `Command
  /opt/aicc/.venv/bin/python is not executable: No such file or directory`)
  — every other check (sandbox directives, `User`/`Group`, resource limits,
  ordering) is accepted; the one failure is that this checkout is not an
  installed `/opt/aicc` release, which is expected and not a unit-file
  defect. No unit reported a directive systemd itself rejects.

## Out of scope here, flagged for awareness

This host already runs two live instances of the template
(`voyn-aicc-worker@1.service`, `voyn-aicc-worker@2.service`, both
`active`/`running`). Reading their real effective properties
(`systemctl show`, read-only) shows they are currently running as
`User=voynadmin` with `NoNewPrivileges=no` — not the `User=aicc-worker`,
`NoNewPrivileges=yes` hardening that `deploy/systemd/voyn-aicc-worker@.service`
declares and that `ops/aicc_staged_worker_rollout.py:verify_unit_configuration`
enforces on rollout. Reconciling a live host's installed unit against the
checked-in template is a privileged deploy action outside a repository
checkout's reach (no code change here can fix it), so it is recorded rather
than acted on.
