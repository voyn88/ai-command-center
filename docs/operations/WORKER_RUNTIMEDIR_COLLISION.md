# Worker RuntimeDirectory collision (VOYN-W0-AICC-WORKER-RUNTIMEDIR-COLLISION)

Root cause of the 2026-08-27..29 queue collapse: 594 tasks dead-lettered, the
owner stopped the planner. Recorded here because the failing units were
hand-made host copies, never committed to this repository, so nothing in
`git log` or `git blame` documents what actually ran.

## What happened

Three systemd units on worker-01 -- `voyn-aicc-worker.service`,
`voyn-aicc-worker@3` and `voyn-aicc-worker@4` -- all declared
`RuntimeDirectory=voyn-aicc-worker`, one shared path, and each installed its
lease/app credential to `/run/voyn-aicc-worker/pgpass`. systemd does not
refcount `RuntimeDirectory` across units: whenever *any* of them stopped,
systemd deleted that directory out from under the still-running siblings.
That pgpass carried the password for both the writer-lease authority
(`10.20.0.2:5432`) and the app DB via the tunnel (`127.0.0.1:5433`), so a
sibling restart made surviving lanes fail authentication ->
`psycopg_pool.PoolTimeout` after 10s -> worker `exit(1)` -> the in-flight
task's attempts burned -> dead-letter.

Evidence: `@3`/`@4` carried `NRestarts=33` each while `worker`/`worker-2`
(each with its own directory) had 0. The 2026-08-29 01:40:48 journal shows a
`PoolTimeout` at `127.0.0.1:5433` immediately followed by `Main process
exited, code=exited, status=1/FAILURE, restart counter is at 19`. Dead-letter
classes over the five days: 240 `workspace_authority_key` (25-26 Aug, a
separate, already-fixed defect), 146 dirty-worktree publish refusals (26-28
Aug, a separate defect), 28 lease contention (by design), 13 `Password for
user voyn_lease_client` (27-29 Aug, this defect, last at 01:40:58 -- 14
minutes before the owner stopped the planner). Confirmed live on 2026-08-29
15:01Z: stopping `@3`/`@4` deleted `/run/voyn-aicc-worker` while
`voyn-aicc-worker.service` was still active.

## Interim mitigation (applied, no root needed)

`@3` and `@4` are stopped and disabled. The fleet runs two lanes
(`voyn-aicc-worker`, `voyn-aicc-worker-2`) whose RuntimeDirectories are
disjoint. This is correct but caps the fleet at half throughput until the
real fix below is installed.

## Real fix (built, not yet installed on the host)

`deploy/systemd/voyn-aicc-worker@.service` already uses
`RuntimeDirectory=voyn-aicc-worker/%i` + `PGPASSFILE=/run/voyn-aicc-worker/%i/pgpass`
-- one subdirectory per lane, so no unit's stop can ever delete another
lane's credentials. It also carries `User=aicc-worker`,
`TimeoutStopSec=3660s` and `Type=notify-reload`, none of which the stale
hand-made host units have.

Installing it requires root: a new system user/groups, `/opt/aicc/current`,
several root-owned env files under `/etc/aicc` and `/etc/voyn/secrets`,
`/srv/aicc-workspaces`, `aicc-principal-recovery.service` and the pinned
`/usr/local/bin/codex`. `deploy/install-agent-principal-isolation.sh` is the
intended installer; the full procedure is
[`AGENT_PRINCIPAL_ISOLATION_ROLLOUT.md`](AGENT_PRINCIPAL_ISOLATION_ROLLOUT.md).
It replaces the legacy `voyn-aicc-worker.service` units with instances of the
template, one per registry entry in `/etc/voyn/aicc-worker-lanes.conf`
(sourced from `deploy/voyn-aicc-worker-lanes.conf`). Until an operator with
root runs it, the fleet stays at the two-lane interim mitigation above.

## Regression guard

[`tests/test_worker_runtimedir_collision.py`](../../tests/test_worker_runtimedir_collision.py)
fails the suite if any two committed unit files root their `RuntimeDirectory`
at the same path, or if the worker template's path stops being keyed by
`%i`. The units that caused this incident were never committed, so this
guard cannot catch a repeat of *this exact* drift by itself -- it only
prevents the same mistake from re-entering the repository. Verifying the live
host units matches the committed ones is part of the installer's rollout
(`AGENT_PRINCIPAL_ISOLATION_ROLLOUT.md`), not a repo-only check.
