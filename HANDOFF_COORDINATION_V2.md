# HANDOFF — Coordination v2 (Grok 4.6 ↔ Cursor)

**Status:** implemented locally, **not activated**. Owner must opt in after migrate/deploy.

**Commit SHA:** _(filled at commit time; see `git rev-parse HEAD` on this branch)_

## What shipped

Smallest durable coordinator integrated into the existing planner tick path:

| Piece | Role |
| --- | --- |
| Grok 4.6 | Bounded **model-only** structured advisory ordering of already-eligible task ids |
| Cursor | Implementation via existing `owner-alternatives` cascade (unchanged; PR #1071) |
| Planner | Remains sole dispatch authority (`backlog_dispatch`); advisory order only reorders candidates |
| Queue | Async `agent_run` work item — **never** holds `planner:global` across a model call |
| State | Versioned table `coordinator_state` + CAS helpers (migration **0031**) |

No new systemd unit. `python -m command_center.db backlog-plan` runs `coordinate_once` **after** `plan_once` returns (lease already released).

## Files

- `command_center/db/sql/0031_coordinator_v2.up.sql` / `.down.sql` — durable state + CAS
- `command_center/orchestrator/coordination.py` — snapshot, parse/fail-closed, enqueue/apply
- `command_center/orchestrator/planner.py` — advisory reorder of eligible candidates
- `command_center/db/cli.py` — post-plan `coordinate_once` hook
- `command_center/db/roles.py` — `coordinator_state` read + CAS EXECUTE grants
- `tests/orchestrator/test_coordination_v2.py` — unit coverage (mocks, no live API)
- `tests/db/test_roles_render.py` — CAS surface named in app backlog functions

**Not touched:** merge App, credentials, merge guards, Grok agent reasoning/partials, routing matrix pins from #1071.

## Persistent state

Table `coordinator_state` (name=`grok_cursor_v2`):

- `snapshot_hash` / `blocker_hash` — economical dedup across restarts
- `in_flight_key` — at most one coordination action in flight
- `advisory_order` — jsonb array of eligible task ids (advisory only)
- `cooldown_until` / `last_status` — reject/stop/cooldown bookkeeping

Writes only via:

- `coordinator_cas_begin`
- `coordinator_cas_finish`
- `coordinator_cas_clear_inflight`
- `coordinator_state_read` (read)

## Opt-in activation / rollback

**Default is off.** Nothing in this change enables coordination in production.

Activation (owner-operated, after migrate + deploy of this SHA):

1. Apply migration 0031 (`python -m command_center.db upgrade` as migrator).
2. Re-assert grants (`upgrade` already does).
3. Set on the control-plane host env (e.g. `/etc/aicc/app.env`):
   - `AICC_COORDINATOR_V2=1`
4. Ensure workers can run Grok model-only (existing #1071 install).
5. Leave `AICC_COORDINATOR_V2_STOP` unset.

Rollback / stop:

- Immediate stop: `AICC_COORDINATOR_V2_STOP=1` (or unset `AICC_COORDINATOR_V2`).
- Schema rollback: migration 0031 down (drops `coordinator_state` + CAS functions).
- Planner without advisory order: empty/missing state → original eligible order.

## Tests actually run

```text
PYTHONPATH=/workspace /opt/aicc/runtime-venv/bin/python -m pytest -q \
  tests/orchestrator/test_coordination_v2.py \
  tests/db/test_migration_set.py \
  tests/db/test_roles_render.py \
  --noconftest
# 54 passed
```

(`--noconftest` used here because this host cannot establish supervisor process identity; tests are hermetic mocks and do not need the suite autouse fixtures.)

Covered behaviors:

- unchanged snapshot → no model enqueue
- unchanged blocker after reject → no model enqueue
- stale / invented ids → fail closed
- malformed / forbidden-key result → reject + cooldown
- concurrent/restart single in-flight
- enqueue failure clears CAS claim
- stop switch / disabled default
- prompt excludes bodies/secrets; cascade is Grok 4.6 model-only
- migration 0031 paired + `coordinator_state` in `ALL_TABLES`

No live provider calls, no production DB, no package installs.

## Limitations / gaps

- **Not activated** — env flag required; this handoff does not claim live coordination.
- No AGENTS.md in repo root; guidance taken from planner/queue/routing + `docs/operations/ALTERNATIVE_EXECUTORS.md`.
- Advisory order is a soft preference only; wave gate, WIP, repo leases, and acceptance gates still bind.
- Coordinator does not create tasks, triage, mark DONE, or widen permissions.
- Apply validates against *current* eligible ids (fail closed if the model’s order references ids no longer eligible).
- Compact catalog capped (`max_candidates=40`); titles truncated; no full backlog/bodies/secrets sent.
- In-flight recovery if the work item row is missing clears the CAS claim (crash between begin and enqueue).
- No dedicated metrics/UI surface in this slice.

## Prioritization policy baked into the prompt

Reconcile evidence-bearing / shipped work before new work; prefer tasks that reduce downstream model calls, retries, CI, and errors; UNKNOWN costs remain unknown; prefer ordering existing eligible ids over inventing work.
