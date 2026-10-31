# Worker credential rotation

`command_center.ops.credential_rotation` is the only supported worker-host
rotation path. It replaces the untracked `~/aicc-preprod/rotate.py`.

## Safety protocol

1. The controller takes a non-blocking host lock, requires `worker.env`, waits
   (with bounded exponential backoff) for the tunnel socket, and asks the
   database for the proved current credential's expiry plus the database clock.
   It refuses mutation unless the server-authoritative remaining lifetime covers
   every configured failed pre-mutation attempt and every systemd retry delay.
2. It requires every registered worker lane to be `aicc-ready`. It does not
   close the fleet's claim gates: an established PostgreSQL session survives a
   verifier change, and the worker opens/authenticates the replacement pool
   before atomically swapping the process pointer. This is also the singleton
   availability contract: its sole lane never leaves `aicc-ready` on the hot path.
3. It prepares and fsyncs the replacement EnvironmentFile and calls
   `enroll_rotate_self`. The prepared file is renamed
   immediately after the database commits the new verifier. Every boundary is
   first recorded in the fsynced
   `/var/lib/voyn-aicc-credential-rotation/phase.json` journal.
4. Lanes hot-reload as a proved canary followed by one registry-derived systemd
   transaction for the remainder. This removes the old credential-TTL ceiling
   caused by multiplying a fixed cohort timeout by fleet size. A background
   lifecycle thread in each lane opens and verifies a new PostgreSQL pool while
   any 3600-second agent job continues.
   The pool pointer changes atomically; old checked-out sessions retire only
   when returned. `Type=notify-reload` does not return before READY. A failed
   reload falls back to a graceful restart only if the remaining controller
   and credential lifetime cover the complete stop plus readiness budget. The
   fixed one-hour credential cannot safely cover a 3600-second job at this
   stage, so the deployed policy refuses that fallback and preserves the lane's
   existing ready process/pool for durable recovery. A singleton is never
   restart-fallback eligible.
5. `ExecStopPost` reads that journal after a crash, timeout or SIGTERM. It
   proves whether `worker.env` or the prepared recovery file is the unique
   credential PostgreSQL accepts, commits that candidate if necessary, then
   reloads and proves every lane named by the root-owned registry
   `/etc/voyn/aicc-worker-lanes.conf` ready before clearing the journal. A later timer
   invocation performs the same recovery before attempting any new rotation.
6. Every transition is schema-versioned JSON in journald and in
   `/var/lib/voyn-aicc-credential-rotation/audit.jsonl`. The file writer loops
   through EINTR/short writes, fsyncs records and directory changes, quarantines
   a partial crash tail, and retains a bounded five-generation rotation under an
   inode-stable lock. Any prerequisite,
   missing credential file, systemd, rotation, auth or readiness failure exits
   non-zero and triggers the versioned `daemon.err` alert unit.

`TimeoutStopSec=3660s` covers the 3600-second payload ceiling plus reporting
slack. `KillMode=mixed` sends SIGTERM only to the daemon; the child agent is
left alone until the final bounded SIGKILL. Lease expiry and queue reaping stay
the last-resort redelivery path.

The controller has a 7200-second monotonic deadline. The systemd unit allows
7800 seconds, leaving 600 seconds for audited recovery and clean exit. Before
changing the database verifier, the controller proves that activation plus a
complete fleet rollback still fits. The budget counts a canary followed by one
systemd-managed fleet transaction and is independent of lane count; that
transaction never performs restart fallback. After
issuance, every reload/readiness operation is bounded by both that controller
deadline and the credential's expiry minus a five-minute safety margin.

The normal timer runs 25 minutes after the service becomes inactive. A failed
oneshot is retried after two minutes. The threshold proof includes the full
worst-case duration of all three failures before mutation plus every declared
retry delay, not just the delays themselves. A lost response once mutation may
have happened switches to the separate new-credential activation/rollback
budget; it is never misclassified as another old-credential retry. Existing
3600-second children continue on checked-out sessions throughout the hot path.

Successful invocations rotate only when the database-authoritative remaining
credential lifetime is at or below the configured threshold. The timer is
therefore a scheduling cadence, not a command to rotate a fresh secret on every
tick. Three consecutive failures open a crash-safe circuit journal;
its cooldown deadline is derived from the PostgreSQL clock, never the host wall
clock. Cooldown checks stay non-mutating and exit non-zero so systemd continues
its bounded retry cadence without extending the same cooldown. Interrupted
recovery always runs before the circuit is consulted.

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
   addresses or secrets belong in Git. Create a non-secret `worker-N.env` for
   every registry entry, each containing distinct `AICC_PUBLISH_OWNER` and
   `VOYN_LEASE_SESSION` values.
4. Enable every `voyn-aicc-worker@N.service` named by the lane registry. Require
   each to show `STATUS=aicc-ready` and complete a database claim/auth smoke
   before disabling the legacy worker services.
5. Run the database migration that installs
   `identity_current_credential(text)`, re-assert the role grants, then run the
   rotation service manually once. Verify the ordered audit chain:
   credential rotated, canary active, fleet transaction active, circuit closed,
   success; at least one lane must remain ready throughout.
6. Enable the versioned timer. Compare installed files byte-for-byte with the
   merged SHA and retain the old host files only in a root-owned rollback
   archive until the controlled long-job drill passes.

Before credential mutation there is no worker-state rollback because no claim
gate has moved. After a successful or ambiguous credential mutation, never
restore an old `worker.env`; probe both durable candidates and hot-reload the
accepted one while the existing ready processes continue serving.

## Control-plane rescue

On 2026-09-22 the worker database credential expired before renewal. Because
both the worker lanes and their rotator authenticated with that credential,
normal retries could not recover. The outage lasted 37 days, until an operator
extended the ledger and role deadline on 2026-10-29.

`command_center.ops.credential_rescue` uses the control plane's existing admin
`AICC_PG_*` configuration to reopen the latest non-revoked credential briefly.
It does not issue a secret. The normal worker rotator then authenticates, rotates,
and revokes the old credential. Inactive principals, workers with any valid
non-revoked credential (including a successor), and credentials expired for
less than 600 seconds are left alone. Workers without a non-revoked credential
are also left alone.

The default grace is 2040 seconds (34 minutes); `--grace-seconds` is clamped to
300–2040 seconds, below the rotator's 2100-second threshold. The default bounds
refuse another rescue within 21600 seconds (six hours) or after three grants
in a rolling 24 hours. These limits count successful audit rows per principal,
across credentials. Flags `--min-expired-seconds`, `--cooldown-seconds`, and
`--max-per-day` configure the bounds. Repeated rescues require investigation of
the worker rotator; recovery must not become indefinite credential renewal.

The repository locks the principal and non-revoked credentials, rechecks
eligibility, and commits the ledger deadline, safely quoted `ALTER ROLE`, and
`_principal_audit` call in one transaction. All timestamps use the database
clock. A failed operation rolls back the whole rescue.

The control installer installs and enables `voyn-aicc-credential-rescue.timer`.
It starts four minutes after boot and five minutes after the last run finishes,
as `voynadmin`, sourcing the existing control environment. To preview decisions
from that configured environment, run:

```sh
python -m command_center.ops.credential_rescue --dry-run
journalctl -u voyn-aicc-credential-rescue.service
```

Output is one `NOOP`, `RESCUED`, `REFUSED`, or `DRYRUN` line per worker, with
principal, credential (or `-`), and reason. Dry-run writes nothing and still
reports limit refusals. Exit 0 means no refusal, 3 means a rescue bound refused
and the systemd service is failed for monitoring, and 1 means an error.

Inspect grants using an administrator session:

```sql
SELECT principal_id, credential_id, created_at, reason, metadata_json
FROM principal_event
WHERE event_type = 'rescue' AND outcome = 'granted'
ORDER BY created_at DESC;
```

Audit details include `previous_expires_at`, `new_expires_at`, and
`grace_seconds`. To disable automatic rescue:

```sh
sudo systemctl disable --now voyn-aicc-credential-rescue.timer
```

A control-profile reinstall enables its declared timers again; preserve a
planned disable through the deployment procedure as well.
