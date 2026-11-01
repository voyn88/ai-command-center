# The PR review-window timer on control-01 (VOYN-W0-AICC-PR-WINDOW-TIMER-NOT-DEPLOYED-ON-CONTROL)

## The incident

2026-09-07/08: 67 open pull requests (752–822) carried **no `queue-*` label**
for over a day, and operators labelled them by hand to keep the fleet moving.

control-01 ran the hand-made `voyn-aicc-{planner,review,merge,reaper,self-deploy}`
system timers and **no PR-window timer at all**. The only committed spelling of
the tick at the time — `deploy/systemd/aicc-backlog-pr-window.{service,timer}` —
names a host layout the control plane has never had (`User=aicc-worker`,
`/usr/bin/python`, an `/srv/ai-command-center` clone, an
`/etc/ai-command-center.env` credential), so it could not be installed there and
never was. `reconcile_pr_window` therefore never ran and the review window was
static.

Every probe on the host stayed green throughout. That is the part worth
remembering: a timer that was **never installed** has no failed unit, no crash
loop, and no unit at all to find. `read_unit_health_snapshot` asks systemd only
for `--type=service`, so a missing *timer* was invisible to it by construction.

## What runs now

`deploy/systemd/voyn-aicc-pr-window.{service,timer}` — the control spelling,
with deliberately no host layout of its own: the operator principal every other
repo-owned control unit runs as (`voynadmin`), the immutable release tree's
interpreter, the deploy-managed source clone, and **no required
`EnvironmentFile`** (the tick reads and writes GitHub only, and runs before any
pool is opened). Cadence is `OnUnitInactiveSec=5min` — scheduled from the end of
the last tick, so a slow tick never queues another behind itself.

It reaches the host through the same release path as the other control timers:

* `ops/aicc_install_transaction.py` — `CONTROL_ONLY_UNITS` installs both unit
  files under the **control** profile, atomically with the rest of the
  generation and rolled back with it;
* `deploy/install-agent-principal-isolation.sh` — `systemctl enable --now` on
  `CONTROL_ONLY_TIMERS` after `run_transaction commit` and after the rollback
  trap is disarmed, so an installed unit is a *running* tick and not just a
  file on disk.

Scanning covers the fleet's real size: `PrWindowConfig.scan_limit` is 100 (the
REST maximum per page) and the listing **paginates until a page comes back
short**, up to `scan_hard_cap`, reporting `pr_list_truncated` rather than
pretending it saw everything. At 180+ open PRs the old default of 50 labelled
the first page and left the rest unlabelled.

## The tick journal

Each run appends one JSON line to `~/.aicc-tick-journal.jsonl`
(`command_center/ops/tick_journal.py`; `$AICC_TICK_JOURNAL` overrides the path):

```json
{"active":5,"age_fallback":0,"at":"2026-09-15T14:24:22+00:00","blocked":1,"outcome":"ok","tick":"pr-window","unchecked":0,"unreadable":0,"waiting":12}
```

Failures are recorded too, with `outcome: "failed"` and the `error` — that is
the case the journal exists for. Without it, "the timer was never installed"
and "the tick ran and had nothing to do" are the same observation, which is
exactly why the outage lasted a day.

The journal is evidence, not a gate: a tick that cannot write its row still
labels, and journald still carries its stdout.

## Verifying it

```sh
systemctl list-timers voyn-aicc-pr-window.timer          # installed, enabled, next elapse
tail -3 ~/.aicc-tick-journal.jsonl                       # it actually ran, and what it did
```

The monitor asks both halves of the question on every control tick
(`voyn-queue-monitor.service`, every two minutes):

* `--control-timers` — are the repo-owned control timers installed, enabled and
  active? Findings: `control_timer_missing` (never installed),
  `control_timer_not_enabled` (a unit file no boot will start — including an
  `enabled-runtime` hand-made one), `control_timer_inactive` (enabled and
  stopped). Three codes because the three remedies differ.
* `AICC_PR_WINDOW_REPO` — the effect side: an open fleet PR carrying `pr`
  evidence that has gone 15 minutes with no window label is
  `pr_window_unlabelled`.

The effect probe alone is not enough, which is why both run: it can only speak
once unlabelled fleet PRs exist and only after their grace period, so on a quiet
fleet an uninstalled timer stays invisible until the moment PRs appear — the
moment it hurts.

## Interim: an operator-run user timer

Only while a control host is waiting for the install transaction. It runs the
same command, as the same user, from the same release tree as the deployed unit:

```sh
systemctl --user edit --full --force aicc-pr-window.service   # then the body below
systemctl --user edit --full --force aicc-pr-window.timer
systemctl --user enable --now aicc-pr-window.timer
loginctl enable-linger voynadmin     # or the timer dies with the last session
```

```ini
# aicc-pr-window.service
[Service]
Type=oneshot
WorkingDirectory=/opt/aicc/current
Environment=GH_REPO=voyn88/ai-command-center
Environment=AICC_FLEET_REPO=/opt/aicc/source
Environment=GIT_TERMINAL_PROMPT=0
ExecStart=/opt/aicc/current/.venv/bin/python -m command_center.db backlog-pr-window --repo-path /opt/aicc/source
TimeoutStartSec=240s

# aicc-pr-window.timer
[Timer]
OnBootSec=4min
OnUnitInactiveSec=5min
RandomizedDelaySec=30s
Unit=aicc-pr-window.service
[Install]
WantedBy=timers.target
```

**Take it away once the deployed timer is running.** A hand-made unit survives
exactly as long as the host does; two timers labelling the same PRs spend the
fleet's GitHub quota twice. `systemctl --user disable --now aicc-pr-window.timer`.

Note that the monitor will keep reporting `control_timer_missing` for the system
timer the whole time this interim unit runs — correctly. The user timer labels
PRs; it does not make the tick deployed.

## Acceptance

* every open PR carries exactly one `queue-*` label within one cadence
  (5 minutes) — check `pr_window_unlabelled` is absent from the monitor;
* window size within 5–8 — `PrWindowConfig.max_active` is 5; the `active` count
  in each journal row is the live measurement;
* no manual labelling for 48h — 48 hours of `outcome: "ok"` rows in the tick
  journal, with no `control_timer_*` finding.
