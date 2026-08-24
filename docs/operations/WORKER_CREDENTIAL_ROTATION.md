# Worker credential rotation

`command_center.ops.credential_rotation` is the only supported worker-host
rotation path. It replaces the untracked `~/aicc-preprod/rotate.py`.

## Safety protocol

1. The controller takes a non-blocking host lock, requires `worker.env`, waits
   (with bounded exponential backoff) for the tunnel socket, and asks the
   database for the proved current credential's expiry plus the database clock.
   It refuses to close a claim gate unless that server-authoritative remaining
   lifetime covers drain, mutation and a complete reopen of both lanes.
2. It requires both worker lanes to be `aicc-ready`, sends each lane `SIGUSR1`,
   and acknowledges drain only after both claim gates report `aicc-drained`.
   Existing jobs continue; no new jobs intentionally enter during the short
   credential hand-off.
3. Only after both claim gates are closed does it prepare and fsync the replacement
   EnvironmentFile and call `enroll_rotate_self`. The prepared file is renamed
   immediately after the database commits the new verifier. Every boundary is
   first recorded in the fsynced
   `/var/lib/voyn-aicc-credential-rotation/phase.json` journal.
4. Lanes hot-reload one at a time. A background lifecycle thread opens and
   verifies a new PostgreSQL pool while any 3600-second agent job continues.
   The pool pointer changes atomically; old checked-out sessions retire only
   when returned. `Type=notify-reload` does not return before READY. A failed
   reload falls back to a graceful restart only if the remaining controller
   and credential lifetime cover the complete stop plus readiness budget. The
   fixed one-hour credential cannot safely cover a 3600-second job at this
   stage, so the deployed policy refuses that fallback, leaves the failed lane
   drained, and restores the other lane with the current credential.
5. `ExecStopPost` reads that journal after a crash, timeout or SIGTERM. It
   proves whether `worker.env` or the prepared recovery file is the unique
   credential PostgreSQL accepts, commits that candidate if necessary, then
   reloads and proves every lane named by the root-owned registry
   `/etc/voyn/aicc-worker-lanes.conf` ready before clearing the journal. A later timer
   invocation performs the same recovery before attempting any new rotation.
6. Every transition is JSON in journald and in
   `/var/lib/voyn-aicc-credential-rotation/audit.jsonl`. Any prerequisite,
   missing credential file, systemd, rotation, auth or readiness failure exits
   non-zero and triggers the versioned `daemon.err` alert unit.

`TimeoutStopSec=3660s` covers the 3600-second payload ceiling plus reporting
slack. `KillMode=mixed` sends SIGTERM only to the daemon; the child agent is
left alone until the final bounded SIGKILL. Lease expiry and queue reaping stay
the last-resort redelivery path.

The controller has a 7200-second monotonic deadline. The systemd unit allows
7800 seconds, leaving 600 seconds for audited recovery and clean exit. Before
changing the database verifier, the controller proves that activation plus a
complete two-lane rollback still fits. After issuance, every reload/readiness
operation is bounded by both that controller deadline and the credential's
expiry minus a five-minute safety margin.

The timer retries 15 minutes after the service becomes inactive. Its interval,
60-second random delay and 15-second accuracy window are budgeted together with
the worst successful hot hand-off and the next full pre-drain recovery; their
sum remains below the one-hour credential TTL. The shorter 180-second claim-gate
deadline does not shorten jobs: drain acknowledgement means the atomic claim
gate is closed, while an existing 3600-second child continues on its old pool.

## Deployment from a merged SHA

1. Drain the existing services and disable the old rotation timer. Do not
   delete the old unit files yet.
2. Install the five versioned units, the rotation helper
   (`deploy/voyn-aicc-rotation-helper` -> `/usr/local/sbin/voyn-aicc-rotation-helper`,
   root:root 0755), the lane registry (`deploy/voyn-aicc-worker-lanes.conf` ->
   `/etc/voyn/aicc-worker-lanes.conf`, root:root 0644) and the sudoers policy from
   the checked-out merged SHA; validate sudoers with `visudo -c` and run
   `systemd-analyze verify`. Sudo grants the rotator only the helper; the helper
   authorizes units against the registry, so scaling the fleet is a registry edit
   plus enabling the new `voyn-aicc-worker@N.service` — never a sudoers change.
   The `--recover-only` stop path runs under `--stop-budget` (TimeoutStopSec
   minus the exit margin): a fleet too large to recover in that window refuses
   fail-closed with the phase journal intact, and the next timer start recovers
   it under the full controller budget.
   The immutable release is `/opt/aicc`; the units never execute code from a
   worker-writable checkout.
3. Create dedicated `aicc-worker` and `aicc-rotator` principals. Copy the
   current credential file once to
   `/var/lib/voyn-aicc-credential-rotation/worker.env` as
   `aicc-rotator:aicc-worker 0640`. Put the tunnel target and lease endpoint in
   root-owned `/etc/aicc/pgtunnel.env` and `/etc/aicc/lease.env`; no internal
   addresses or secrets belong in Git. Create non-secret per-lane files
   `worker-1.env` and `worker-2.env` containing
   distinct `AICC_PUBLISH_OWNER` and `VOYN_LEASE_SESSION` values.
4. Enable every `voyn-aicc-worker@N.service` named by the lane registry. Require each to show
   `STATUS=aicc-ready` and complete a database claim/auth smoke before disabling
   the legacy worker services.
5. Run the database migration that installs
   `identity_current_credential(text)`, re-assert the role grants, then run the
   rotation service manually once. Verify the ordered audit chain:
   both drained, credential rotated, lane 1 active, lane 2 active, success.
6. Enable the versioned timer. Compare installed files byte-for-byte with the
   merged SHA and retain the old host files only in a root-owned rollback
   archive until the controlled long-job drill passes.

Rollback before credential mutation is simply reloading both lanes. After a
successful credential mutation, never restore an old `worker.env`; repair the
failed drained lane using the current versioned file while the other ready lane
continues serving.
