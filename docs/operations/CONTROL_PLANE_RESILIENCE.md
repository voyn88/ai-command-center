# Control-plane resilience (VOYN-W0-AICC-CONTROL-PLANE-RESILIENCE)

The autonomous loop's dispatch/review/merge/reaper ticks all run as
`systemd` timers on control-01, reached only over Tailscale. This host is a
single point of failure for the loop's forward progress, and it failed
twice in the same week in ways that were each invisible from the outside
until someone happened to look:

1. **Connectivity churn banned the operator.** fail2ban's default sshd jail
   treated repeated legitimate reconnects (a sleeping/waking client Mac, a
   flaky Tailscale link) as an attack pattern and banned the operator's own
   IP — self-inflicted lockout of the one host that could fix a stuck
   control plane.
2. **A leftover unit sabotaged review jobs.** An old, no-longer-referenced
   `voyn-aicc-rotate.timer` (predating the credential-rotation unit's
   rename) was still enabled and periodically sent `SIGTERM` into review
   runs it had no business touching.
3. **A silently dead timer starved dispatch for 13 hours (2026-08-29).**
   `aicc-backlog-planner.timer` went `inactive (dead)` at 01:54:49 UTC after
   a 3d3h run. The journal shows only `Deactivated successfully` — no
   service failure, no OOM, no reboot. Review, merge and self-deploy timers
   kept running, so PRs kept merging and hosts kept deploying: every OTHER
   signal stayed green while zero new work was dispatched. Restarting it by
   hand made the very next tick dispatch 4 tasks and ingest 4 results,
   proving the queue had been starved, not empty. `systemctl is-active`
   alone cannot catch this class of failure — the timer genuinely IS
   active; it is the tick behind it that stopped doing anything.

## Connectivity assumptions

- **Tailscale is the only network path** between the operator, control-01
  and worker-01. There is no fallback transport; a Tailscale outage is a
  control-plane outage for anyone not already holding an open session.
- **control-01 cannot reach worker-01 over SSH** (verified live — see
  `command_center/deployment/self_deploy.py`'s module docstring). Every
  cross-host mechanism in this codebase is therefore built as worker-01
  reaching OUT to control-01, never the reverse:
  `voyn-aicc-pgtunnel.service` opens the SSH tunnel worker-01 uses to reach
  control-01's PostgreSQL, and `aicc-control-watchdog.service` (below) rides
  that same tunnel.
- **Each host self-deploys**; there is no cross-host deploy credential
  (VOYN-W0-AICC-DEPLOY-AUTOMATION).
- **fail2ban and SSH connection reuse are host/client configuration, not
  application code** — they live under `deploy/fail2ban/` and `deploy/ssh/`
  (below) precisely so the fix from the 2026-08-29 session survives a
  reprovision instead of being a one-off `ssh` invocation someone remembers
  to redo.

## The reconciler

`command_center/orchestrator/control_plane_reconciler.py` is two
deliberately separate entry points, not one function with a flag — see its
module docstring for the full design. In one sentence each:

- `reconcile_once` (control-01, `aicc_app`, `aicc-control-reconciler.timer`
  every 2 minutes): re-asserts every unit in `DECLARED_TIMERS` is active
  (bounded backoff, circuit breaker after 3 consecutive failures, escalate
  once rather than retry forever), quarantines
  `DEFAULT_QUARANTINE_UNITS` (stop + disable), and — for every timer whose
  tick is heartbeat-instrumented — restarts the SERVICE if its heartbeat
  has gone stale despite the timer reporting active. This is the check that
  would have caught 2026-08-29 directly.
- `check_heartbeats_once` (worker-01, `aicc_worker`,
  `aicc-control-watchdog.timer` every 3 minutes): read-only, touches no
  systemd unit, and runs on a **different host** so a reconciler that
  silently stopped ticking — the planner's exact failure mode, recursively
  applied to the thing meant to catch it — shows up as one more stale
  `tick_name` instead of vanishing along with the process that should have
  reported it. `aicc_worker` holds `SELECT` on `control_plane_heartbeat`
  only (`command_center/db/roles.py:_WORKER_CONTROL_PLANE_TABLES`) and
  neither `INSERT` nor `UPDATE` — a compromised worker-01 cannot forge a
  fresh `last_ok_at` to hide a real stall.

Both write to three tables added by migration `0017`:
`control_plane_heartbeat` (per-tick `last_ok_at`, the "is the tick doing
work" signal), `control_plane_unit_state` (circuit-breaker bookkeeping) and
`control_plane_event` (an append-only action ledger — what the reconciler
already tried, for an operator or the watchdog to read).

Supervision of the reconciler itself is two independent layers, matching
the acceptance criterion that "a reconciler that dies the same way is no
reconciler":

1. `aicc-control-reconciler.service` sets `Restart=on-failure` — covers the
   process crashing outright, before it can even write its own heartbeat.
2. `aicc-control-watchdog.timer` on worker-01 — covers the process going
   quiet without crashing (2026-08-29's actual failure shape), which a
   same-host restart policy cannot observe.

Both `OnFailure=aicc-control-plane-alert@%n.service`, the owner-visible
alert path: a `daemon.err` log line naming the failing unit, fired whenever
either tick's exit code is non-zero (which the CLI returns exactly when
something escalated — see `command_center/db/cli.py`'s `control-reconcile`/
`control-watchdog` handlers).

## Install

```sh
# control-01
cp deploy/systemd/aicc-control-reconciler.{service,timer} /etc/systemd/system/
cp deploy/systemd/aicc-control-plane-alert@.service /etc/systemd/system/
cp deploy/sudoers.d/aicc-control-reconciler /etc/sudoers.d/
visudo -cf /etc/sudoers.d/aicc-control-reconciler
chmod 0440 /etc/sudoers.d/aicc-control-reconciler
cp deploy/fail2ban/voyn-aicc.local /etc/fail2ban/jail.d/
systemctl restart fail2ban
systemctl daemon-reload
systemctl enable --now aicc-control-reconciler.timer

# worker-01
cp deploy/systemd/aicc-control-watchdog.{service,timer} /etc/systemd/system/
cp deploy/systemd/aicc-control-plane-alert@.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now aicc-control-watchdog.timer

# operator's machine (once)
mkdir -p ~/.ssh/config.d
cp deploy/ssh/control-01.ssh-config ~/.ssh/config.d/aicc-control-01
mkdir -p -m 700 ~/.ssh/control
# then add `Include ~/.ssh/config.d/*` as the FIRST line of ~/.ssh/config —
# see deploy/ssh/control-01.ssh-config for the exact reasoning/steps.
```

## Verifying the acceptance

- **A timer stopped**: `systemctl stop aicc-backlog-planner.timer` on
  control-01 — the next `aicc-control-reconciler.timer` tick (≤2 min)
  restarts it and records a `control_plane_event` row.
- **A transient network drop**: both ticks are idempotent oneshots (the
  reaper's pattern used everywhere else in this codebase); a missed tick
  delays recovery, it never corrupts state.
- **A sabotaging unit**: re-enable `voyn-aicc-rotate.timer` — the next
  reconciler tick stops and disables it.
- **A silent stall** (the 2026-08-29 case, timer active but its tick not
  running): freeze the service (or otherwise stop it heartbeating) while
  its timer stays active — once `last_ok_at` exceeds
  `interval_seconds * max_missed_intervals`, the reconciler restarts the
  SERVICE, not just the timer.
- **The reconciler itself going quiet**: stop `aicc-control-reconciler.timer`
  without touching anything else — `aicc-control-watchdog.timer` on
  worker-01 reports `control-reconcile` stale within 3 minutes and the
  `OnFailure=` alert fires, independent of control-01's own state.
