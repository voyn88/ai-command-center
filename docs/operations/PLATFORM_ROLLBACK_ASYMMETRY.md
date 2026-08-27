# Platform rollback asymmetry (`VOYN-W0-AICC-SRV-09-PLATFORM-ROLLBACK-ASYMMETRY`)

## The finding

`scripts/aicc_pg_restore.sh` restores exactly one thing: the AICC-owned
PostgreSQL database named by `AICC_PG_DB` (`pg_dump`/`pg_restore` of that
database's own schema and rows). During normal operation AICC also writes,
through two mechanisms described below, into **the AIOS platform's own
PostgreSQL database** — an installation the AICC repository holds no
credentials, migration authority or restore procedure for. A restore of
`AICC_PG_DB` to a pre-cutover snapshot has no way to reach those rows, and
nothing in either the AICC or AIOS repository compensates for the gap: the
platform keeps recording AICC's actions from the window between the backup
and the restore point as if they had never been undone, because — from the
platform's side — they weren't.

This is traced below to two concrete write surfaces, why the restore script
cannot and should not reach them, why the gap is a direct consequence of
`SRV-02`'s decision to split the writer lease out of AICC's own database
(the "LEASE-SPLIT"), and a defined reconciliation procedure for an operator
to run around any restore during a cutover window.

## What AICC writes into the platform

### 1. The writer lease — `repo_lease` / `repo_lease_event` ("аренды, их журнал")

`repo_lease` is a table in the **AIOS platform database**
(`migrations/versions/0010_repo_lease.py` in the `aios` repository), not in
AICC's. Its own docstring states the reason: a per-consumer-database lock
"gets a lock keyed on its own worktree path — which is per checkout, so two
hosts each grant themselves a lease the other cannot observe," which is
exactly the failure this table exists to close. It carries no tenant
dimension; it is installation-level, keyed on `repository_id`. Every
`held` row also carries `task_id`, `owner_session`, `owner_host`,
`owner_pid`, `worktree_path`, `branch`, `expected_head`, `expires_at` and a
monotonic `fence`. Beside it, `repo_lease_event` is an append-only journal —
one row per `acquire`/`renew`/`release`/`expire` decision, `ON DELETE
RESTRICT` from its parent specifically so a deleted lease row cannot take its
own history with it.

AICC never talks to that database directly. It shells out to the external
`voyn-lease` CLI (named by `VOYN_LEASE_TOOL`, reached through
`VOYN_LEASE_DSN` — a DSN naming the platform's database, not AICC's) from
three call sites that share one identity/argv shape
(`command_center/worker/lease_client.py`):

- `command_center/worker/writer_lease.py` — the full-lifecycle lease held
  from workspace provisioning through the agent run, tests and publish
  (`acquire`, periodic re-`acquire` as renewal, `release`).
- `command_center/orchestrator/publish.py` — the lease held around the
  actual `git push` (`acquire`, `install-hooks`, `release`).
- `command_center/worker/worktree_lease.py` — read-only (`list` only); it
  never acquires or writes anything.

Every `acquire`/`release` call is a write into `repo_lease` and a new row in
`repo_lease_event`, on the platform's database, the instant the CLI returns
success. None of it is transactional with anything AICC commits to its own
`AICC_PG_DB`.

### 2. Task state — `tasks` / `task_events` ("состояние задач через SDK")

`tasks` and `task_events` are tenant-scoped tables in the same platform
database (`migrations/versions/0006_tasks_engine.py`, row-level security
keyed on `aios.tenant_id`). AICC's sole path to them is the public `aios_sdk`
package, consumed through exactly one adapter
(`command_center/application/aios_tasks.py`, enforced by the architecture
fitness gate documented in `docs/AIOS_BOUNDARY.md`). `AIOSSDKTasksGateway`
issues `tasks.create`/`.assign`/`.start`/`.complete`/`.cancel` — each one a
write into `tasks` plus (by the platform's own convention) a `task_events`
row — whenever `AIOSTasksRepository.create`/`.upsert`/`.update_status`/
`.delete` advances an AICC task's lane (`command_center/tasks_repository.py`
wires this in as `AICC_TASKS_BACKEND=aios`). `aios_task_to_aicc_dict`'s own
comment states the design intent plainly: *"The remote lifecycle is
authoritative. A stale payload lane cannot promote or roll back a task."*
That is correct as a normal-operation invariant and is precisely what turns
into the asymmetry below once AICC's own database — the thing that lane
comes from — gets rolled back underneath it.

## Why `aicc_pg_restore.sh` cannot reach either of these

The script's own header states its scope: it restores *"an AICC server
database from a `pg_dump` archive"*, gated on `AICC_PG_*` credentials for
one target database. That scope is correct and should not widen — AICC has
no migration role, no schema ownership and (per `0002_queue_claim.up.sql`'s
own placement note) no business writing into `repo_lease` or `tasks`
directly; they are the platform's tables, reached only through its CLI and
SDK. But it means a restore is, by construction, a **single-database**
operation against a system whose in-flight state is split across two
independently-committed databases with no distributed transaction between
them. `queue_claim`'s own header draws this exact line for the lease
specifically: *"A lease check at claim time would also be worthless: it is
stale by the time a git command runs,"* precisely because the two
protocols — AICC's queue claim and the platform's repository lease — compose
without nesting. Restoring one side of that composition and not the other is
not a bug in the restore script; it is the inherent shape of two systems of
record, and today nothing else closes the gap it opens.

### Why this is a direct consequence of `SRV-02-LEASE-SPLIT`

`repo_lease`'s own migration docstring records the decision explicitly: an
earlier, per-consumer-database lock was rejected — *"The consumer's own
architecture gate refused the implementation for the same reason, in its own
words: new capability of these categories belongs to the platform and is
consumed through a versioned contract. So the table is here."* Before that
split, a lease implemented inside AICC's own database would have rolled back
coherently with everything else a restore touches — wrong in a different way
(per-checkout, not cross-host, exactly the bug the split fixed), but at
least symmetric with the restore boundary. The split traded that symmetry
for the correctness property the AIOS platform can uniquely provide
(cross-host visibility, a fencing token, proof-of-death takeover), and this
task is the follow-on cost of that trade: an AICC-side restore can no longer
reach the lease at all, coherently or otherwise.

## The restored database cannot even tell you what it missed

`work_event` — AICC's own append-only audit of every claim/heartbeat/
complete/fail/enqueue/redrive decision, *"INCLUDING THE REFUSALS"* per its
migration comment — lives inside `AICC_PG_DB`, the very database a restore
rolls back. Anything appended to it after the backup's snapshot point is
exactly what the restore discards. So a restore does not only fail to undo
the platform writes made during the window; it also destroys the one
AICC-side record of *which* work was in flight when the window opened, which
is what would have named which `repo_lease`/`tasks` rows to go reconcile.

Two things survive by accident, not by design, because they are filesystem
writes outside `pg_dump`'s scope rather than rows in `AICC_PG_DB`:

- **`aios_task_map.json`** (`command_center/application/aios_tasks.py`'s
  `AIOSIdMap`, at `<AICC_DATA_DIR>/aios_task_map.json` —
  `command_center/tasks_repository.py` wires the path) — the AICC-id ↔
  AIOS-id correlation. A restore of `AICC_PG_DB` does not touch this file, so
  after restore it still names every AIOS task AICC had touched, even though
  AICC's own restored idea of that task's lane may now be stale.
- **The pre-push hook's on-disk lease identity file**, written by
  `voyn-lease install-hooks` into the repository clone's common git dir
  (documented in `command_center/orchestrator/publish.py`'s module
  docstring) — the last repository/owner/session/task/pid/process-start
  identity AICC's lease held. It also survives a `AICC_PG_DB` restore
  untouched, and is the only on-host record of who last acquired the lease.

Neither file was built as a reconciliation aid — they exist for idempotent
task creation and pre-push hook verification, respectively — but they are
the only surviving breadcrumbs once the restore has happened, and the
procedure below depends on them where AICC's own database offers nothing.

## Defined procedure

The reconciliation manifest has to be captured **before** the restore runs,
while `AICC_PG_DB` still has a live opinion about what is in flight — after
the restore, that opinion is gone (see above). Concretely, around any
`scripts/aicc_pg_restore.sh --allow-overwrite` run during a cutover window:

**Before restoring:**

1. Freeze dispatch — stop or pause every `aicc-worker@N` lane so no new
   `queue_claim()` succeeds while the manifest below is captured and the
   restore runs. A worker claiming work against the pre-restore database
   while this procedure is in flight would itself become a third undocumented
   writer into the platform.
2. Snapshot in-flight work from the live (not-yet-restored) `AICC_PG_DB`:
   every `work_item`/`work_attempt` pair in `state = 'claimed'`
   (`work_item_public`/`work_attempt_public`), each row's `task_id`. There is
   no existing admin query for this — `command_center/db/work_queue_admin.py`
   today only exposes `reap`/list-dead-letters/`redrive` — so this is a gap
   to close (see Follow-up).
3. For each in-flight `task_id`, resolve its platform identities *now*, while
   they are still fresh, and write the result **outside** `AICC_PG_DB` (a
   plain file on the operator's restore host is enough — it must not be
   destroyed by the very restore it is protecting against):
   - the AIOS id from `aios_task_map.json`;
   - whether `voyn-lease list` shows a `held` row whose `worktree`/`task`
     matches this task (`command_center/worker/worktree_lease.py`'s
     `blocking_lease` already parses this exact JSON shape).

**Run** `scripts/aicc_pg_backup.sh`/`scripts/aicc_pg_restore.sh` as the
cutover requires.

**After restoring, before resuming dispatch:**

4. Lease reconciliation: for every manifest row still reported `held` by
   `voyn-lease list`, decide with an operator, per row — the restored AICC
   has no process left that remembers acquiring it, so nothing will ever
   renew or `release` it through the normal path
   (`writer_lease._Handle.__exit__`/`publish_run`'s `finally`). Today
   `voyn-lease` is called from this repository only with `acquire`
   (`--auto-takeover`, which requires proof the *holder's process* is dead —
   not applicable here, since the holder may still be a live process that
   simply lost its database), `install-hooks`, `release` and `list`; an
   operator-forced release/takeover of a lease whose owning *database* was
   rolled back, rather than whose owning *process* died, is not a verified
   capability of the tool as used anywhere in this repository today and
   needs confirming with whoever owns `voyn-lease` before this step can be
   automated.
5. Task reconciliation: for every AIOS id named in `aios_task_map.json` (or
   the pre-restore manifest), read its current state via the same
   `TasksGateway`/`aios_sdk` path AICC already uses
   (`AIOSTasksRepository`/`get_task`) and compare it to the *restored*
   AICC task's lane. Where the platform is ahead of the restored AICC state
   (e.g. platform `completed`/`in_progress`, restored AICC `open`/`ready`),
   `AIOSTasksRepository._advance` will refuse to reverse it
   (`UnsupportedTaskTransitionError`) if dispatch is simply resumed, so an
   operator must choose explicitly, per task:
   - **trust the platform** (consistent with `aios_task_to_aicc_dict`'s own
     "the remote lifecycle is authoritative") and resync AICC's local lane
     forward to match — the work the platform recorded genuinely happened
     and stays done; or
   - **cancel** the platform task (`AIOSTasksRepository.delete` /
     `cancel_task`) if the restored AICC state means the work must be
     redone. This creates a new tombstone; it does not retract whatever the
     original run already published (a PR, pushed commits) during the
     window, which needs its own separate review.
   Update or remove the corresponding `aios_task_map.json` entry to match
   whichever choice was made, so the map does not keep pointing a future
   AICC run at a cancelled or already-superseded remote task.
6. Only after every manifest row from step 2 has an explicit disposition
   from steps 4–5 should worker dispatch resume.

## Follow-up (not done by this task)

Nothing above is automated in either repository today; this document defines
the procedure an operator runs by hand. Two concrete gaps to close as
separate, reviewed work:

- A read query (`command_center/db/work_queue_admin.py` or a sibling) that
  lists in-flight (`state = 'claimed'`) `work_item`/`work_attempt` rows —
  step 2 above has no existing surface to call today.
- A `scripts/aicc_post_restore_platform_reconcile.py` that drives steps
  3–5 mechanically: read the pre-restore manifest and `aios_task_map.json`,
  call `voyn-lease list` and the same `TasksGateway` AICC already uses, and
  print the operator's decision points instead of requiring them to be
  reconstructed by hand from logs. This depends on first confirming with the
  `voyn-lease` owner whether an operator-forced release of a database-orphaned
  (not process-dead) lease is a supported verb — see step 4.
