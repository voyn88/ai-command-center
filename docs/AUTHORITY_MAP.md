# AICC Transactional Authority Map (NIGHT-W9-AICC-AUTHORITY)

One documented source of truth per store and per field family: **exactly one
writer module**, everyone else reads. A fitness test
(`tests/architecture/test_authority_map.py`) fails when a store appears under
`data/` that this map does not name, or when a PostgreSQL table declared in
`command_center/db/roles.ALL_TABLES` is not named in the
[PostgreSQL](#postgresql--server-line-command_centerdb) section below — a new
store cannot ship undocumented, on either database.

This map covers **two lines**, not one:

* **Desktop/Streamlit** — `data/*.json` (+ `.lock`/`.jsonl` siblings) and
  SQLite `data/runtime.db`, the execution source of truth for the desktop app
  and CLI.
* **Server** (planner/review/merge/worker) — PostgreSQL, provisioned by
  `command_center/db/*`. Some PostgreSQL tables are the *only* copy of their
  data (the backlog/planner store, the work-queue claim protocol, worker
  enrollment); most of the rest are dual-write **mirrors** of the SQLite/JSON
  authority above, built by the in-progress `VOYN-W0-AICC-SRV-01b` cutover —
  see the PostgreSQL section for which is which. `command_center/db/__init__.py`
  names the target state explicitly: moving the runtime store off SQLite onto
  this seam is SRV-01b's job, and **until that lands, `command_center.runtime.db`
  remains the authority for existing installs**. A table with a mirror module
  is an unfinished seam, not a second source of truth — do not treat one as a
  duplicate to delete.

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
| `model_entry`, `model_event` | Wave-3 model-registry engine — the AI-model catalog: one mutable current-state row per registered model plus its append-only governance log (`runtime/db/model_registry.py`; every write goes through the shared `connect()`/`transaction()` primitives, version-CAS on `model_entry`). `create_model_entry` writes the `register` event in the same transaction as the row; `set_model_status` only moves along `MODEL_STATUS_TRANSITIONS`. | db backup. |
| `motion`, `council_vote`, `council_decision`, `council_event` | Wave-3 Council/Board-of-Directors engine — the collective-decision pipeline Motion → Vote → Decision (`runtime/db/council.py`; version-CAS on `motion`). Votes are append-only with `UNIQUE(motion_id, voter_id)` enforced at this boundary (`DoubleVoteError`); a decision is written exactly once, in the same transaction that moves its motion to `decided`. | db backup. |

## PostgreSQL — server line (`command_center/db/*`)

Configuration is fail-closed (`command_center/db/config.py`): no usable
default host/database/user/password, `sslmode` defaults to `verify-full`.
Pooling is `command_center/db/pool.py`; schema is versioned SQL in
`command_center/db/sql/*.up.sql` applied by `command_center/db/migrations.py`
(bookkeeping table: `schema_migration`). Access is least-privilege by role
(`command_center/db/roles.py`, `PRIVILEGES`, enforced by
`tests/db/test_role_privileges.py` connecting *as each role*):
`aicc_migrator` (schema owner, DDL only), `aicc_app` (API/dispatcher, full DML
on domain tables, no DDL), `aicc_worker` (execution hosts — the least trusted
component; scoped to the queue-claim protocol, no governance tables), and
`aicc_operator` (host admission — no DML at all, EXECUTE-only on enrollment
functions). `command_center/db/roles.ALL_TABLES` is the canonical list of
every table the schema declares; the fitness gate below diffs this map
against it directly.

Two categories of table live here, and conflating them is exactly the mistake
this ticket exists to prevent:

### PostgreSQL-native — the server line's own authority (no SQLite equivalent)

| Table(s) | Authority | Writer | Recovery |
|---|---|---|---|
| `backlog_task`, `backlog_dependency`, `backlog_writer_lease`, `backlog_evidence`, `backlog_event`, `backlog_task_remediation` | The structured backlog store (BO-S1/BO-S2) — the planner-facing backlog: task status machine, dependency graph, a lease-guarded single-writer protocol (`backlog_writer_lease`), the audit trail, and review-cycle-remediation links back to the task that rejected a change. | `command_center/db/backlog_store.py` (`BacklogStore`), calling SECURITY DEFINER SQL functions in migration `0005_backlog_store` (+ `0010_review_cycle_remediation`) — the migration owns the status machine, cycle check and lease protocol so Python cannot duplicate the decision. During the migration period the backlog Markdown file is the incumbent authority; `BacklogStore.import_markdown` reconciles it via `backlog_upsert_task`, the one path allowed to set status directly. | Re-import from the backlog Markdown file (incumbent authority) + db backup. |
| `backlog_scan_cursor` | Per-scanner claim fence so two backlog scans never race. | `backlog_scan_claim()` SQL function (migration `0015_backlog_scan_cursor`), called directly by `command_center/orchestrator/review_merge.py`. | db backup. |
| `work_item`, `work_attempt`, `work_result`, `work_event` | The dispatcher→worker claim protocol — the queue a worker claims, heartbeats and completes/fails against. Ownership is a property of the grant graph, not a `WHERE` clause: `aicc_worker` holds **no table privilege at all** on any of the four, only `EXECUTE` on the protocol functions, so there is no second route to a claim to audit. | `command_center/db/work_queue_store.py` (`WorkQueueStore`) wraps `queue_enqueue`/`queue_claim`/`queue_heartbeat`/`queue_complete`/`queue_fail` (PL/pgSQL, migration `0002_queue_claim`). Recovery is a separate, more-trusted surface: `command_center/db/work_queue_admin.py` wraps `queue_reap`/`queue_redrive`, granted to `aicc_app` only — a worker may not reap or redrive its own fleet. `command_center/db/work_queue_read.py` is read-only (control-plane status, no protocol calls). | `queue_reap()` requeues or dead-letters lapsed leases; `work_dlq` view is the audited dead-letter trail; db backup. |
| `principal`, `principal_credential`, `principal_event`, `enrollment_ticket`, `worker_host_fingerprint` | Worker identity & enrollment (SRV-06) — which hosts may hold an `aicc_worker` credential. Deliberately unreachable from the control plane: admitting or evicting a host must not be something a compromised `aicc_app` credential can do. | SQL functions in migration `0003_worker_enrollment` (`identity_*`, `enroll_*`), granted to `aicc_operator` only. No Python wrapper exists yet — enrollment is operated directly against the database by an operator credential; only the grants are declared in code (`roles.py`). | `identity_sweep_expired()` / `enroll_sweep_expired()`; db backup. |
| `run_finalization_claim` | Host-local PID/start-identity fence for run finalization — reserved schema, not yet a writer target. | **None yet.** Migration `0016_run_finalization_claim`'s own header states the SQLite authority does not (and structurally cannot) dual-write this row — PID/start identity only means something on the execution host — and the table is `REVOKE ALL ... FROM PUBLIC` until a PostgreSQL-native claim implementation lands. | N/A — schema-only. |
| `schema_migration` | The migration runner's own bookkeeping (applied migration versions); not a domain store. | `command_center/db/migrations.py` | Re-derived from the `sql/*.up.sql` file list. |

### Mirrors of the SQLite/JSON authority (`VOYN-W0-AICC-SRV-01b`, cutover in progress)

Every row below is a **dual-write mirror**, not a second authority: the
SQLite/JSON writer named in the "Authority (unchanged)" column commits first,
then calls a `_mirror_*` hook that upserts the same row into PostgreSQL via
the module in the "Mirror module" column. The hook is best-effort and silent
on failure (mirror drift is caught by reconciliation, never surfaced to the
caller), and reads are **not** switched to PostgreSQL yet — the authority
column is unchanged from the SQLite table above until SRV-01b's reconciliation
plus rollback/backup-restore drills clear the cutover.

| Table(s) | Authority (unchanged) | Mirror module |
|---|---|---|
| `task`, `session` | `data/runtime.db` (`command_center/runtime/db.py`) | `execution_store.py` — also mirrors the `ON DELETE CASCADE` `delete_task` relies on. |
| `run` | `data/runtime.db` | `run_store.py` |
| `run_event`, `report` | `data/runtime.db` | `run_children_store.py` |
| `completion`, `completion_event`, `completion_validation` | `data/runtime.db` (`runtime/db/completion.py`) | `completion_store.py` |
| `run_provenance`, `provenance_evidence`, `run_provider_route`, `provider_attempt` | `data/runtime.db` (`runtime/db/provenance.py`) | `provenance_store.py` |
| `proposal`, `proposal_event`, `proposal_evidence` | `data/runtime.db` (`runtime/autonomy_service.py` via `runtime/db/proposal.py`) | `proposal_store.py` |
| `queue_entry` | `data/execution_queue.json`, via the `runtime.db` `queue_entry` read-only mirror | `queue_store.py` — a mirror of a mirror; the JSON file stays authoritative throughout. |
| `advisor_proposal` | `data/runtime.db` (`runtime/db/wave1.py`) | `advisor_store.py` |
| `owner_item` | `data/runtime.db` (`runtime/db/wave1.py`) | `owner_item_store.py` |
| `digest_item` | `data/runtime.db` (`runtime/db/wave1.py`, `command_center/digest`) | `digest_item_store.py` |
| `conflict` | `data/runtime.db` (`runtime/db/conflict.py`) | `conflict_store.py` |
| `audit_run`, `audit_finding` | `data/runtime.db` (`runtime/db/audit.py`) | `audit_store.py` |
| `market_item`, `market_install_log` | `data/runtime.db` (`runtime/db/marketplace.py`) | `marketplace_store.py` |
| `contact`, `message`, `networking_invitation` | `data/runtime.db` (`runtime/db/networking.py`) | `networking_store.py` |
| `model_entry`, `model_event` | `data/runtime.db` (`runtime/db/model_registry.py`) | `model_registry_store.py` |
| `motion`, `council_vote`, `council_decision`, `council_event` | `data/runtime.db` (`runtime/db/council.py`) | `council_store.py` |

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
| `data/evolution_population.json` (+lock) | Evolutionary config-agent population (VOYN-MIN-EVOL): each config-agent's genes (`executor`/`max_attempts`/`timeout_seconds`/`wip_limit`), lineage (`generation`, `parent_ids`) and recorded `RunOutcome` metrics. A separate, additive axis from `orchestrator.routing.ROUTING_MATRIX` (which stays static) — nothing in the fleet's live dispatch path reads or writes this file yet; it is populated and evolved via `command_center/evolution/store.py`'s API only. Fail-closed parse (malformed entry ⇒ dropped, never a half-built config-agent). | `command_center/evolution/store.py` | Defaults are empty (no config-agents until `seed_population` is called); re-seed from `evolution.seeds.DEFAULT_SEED_CONFIG_AGENTS` and replay `record_outcome` from `runtime.db` run history. |

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

## Deployed truth

The deployed AICC is whatever exact-SHA checkout a given instance runs from
(e.g. `ai-command-center-production-8dd3b1f`, staging worktrees) — verified by
the product itself (Git Center reports repo root/HEAD). A dashboard may only
claim accepted/deployed SHAs backed by `run_provenance`; anything else renders
**Unknown**. `ROADMAP_STATE.md` and GitHub issues/PRs are projections for
humans: on conflict, remote GitHub state + runtime.db provenance win.
