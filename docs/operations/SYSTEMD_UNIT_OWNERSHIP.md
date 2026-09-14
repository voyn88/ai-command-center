# Repo-owned systemd units (VOYN-W0-AICC-SYSTEMD-DRIFT-VERSIONED-UNITS)

## The finding

The drift audit of 2026-08-29 asked one question — are we on the latest
everything, with no stale versions or crutches — and got two different answers
for the two layers. The **code** layer was current: both hosts' checkouts at
`origin/main`, the self-deploy tick reconfirming it every five minutes, schema
at the latest merged migration. The **ops** layer was 100% unversioned drift:

> Not one systemd unit on either host came from the repository. All 22
> versioned units in `deploy/systemd/` were unused; the hosts ran hand-made
> copies in `/etc/systemd/system` under different names — host
> `voyn-aicc-review.timer` against repo `aicc-backlog-review.timer`, host
> `voyn-aicc-worker.service` against the repo's `voyn-aicc-worker@.service`.

The units were never the point. The consequence was: **every unit fix merged
through the pipeline was inert on the fleet.** PR #383's principal-isolation
architecture was the visible casualty, but it is the general case, and it is
why `VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION` could exist at all — the
repository template already had `RuntimeDirectory=voyn-aicc-worker/%i` per
lane, and the host copy did not.

The sharpest case was the self-deploy tick itself: the one unit whose entire
job is "track the repository" was, on both hosts, a pair of symlinks into a
directory under the operator's home. The tick updated the CODE every five
minutes while nothing at all updated the UNITS that run it.

## What owns a unit now

`default_specs()` in `ops/aicc_install_transaction.py` is the single authority
for what the repository owns, per profile. Installing is
`deploy/install-agent-principal-isolation.sh` (root), and every unit it names
lands in the same atomic, reversible generation as the rest of the install: it
commits together or rolls back together.

| Unit | Profile | Source |
| --- | --- | --- |
| `voyn-aicc-self-deploy.service` | both | `voyn-aicc-self-deploy-worker.service` (worker) / `voyn-aicc-self-deploy.service` (control) |
| `voyn-aicc-self-deploy.timer` | both | same name |
| `voyn-aicc-github-token.{service,timer}` | both | same name |
| `aicc-principal-recovery.service`, `aicc-agent-launcher.socket`, `aicc-agent-launcher@.service` | worker | same name |
| `voyn-aicc-worker@.service` + both principal-isolation drop-ins | worker | same name |
| `voyn-aicc-source-clone-refresh.{service,timer}` | worker | same name |
| `voyn-infra-monitor.{service,timer}` | worker | same name |
| `voyn-aicc-{review,merge,remediate,pr-window}.{service,timer}` | control | same name |
| `voyn-queue-monitor.{service,timer}` | control | same name |

Two profiles, one target, different `ExecStart` — that is deliberate for
self-deploy. The control host owns the database and migrates; the worker host
holds no DDL privilege and restarts its lanes instead. The timer,
`hold_lane_timers`, the staged rollout and every runbook name one unit
(`voyn-aicc-self-deploy.service`), so shipping the worker variant under a name
of its own would have left the host's hand-made file live under the name
everything actually uses.

Timers are `enable --now`-ed after the generation commits and after the
rollback trap is disarmed — a committed file activated afterwards, never an
enablement symlink pointing at a unit file a rollback could remove.

## What the same generation retires

* `/usr/local/sbin/voyn-infra-monitor` — superseded by
  `deploy/systemd/voyn-infra-monitor.service`.
* `/usr/local/sbin/voyn-worker-health` — no replacement and none needed: it
  probes `claude_supervisor`, a component this architecture no longer runs,
  and had been exiting 1 on every tick for that reason.

Both were root-owned, unreadable to `voynadmin`, absent from git and without
an owner. They are retired through the ordinary removal machinery, so the
retirement rolls back with the generation that replaces them; a committed
generation keeps its backups, so `recover` can put them back byte-for-byte.

`voyn-findings-sync` (`/opt/voyn-worker/bin/voyn-sync-findings`) was stopped
and disabled on 2026-08-29 — it ran every minute against a stale host and
failed every time, and never delivered anything.

## What is still hand-made, and why

* **The backlog planner and the queue reaper.** Still operator-installed;
  they follow under `VOYN-W0-AICC-CONTROL-PLANE-REPO-OWNED-UNITS`.
* **Credential rotation.** The old `voyn-aicc-rotate.timer` is active and
  enabled; the repo's safe `voyn-aicc-credential-rotation.timer` is present
  and disabled. Deliberately not flipped by an installer that runs for other
  reasons: switching rotation has credential-wide blast radius and needs its
  own maintenance window and a rehearsed rollback. See
  [`WORKER_CREDENTIAL_ROTATION.md`](WORKER_CREDENTIAL_ROTATION.md).

## The drift probe

It took a human audit to find a fleet-wide inert-deployment condition, so the
measurement now runs on every tick. Both fail-closed probes carry it —
`AICC_UNIT_DRIFT_REPO=.` plus the host's profile — and it reads the
installer's own spec list rather than a second list of unit names, so the
probe and the installer cannot disagree about what the repository owns.

Four finding classes, because each has a different fix:

| Finding | Meaning | Fix |
| --- | --- | --- |
| `unit_hand_made` | the target is a **symlink** | run the root installer; a repo-owned unit is a regular file |
| `unit_drift` | installed, but not byte-identical to the checkout | run the root installer — until then the merged fix is not on this host |
| `unit_absent` | the repository owns it, the host does not have it | run the root installer |
| `unit_retired_present` | the generation removes it and it is still there | a conversion that did not complete |
| `unit_drift_probe_failed` | the probe could not measure | a probe that cannot see is not a probe that saw nothing |

`unit_drift` is **expected** to go red as soon as a unit change merges, and to
stay red until the root installer runs on that host. That is the signal, not
noise: it is precisely the state the audit had to be run by hand to discover.

## Running it

```sh
sudo deploy/install-agent-principal-isolation.sh            # worker (default)
sudo deploy/install-agent-principal-isolation.sh --profile control
```

`voynadmin`'s `systemctl` + `apt-get` grants cannot do this: the install
creates the `aicc-worker` principal, the `aicc-workspace`/`aicc-publisher`
groups, `/opt/aicc/current`, `/etc/aicc/*.env`, `/etc/voyn/secrets`,
`/srv/aicc-workspaces` and the boot-recovery anchor. Until it lands on a host,
every unit-level fix merged into main stays inert there — which is the whole
reason this is the programme's highest-leverage item.

Verify afterwards, unprivileged, from the deploy-managed clone:

```sh
.venv/bin/python -m command_center.ops.infra_monitor \
  --skip-queue --skip-workers --minimum-active-workers 0 \
  --prometheus-url http://127.0.0.1:9090/-/ready \
  --unit-drift-repo . --unit-drift-profile worker | python3 -m json.tool
```

`unit_drift.judged` is how many unit targets were compared; empty `absent`,
`diverged`, `unmanaged` and `retired` is a host whose systemd layer is the
repository's.
