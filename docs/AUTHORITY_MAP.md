# AICC Transactional Authority Map (NIGHT-W9-AICC-AUTHORITY)

One documented source of truth per store and per field family: **exactly one
writer module**, everyone else reads. A fitness test
(`tests/architecture/test_authority_map.py`) fails when a store appears under
`data/` that this map does not name, or when a table or view in the
PostgreSQL server schema (`command_center/db/roles.py`'s `ALL_TABLES` /
`ALL_VIEWS`) is not mentioned here — a new store cannot ship undocumented on
either line, desktop or server.

Conventions used below:

* **Writer** — the single module allowed to mutate the store. Any other
  mutation path is a defect.
* **Recovery** — where truth is recovered from after loss/corruption.
* **Unknown stays Unknown** — no reader may fabricate missing accepted or
  deployed evidence (see `run_lineage.unknown_fields`); a projection that
  cannot be proven from remote/runtime evidence renders as unknown.

## SQLite — `data/runtime.db` (the execution source of truth)

Writer: `command_center/runtime/db.py` only — since the NIGHT-W9 decomposition
a package (`command_center/runtime/db/`, split by table-family: core/schema/
execution/provenance/completion/proposal) whose `__init__` facade re-exports
the same functions unchanged (every other runtime module goes
through its functions; WAL, optimistic `version` columns).

| Field family (tables) | Authority | Recovery |
|---|---|---|
| `task`, `session`, `run`, `run_event`, `report` | Execution lifecycle: what actually ran, its pid/state/events. Runs are reconciled against live OS processes (`Supervisor.reconcile`) — never guessed. | `runtime/maintenance.py`: backup → cold archive (gzip JSONL + sha256) → prune → integrity; `restore_backup()` is the proven rollback (rehearsed on a copy of the real 346MB db, #193). |
| `completion`, `completion_validation`, `completion_event` | Completion pipeline verdicts (validation → PR → merge → verified-in-target). `Done` means merge verified in target, never "a PR exists". | Re-derivable from git remotes + GitHub (`runtime/completion_service.py` re-checks); db backup as above. |
| `run_provenance`, `provenance_evidence` | Canonical run→commit→PR→CI→accepted→deployed lineage (`run_lineage.py`); `accepted_sha`/`deployed_sha` are immutable once recorded and require target verification. Missing evidence is listed in `unknown_fields`, never invented. | Immutable facts re-verifiable against git/GitHub/deployment; db backup. |
| `queue_entry` (mirror) | **Read-only mirror** of `data/execution_queue.json` for SQL joins (`execution_queue._mirror_to_runtime_db`); the JSON file is authoritative, divergence is detected (`queue_divergence`), the mirror is backfillable (`backfill_mirror`). | Rebuild from the JSON queue file. |
| `proposal`, `proposal_evidence`, `proposal_event` | Autonomy proposals/policy approvals (`runtime/autonomy_service.py` via db.py). | db backup. |
| `run_provider_route`, `provider_attempt` | Provider routing + attempt outcomes per run. | db backup. |
| `advisor_proposal`, `owner_item`, `digest_item` | Wave-1 "new engine" surfaces — Советник advisor inbox, «Мой день» owner list, Дайджест rollup (`runtime/db/wave1.py`, written via `api/wave1_service.py` and the `command_center/digest` engine — morning-digest build + «Мой день» event auto-fill; version-CAS rows, status-transition allowlist, per-day idempotent digest rebuild). `advisor_proposal.promoted_task_id` only *records* a task the caller created through `tasks_repository`. | db backup. |
| `conflict` | Wave-2 Conflicts/Incidents engine — tracked frictions (merge/perf/budget/security) moving `open → mitigating → resolved` (`runtime/db/conflict.py`, written via `command_center/conflicts` — the `command_center.conflicts.service` API tier and the `ConflictIntake` bus subscriber that opens a conflict per `IncidentOpened`, dedup by `source_ref`). Version-CAS rows, status-transition allowlist; the resolve invariant (mitigation + owner required) is enforced in the service, never the DB. `project_ref` is the BANK/LEGAL redaction key (excluded in SQL). | db backup. |
| `audit_run`, `audit_finding` | Wave-2 "new engine" Audit surface — automated in-repo audit passes (security/lint/code-quality/deps/coverage checks) and their findings (`runtime/db/audit.py`, written via `api/audit_service.py`, checks in `command_center/audit`; version-CAS rows, status-transition allowlists). Every `audit_finding` always carries a `status` and an `owner`, enforced at the write boundary. `project_ref` (NOT NULL on runs) drives BANK/LEGAL redaction in SQL. `audit_finding.promoted_task_id` only *records* a task the caller created through `tasks_repository`. | db backup. |
| `market_item`, `market_install_log` | Wave-3 Marketplace — the catalogue of installable modules/add-ons (`module`/`domain_pack`/`plugin`) and its append-only install trail (`runtime/db/marketplace.py`, written via `command_center/marketplace` — the `command_center.marketplace.service` API tier and its injected `Installer` seam). `market_item` is a version-CAS row on a `listed → installed` allowlist; each install atomically flips the status *and* appends one immutable `market_install_log` line recording who/when/what version installed it. No code execution lives in this layer — the installer is injected and the baseline default is a no-op (real sandboxing is a later wave). | db backup. |
| `contact`, `message`, `networking_invitation` | Wave-3 Networking engine (schema v23) — people you network with, messages exchanged, and Council invitations (`runtime/db/networking.py`, written via `command_center/networking` — the `command_center.networking.service` API tier). Version-CAS rows, invitation status-transition allowlist (`pending → accepted/declined`). Inbound `feedback`-kind messages are turned into actionable board tasks through `tasks_repository` (never a second writer); the created task and a `NetworkingFeedbackReceived` signal are the only side effects. `networking_invitation.council_ref` is the stable seam the Council engine consumes (no external identity wired). `project_ref` is the BANK/LEGAL redaction key (excluded in SQL). | db backup. |

## JSON (file-locked, atomic-replace via `command_center/storage.py`)

| Store | Authority | Writer | Recovery |
|---|---|---|---|
| `data/tasks.json` (+`tasks.lock`) | The Kanban/product task board — titles, lanes, deps, workflow fields. Execution state is **projected onto** it one-way from runtime.db (`runtime/task_sync.py`); tasks never claim run state on their own. | `tasks_repository.py` | Git-history of intentional snapshots is not kept; operational recovery = re-projection from runtime.db for execution fields + `_founder_reset_backup/` for product fields. |
| `data/execution_queue.json` (+lock) | Dependency-ready launch queue (authoritative; runtime.db carries the mirror). | `execution_queue.py` | Rebuild by re-enqueueing open tasks (`enqueue_and_persist` is idempotent). |
| `data/pipeline_settings.json` (+lock) | Autopilot opt-ins, concurrency caps, `max_daily_spend_usd`; fail-closed parse (malformed ⇒ all off). | `pipeline_settings.py` | Defaults are safe (everything off); re-opt-in by the operator. |
| `data/dispatch_policy.json` (+lock) | Agent-dispatch policy (VOYN-W2-AGENT): local-preference flag, cost matrix, per-agent/per-project budget limits, priority weights, per-business-path tail-risk scenarios (VOYN-MIN-COST-TAIL: probability/impact assumptions priced into an expected cost of error, gated against a limit); fail-closed parse (malformed field ⇒ safe default — an unparseable tail-risk registry falls back to the top-5 default scenarios, never an empty gate). Policy only — never execution truth; budget/kill-switch enforcement reuses `pipeline_settings`/`task_pipeline`. | `command_center/dispatch/policy_config.py` | Defaults are safe (prefer-local, no extra spend limits, tail-risk scenarios priced with headroom under their limits); re-enter via `PUT /api/v1/dispatch/policy`. |
| `data/project_config.json` (+lock) | Project registry: repository paths, allowed execution providers, default branches. | `project_config.py` | `project_config.example.json` + operator re-entry. |
| `data/portfolio_launches.json`, `data/portfolio_locks/` | Portfolio launch records/locks. | `portfolio_launch.py` | Append-only; truncate to last valid line on corruption. |
| `data/chats.json` | Project chat threads (UI convenience). | `chat_service.py` | Non-critical; loss is acceptable by design. |
| `data/integration_registry.json` (+lock) | Integration Center project registry (AICC-INT-001): locally-configured repositories — machine-local paths, `gh` remotes, task-namespace mapping. Machine-local configuration, gitignored; contents are never committed. Operator configuration, never execution truth (see `docs/INTEGRATION_CENTER.md`). | `command_center/integration/registry.py` | Seeded defaults (`DEFAULT_ENTRIES`) + operator re-entry of paths. |

## JSONL (append-only, crash-truncatable)

| Store | Authority | Writer | Recovery |
|---|---|---|---|
| `data/activity.jsonl` | Operator-visible activity feed. | `activity_log.py` via `storage.py` | Append-only: recover by dropping a torn final line. |
| `data/runs.jsonl` | **Legacy v1.2 run records — frozen.** Read-only source for the one-way, non-destructive import into runtime.db (`runtime/legacy_import.py`); nothing writes it anymore. | none (frozen) | It *is* the recovery source for pre-v2 history. |

## Other

| Store | Status |
|---|---|
| `data/runs.db` | **Orphan** — zero code references; predecessor experiment. Retained per recoverable-hygiene policy; removal proposal tracked in NIGHT-W9 cleanup. |
| `data/_founder_reset_backup/` | Operator-made snapshot of product data; user-owned, never touched by code. |
| `data/backups/` | Operator/maintenance backup drop zone (e.g. pre-retention snapshots); write-once artifacts, never read by product code at runtime. |
| `data/daily-audit.*.log`, `data/audits/` | Daily-audit daemon output (`scripts/daily_audit_daemon.py`); disposable diagnostics. |
| `data/task_pipeline.lock` | Advisory same-host tick serialization (`task_pipeline.pipeline_lock`); content-free. |

## PostgreSQL — the server line (planner / review / merge / worker)

Everything above is the desktop/Streamlit line: SQLite plus its JSON/JSONL
satellites. A second, independent line exists — the planner, the independent
reviewer, the merge loop and the execution workers described in
`docs/postgres-foundation.md` — and it runs entirely against PostgreSQL,
provisioned by `command_center/db/*`. `command_center/db/roles.py` is that
schema's own inventory (`ALL_TABLES`, `ALL_VIEWS`): every table and view the
migrations create, so the fitness test below can hold this schema to the same
"no undocumented store" line drawn for `data/` above.

Two different things are true of the tables in this schema, and the whole
point of this section is to not conflate them.

### A. The SRV-01b migration seam — same 33 tables, SQLite remains the authority

`command_center/db/sql/0001_initial.up.sql` gave PostgreSQL a table for every
SQLite domain table the runtime store has — a verified 1:1 correspondence
(`docs/srv01b-schema-map.md`, re-checked against a live database by
`tests/db/test_schema_correspondence.py`, not hand-maintained prose).
`command_center/db/__init__.py` states the scope plainly: this slice
(`VOYN-W0-AICC-SRV-01a`) delivers the schema, roles and grants; cutting the
runtime store's *reads* over to PostgreSQL is `VOYN-W0-AICC-SRV-01b`, and it
has not shipped, so **`command_center/runtime/db.py` remains the sole
authority for this data**.

That does not mean the PostgreSQL side is unwritten. Of these 33 tables, 32
are **live-mirrored today**: every write inside the SQLite family module
already named for it in the SQLite section above (`runtime/db/completion.py`,
`proposal.py`, `council.py`, `audit.py`, `provenance.py`, `networking.py`,
`model_registry.py`, `marketplace.py`, `wave1.py`, `execution.py`) ends with a
call to a local `_mirror()` helper that pushes the same row into PostgreSQL
through one shared implementation, `PostgresTableMirror.upsert()`
(`command_center/db/table_mirror.py`), parameterised per table by a
declaration in the matching `command_center/db/<family>_store.py`. The
`_mirror()` helper swallows any exception the write raises — a PostgreSQL
outage must never block or fail the real SQLite write — so the mirror is
**best-effort and can silently fall behind**; it exists for SRV-01b's future
cutover and for cross-store reconciliation, not as a second read path
anything in production consults today. A PostgreSQL table under one of these
33 names is that shadow copy, not a duplicate source of truth, and its
staleness is not itself a defect.

The 33rd, `queue_entry`, is the one exception: `command_center/db/queue_store.py`
(`PostgresQueueMirror`) implements the identical mirror shape but has **no
production caller** — the JSON→SQLite mirror already documented above
(`execution_queue._mirror_to_runtime_db`) is not, today, also mirrored into
PostgreSQL. Its PostgreSQL table is schema-only, exercised by
`tests/db/test_queue_store.py` and nothing else.

The 33: `advisor_proposal`, `audit_finding`, `audit_run`, `completion`,
`completion_event`, `completion_validation`, `conflict`, `contact`,
`council_decision`, `council_event`, `council_vote`, `digest_item`,
`market_install_log`, `market_item`, `message`, `model_entry`, `model_event`,
`motion`, `networking_invitation`, `owner_item`, `proposal`, `proposal_event`,
`proposal_evidence`, `provenance_evidence`, `provider_attempt`, `queue_entry`,
`report`, `run`, `run_event`, `run_provenance`, `run_provider_route`,
`session`, `task`. Every one is a field family documented in the SQLite
section above under `command_center/runtime/db.py`'s authority; the Council
and model-registry rows among them (`council_decision`/`council_event`/
`council_vote`, `model_entry`/`model_event`) are written there today via the
same facade, through `runtime/db/council.py` and `runtime/db/model_registry.py`
respectively.

### B. Server-only tables — no SQLite equivalent, PostgreSQL is the authority today

These exist only in PostgreSQL: the structured backlog store (the
planner/review/merge loop this very pipeline runs on) and the worker fleet's
claim and identity protocols. Each has exactly one writer, and it is enforced
at the grant level — `aicc_app`/`aicc_worker` hold no table-wide DML on most
of these rows, only `EXECUTE` on the functions listed — not left to
convention (`command_center/db/roles.py`'s module docstring is the fuller
version of the "why" behind each row below).

| Table(s) | Authority | Writer | Recovery |
|---|---|---|---|
| `backlog_task`, `backlog_dependency`, `backlog_evidence`, `backlog_event`, `backlog_task_remediation`, `backlog_scan_cursor`, `backlog_writer_lease` (+ view `backlog_eligible`) | The structured backlog store (BO-S1/S2/S3): triage → dispatch → review/merge → remediation, one status machine per task, cycle-checked dependencies. Every state change is a `SECURITY DEFINER` SQL function (`backlog_upsert_task`, `backlog_transition`, `backlog_add_dependency`, `backlog_record_evidence`, `backlog_record_remediation`, `backlog_lease_acquire`/`heartbeat`/`release`, `backlog_dispatch`, `backlog_ingest_results`, `backlog_return_to_pool`, `backlog_resume_deferred`, `backlog_scan_claim`, `backlog_triage`); no role has a plain table `UPDATE`. | `command_center/db/backlog_store.py` is the only Python wrapper over these functions. Mutating callers: `command_center/orchestrator/planner.py` (the BO-S2 dispatch tick — lease acquire/release, ingest, dispatch), `command_center/orchestrator/review_merge.py` (the BO-S3 review/merge loop — evidence, the scan cursor, the terminal `DONE` transition), and `command_center/db/cli.py` (operator Markdown import via `import_markdown`, the one path allowed to set status directly, since ingest of current truth is not a transition). `command_center/api/backlog_service.py` only reads through the same class. | `scripts/aicc_pg_backup.sh` / `aicc_pg_restore.sh`. During the migration period the Markdown backlog file is the incumbent authority; `BacklogStore.import_markdown` reconciles it into the store via `backlog_upsert_task`. |
| `work_item`, `work_attempt`, `work_result`, `work_event` (+ views `work_item_public`, `work_attempt_public`, `work_dlq`) | The queue-claim protocol (`0002_queue_claim`): one worker owns one attempt, a stale claim cannot write a result, an item reaches `succeeded` only through a durable result. Every step is a PL/pgSQL function (`queue_enqueue`, `queue_claim`, `queue_heartbeat`, `queue_complete`, `queue_fail`); no role holds table-level `INSERT`/`UPDATE` on any of the four tables, so the function set is the only route to a state change — enforced per role, not per module: `queue_enqueue` is `aicc_app`-only, the other four are `aicc_worker`-only. | `command_center/db/work_queue_store.py` wraps both halves. `command_center/worker/daemon.py` is the claim-side caller (the execution loop — claim/heartbeat/complete/fail); `command_center/webapi/queue_routes.py`'s `POST /audit` route is the enqueue-side caller. Recovery/admin (`queue_reap`/`queue_redrive`) is a separate, `aicc_app`-only surface: `command_center/db/work_queue_admin.py` / `work_queue_read.py`, reached from `command_center/db/cli.py` and the same webapi module's read routes. | `scripts/aicc_pg_backup.sh` / `aicc_pg_restore.sh`. A lapsed lease is requeued or dead-lettered by `queue_reap()`, never hand-edited. |
| `principal`, `principal_credential`, `principal_event`, `enrollment_ticket`, `worker_host_fingerprint` (+ views `principal_credential_public`, `enrollment_ticket_public`) | Worker-fleet enrolment and identity (`0003_worker_enrollment`): which hosts are admitted, their rotating credential, and the fingerprint history that tells a rebuild from a clone. `principal_credential`/`enrollment_ticket` are granted to no role — the hash they hold is the capability itself; reads go through the `_public` views. | Split by function, not by module: `enroll_rotate_self`/`identity_current_credential` (a host renewing its own secret) are called from `command_center/ops/credential_rotation.py`, the rolling rotation controller; `identity_revoke_principal` (suspending a host) is called from `command_center/db/fleet_admin.py`, reached by `python -m command_center.db fleet-suspend` (`docs/operations/FLEET_STATUS.md`). **`enroll_mint_ticket`, `enroll_redeem_ticket`, `enroll_sweep_expired` and `identity_assert` have no Python caller anywhere in this repository** — admitting a new host (minting and redeeming its ticket) is an operator running these functions directly over SQL today, proven only by `tests/db/test_enrollment.py`; there is no CLI or app-code path yet. That gap is real, not a documentation oversight, and belongs on the SRV-01b/fleet-tooling backlog rather than being papered over here. | `scripts/aicc_pg_backup.sh` / `aicc_pg_restore.sh`; a revoked host's live backends are terminated via `pg_signal_backend`, not by deleting rows. |
| `schema_migration` | The migration ledger: which numbered migrations have been applied, in what order, and their checksums. Read-only to every non-owner role. | `command_center/db/migrations.py`, run through `python -m command_center.db upgrade`; `aicc_migrator` owns it via DDL, not a granted DML row. | Re-derivable from the `command_center/db/sql/*.up.sql` files themselves; a checksum mismatch surfaces as a `/readyz` failure, never silently. |
| `run_finalization_claim` | Host-local run-finalization fencing. **Unlike every table above, this one has no PostgreSQL writer at all yet.** `command_center/db/roles.py` grants it to no role, on purpose, and says why in its own comment: a future PostgreSQL authority must expose a dedicated CAS function, never blanket DML. This is a declared, empty seam — a schema with no code behind it yet — not a duplicate or an orphan. | — none in PostgreSQL yet. | The SQLite table of the same name is live today, written via `command_center/runtime/db/execution.py` under `runtime/db.py`'s authority (grouped into the Execution lifecycle family in the SQLite section above); that remains the real authority until a CAS function lands here. |

Roles and grants for every table and view above are rendered by
`command_center/db/roles.py` (`render_bootstrap()` / `render_table_grants()`)
and applied by `python -m command_center.db upgrade`; `tests/db/test_role_privileges.py`
connects as each role and proves the matrix rather than trusting the
declaration.

## Deployed truth

The deployed AICC is whatever exact-SHA checkout a given instance runs from
(e.g. `ai-command-center-production-8dd3b1f`, staging worktrees) — verified by
the product itself (Git Center reports repo root/HEAD). A dashboard may only
claim accepted/deployed SHAs backed by `run_provenance`; anything else renders
**Unknown**. `ROADMAP_STATE.md` and GitHub issues/PRs are projections for
humans: on conflict, remote GitHub state + runtime.db provenance win.
