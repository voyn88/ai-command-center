# Worker credential rotation

`command_center.ops.credential_rotation` is the only supported worker-host
rotation path. It replaces the untracked `~/aicc-preprod/rotate.py`.

## Safety protocol

1. The controller takes a non-blocking host lock, waits (with bounded
   exponential backoff) for the tunnel socket and proves the current database
   credential.
2. It requires both worker lanes to be `aicc-ready`, sends each lane `SIGUSR1`,
   and acknowledges drain only after both claim gates report `aicc-drained`.
   Existing jobs continue; no new jobs intentionally enter during the short
   credential hand-off.
3. Only after both claim gates are closed does it prepare and fsync the replacement
   EnvironmentFile and call `enroll_rotate_self`. The prepared file is renamed
   immediately after the database commits the new verifier.
4. Lanes hot-reload one at a time. A background lifecycle thread opens and
   verifies a new PostgreSQL pool while any 3600-second agent job continues.
   The pool pointer changes atomically; old checked-out sessions retire only
   when returned. `Type=notify-reload` does not return before READY. A failed
   reload falls back to a graceful restart only if the remaining controller
   and credential lifetime cover the complete stop plus readiness budget. The
   fixed one-hour credential cannot safely cover a 3600-second job at this
   stage, so the deployed policy refuses that fallback, leaves the failed lane
   drained, and restores the other lane with the current credential.
5. Every transition is JSON in journald and in
   `/var/lib/voyn-aicc-credential-rotation/audit.jsonl`. Any prerequisite,
   systemd, rotation, auth or readiness failure exits non-zero.

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

## Deployment from a merged SHA

1. Drain the existing services and disable the old rotation timer. Do not
   delete the old unit files yet.
2. Install the four versioned units and the sudoers policy from the checked-out
   merged SHA; validate sudoers with `visudo -c` and run `systemd-analyze verify`.
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
4. Enable `voyn-aicc-worker@1.service` and `@2.service`. Require both to show
   `STATUS=aicc-ready` and complete a database claim/auth smoke before disabling
   the legacy worker services.
5. Run the rotation service manually once. Verify the ordered audit chain:
   both drained, credential rotated, lane 1 active, lane 2 active, success.
6. Enable the versioned timer. Compare installed files byte-for-byte with the
   merged SHA and retain the old host files only in a root-owned rollback
   archive until the controlled long-job drill passes.

Rollback before credential mutation is simply reloading both lanes. After a
successful credential mutation, never restore an old `worker.env`; repair the
failed drained lane using the current versioned file while the other ready lane
continues serving.
