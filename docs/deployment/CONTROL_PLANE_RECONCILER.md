# Control-plane reconciler deployment

Task: `VOYN-W0-AICC-CONTROL-PLANE-RECONCILER`.

The reconciler is a control-only rollout. It does not restart workers and does
not relax the writer lease, exact-SHA acceptance or merge gates. Its PostgreSQL
lane is the durable authority for `next_action`, owner, deadline, heartbeat and
retry budget; systemd timers are replaceable wake-up mechanisms.

## Preconditions

1. Deploy only a merged, independently accepted SHA. The checkout at
   `/opt/aicc` must be clean and exactly that SHA.
2. Apply migration `0013` under `aicc_migrator`; never let the root service run
   migrations with the application credential.
3. Keep the guarded publisher disabled until the task-local Git sandbox and
   separate publisher-principal changes are merged and deployed. In that
   state a due `GUARDED_PUBLISH` lane remains a visible bounded retry with
   `capability_not_configured`, rather than running ambient `git push`.
4. Confirm `/etc/aicc/app.env` is root-readable and contains the application
   PostgreSQL and independent GitHub App settings. It must not contain an
   agent/model credential.

## Ordered rollout

```text
drain control ticks (workers continue) -> deploy exact merged SHA ->
database backup/restore point -> migrator upgrade -> control-plane dry-run ->
install root-owned units from exact SHA -> readiness -> recovery drills -> canary
```

Commands are intentionally split by privilege:

```bash
# first install only; the third argument is a root-owned 0400/0600 env file
sudo ./ops/bootstrap_control_plane.sh <trusted-clean-source> <merged-sha> \
  /root/aicc-app.env

# migrator identity
/opt/aicc/.venv/bin/python -m command_center.db upgrade

# read-only application-identity probe
/opt/aicc/.venv/bin/python -m command_center.db control-plane-reconcile --dry-run

# root; argument two is the exact merged SHA
/opt/aicc/ops/install_control_plane.sh /opt/aicc <merged-sha>
```

The installer refuses a non-root caller, a dirty/different checkout, a missing
runtime/env file, a red database status or a red dry-run. It then installs the
four versioned unit files as root, enables the reconciler and independent
watchdog timers, runs one tick and requires a fresh healthy heartbeat. Only
after all of those gates pass it atomically records the exact merged SHA in
root-owned `/var/lib/aicc-control-plane/deployed-sha`.

## Required drills

1. Stop `aicc-backlog-planner.timer` and `aicc-backlog-review.timer`; within one
   reconciler tick both must be active again and the component event history
   must show the repair.
2. Insert a staging lane with an expired RUNNING lease. The watchdog must
   return it to READY once; at exhausted retry budget it must become BLOCKED,
   never duplicate execution.
3. Make an allowlisted unit fail three times. Its persisted circuit opens for
   the configured cooldown and the resulting event becomes the only input an
   owner-notification adapter may consume. This slice records the escalation
   durably; installing a delivery channel is a separate deployment-specific
   adapter, not a hard-coded chat/webhook in the supervisor.
4. Complete tests and independent review evidence for a canary commit. Exactly
   one GUARDED_PUBLISH action must be dispatched by the publisher capability,
   followed by CI, exact-SHA acceptance, merge and backlog evidence.
5. Fail deployment after merge. The reconciler must create a separate OPEN
   deployment-blocker task while the original lane remains WAITING on DEPLOY;
   it must neither enter backlog sync nor convert a technical failure into
   `DEFER_TO_USER` before matching deployment evidence exists.

Worker crash/restart and 3600-second drain drills remain gated on
`VOYN-W0-AICC-ROTATE-PY-UNTRACKED`; this control-only rollout must not guess at
worker shutdown semantics.

## Worker monitor correction

The worker-side monitor rollout is versioned separately and never restarts an
AICC worker. `voyn-worker-health` now proves systemd `Type=notify` readiness and
a fresh watchdog timestamp for both canonical worker instances; it has no dependency on the retired
`voyn-claude.service` or `/run/voyn-claude/heartbeat`. The canonical lanes are
`voyn-aicc-worker@1.service` and `voyn-aicc-worker@2.service`; every health,
recovery and canary consumer targets those same two template instances. The 24-hour canary uses
the same two-lane probe and a new state directory, so old Claude-canary state
cannot be mistaken for current evidence.

Findings delivery reads its control endpoint from the root-owned
`/etc/voyn/findings-sync.env`. The shipped default is the stable Tailscale DNS
name `voyn-control-01.tail39d0b6.ts.net`, and SSH uses a dedicated identity plus
the separately pinned ED25519 host key in `/etc/voyn/findings-known-hosts`.
Changing the DNS endpoint therefore also requires an explicit matching host-key
pin; there is no `accept-new`, key scan or hard-coded overlay IP fallback.

After the merged SHA is present in a clean worker checkout:

```bash
sudo /home/voynadmin/aicc-preprod/repo/ops/install_worker_monitors.sh \
  /home/voynadmin/aicc-preprod/repo <merged-sha>
```

The installer validates the exact SHA, scripts, root-owned configuration,
pinned endpoint and current readiness of both workers before replacing monitor
units. It only restarts the monitor canary and enables monitor timers; worker
restart/drain remains the rotation rollout's responsibility.
It also atomically records that exact merged SHA in the root-owned
`/var/lib/voyn-worker-monitor/deployed-sha`; the canary refuses to start without
that evidence and includes the SHA in its signed-by-hash evidence document.

## Rollback

Disable the two new timers and return `/opt/aicc` to the previous accepted SHA.
Do not downgrade migration `0013` during incident response: removing durable
lane/event rows destroys the evidence needed to reconcile. The old planner,
review, merge and reaper timers remain independently runnable throughout.
