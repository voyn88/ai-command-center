# VOYN-W0-AICC-SRV-02-LEASE-SPLIT — lease vs principal scope

Child slice of the SRV-02 pipeline split (`LEASE-SPLIT` suffix). It records
the boundary the fleet must not conflate when dispatching or reviewing work.

## What this slice closes

**Writer lease** (execution coordination) is not part of **SRV-02**
(principal-and-permission model):

| Concern | Mechanism | Question it answers |
|--------|-----------|---------------------|
| Writer lease | `voyn-lease` + `writer_lease.hold` (per-task scope, ADR-0011) | Which process may mutate a checkout right now? |
| Planner WIP lease | `backlog_writer_lease` in PostgreSQL (BO-S1) | Which planner session holds repo dispatch? |
| SRV-02 / AUTH-HTTP-01 | `command_center/http_auth/` + `roles.py` principal registry | Which platform principal may call which HTTP/DB surface? |

SRV-02's intended scope was delivered under `VOYN-W0-AICC-AUTH-HTTP-01`; see
`CHANGELOG.md` (2026-08-15). The identity registry placement is recorded in
`docs/AIOS_BOUNDARY.md` (`VOYN-W0-AICC-SRV-02-PLACEMENT`).

No further production code is required under this task id: the split exists so
lease coordination work is not mistaken for permission-model work (and vice
versa).

## Why pr_evidence stalled on this task

The task reached `READY_TO_REVIEW` without `pr` evidence and without an open
pull request on `backlog/VOYN-W0-AICC-SRV-02-LEASE-SPLIT`, so
`reconcile_pr_evidence` could only skip with `no_open_pr_on_task_branch`.
That state cannot self-heal. The pr_evidence tick now applies
`backlog_recover_stuck_ready_to_review` (0018) in that case so the task returns
to `OPEN` and a fresh dispatch can run `publish_run` instead of tick-looping.
