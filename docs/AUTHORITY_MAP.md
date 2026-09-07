# AICC Transactional Authority Map (NIGHT-W9-AICC-AUTHORITY)

One documented source of truth per store and per field family: **exactly one
writer module**, everyone else reads. A fitness test
(`tests/architecture/test_authority_map.py`) fails when a store appears under
`data/` that this map does not name, or when a table or view exists in the
PostgreSQL server schema (`command_center/db/roles.ALL_TABLES` /
`ALL_VIEWS` — the same inventory the grant matrix and the migration tests
check the live catalog against) that this map does not mention — a new store
cannot ship undocumented in either deployment.

Two deployment lines exist side by side, not one:

* **Desktop/Streamlit** — `data/*.json` (+ locks) and the SQLite
  `data/runtime.db`, covered below.
* **Server** (planner/review/merge/worker) — PostgreSQL, covered in
  "PostgreSQL — the server deployment" below. `command_center/db/__init__.py`
  states the target explicitly: moving the runtime store off SQLite onto this
  seam is follow-up slice **VOYN-W0-AICC-SRV-01b**; *until that lands,
  `command_center.runtime.db` remains the authority for existing installs*.
  That is why most of the PostgreSQL schema is documented below as a
  **mirror** of the SQLite tables above rather than as a second authority —
  the unfinished migration is a documented seam, not an undocumented
  duplicate.

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
| `task`, `session`, `run`, `run_event`, `report`, `run_finalization_claim` | Execution lifecycle: what actually ran, its pid/state/events, and the durable CAS claim (`run_finalization_claim`, `runtime/db/execution.py`) that fences two processes from finalizing the same run. Runs are reconciled against live OS processes (`Supervisor.reconcile`) — never guessed. | `runtime/maintenance.py`: backup → cold archive (gzip JSONL + sha256) → prune → integrity; `restore_backup()` is the proven rollback (rehearsed on a copy of the real 346MB db, #193). |
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
| `motion`, `council_vote`, `council_decision`, `council_event` | Council engine — a motion put to the fleet, one vote per voter (`UNIQUE (motion_id, voter_id)`), the resulting decision, and its event trail (`runtime/db/council.py`, written via `command_center/council` — the `command_center.council.service` API tier; `create_motion` and the vote/decide/event calls it drives). | db backup. |
| `model_entry`, `model_event` | Wave-3 Model registry — one mutable current-state row per registered AI model (external or local) plus its append-only governance log (`runtime/db/model_registry.py`, written via `api/model_registry_service.py`). `create_model_entry()` writes the row and its `register` event in one transaction, so a model can never exist without the first entry in its history; `set_model_status()` only moves along `MODEL_STATUS_TRANSITIONS`. | db backup. |

## PostgreSQL — the server deployment (`command_center/db/*`)

The server line (planner/review/merge/worker) runs entirely against
PostgreSQL, provisioned by the migrations under `command_center/db/sql/` and
gated by the least-privilege matrix in `command_center/db/roles.py`
(`ALL_TABLES` / `ALL_VIEWS`, which the schema tests and the live-catalog grant
checker in `tests/db/test_grant_compliance.py` both hold the database to). Its
schema splits into two families with different authorities:

* **Mirrors of the SQLite tables above** — one-way, SQLite → PostgreSQL,
  written by a per-table (or per-family) mirror module built on the shared
  machinery in `command_center/db/mirror_support.py` and
  `command_center/db/table_mirror.py`. *SQLite stays the writer of record*;
  the PostgreSQL copy exists so the server line can join against execution
  history without a cross-database call, and is disposable — rebuildable from
  SQLite — rather than a second place where loss can happen.
* **Server-native tables with no SQLite counterpart** — the backlog/planner
  store, the worker queue-claim protocol, and the fleet identity/enrollment
  protocol. None of these exist on the desktop/Streamlit line at all, so
  *PostgreSQL is their sole authority*, each written through exactly one
  PL/pgSQL function surface (never ad hoc `UPDATE`/`INSERT` from Python — see
  `roles.py`'s docstring on why the queue-claim and backlog protocols are
  granted `EXECUTE`-only, not table DML).

### A. Mirrors of the SQLite authority (SQLite above remains the writer of record)

| Tables | Authority | Mirror writer |
|---|---|---|
| `task`, `session` | See the SQLite execution-lifecycle row above. | `command_center/db/execution_store.py` |
| `run`, `run_event`, `report` | See the SQLite execution-lifecycle row above. | `command_center/db/run_store.py` (`run`); `command_center/db/run_children_store.py` (`run_event`, `report`) |
| `completion`, `completion_event`, `completion_validation` | See the SQLite completion-pipeline row above. | `command_center/db/completion_store.py` |
| `run_provenance`, `provenance_evidence`, `run_provider_route`, `provider_attempt` | See the SQLite provenance/routing rows above. | `command_center/db/provenance_store.py` |
| `proposal`, `proposal_event`, `proposal_evidence` | See the SQLite proposal row above. | `command_center/db/proposal_store.py` |
| `advisor_proposal` | See the SQLite Wave-1 row above. | `command_center/db/advisor_store.py` |
| `owner_item` | See the SQLite Wave-1 row above. | `command_center/db/owner_item_store.py` |
| `digest_item` | See the SQLite Wave-1 row above. | `command_center/db/digest_item_store.py` |
| `conflict` | See the SQLite Conflicts/Incidents row above. | `command_center/db/conflict_store.py` |
| `audit_run`, `audit_finding` | See the SQLite Audit row above. | `command_center/db/audit_store.py` |
| `market_item`, `market_install_log` | See the SQLite Marketplace row above. | `command_center/db/marketplace_store.py` |
| `model_entry`, `model_event` | See the SQLite Model registry row above. | `command_center/db/model_registry_store.py` |
| `contact`, `message`, `networking_invitation` | See the SQLite Networking row above. | `command_center/db/networking_store.py` |
| `motion`, `council_vote`, `council_decision`, `council_event` | See the SQLite Council row above. | `command_center/db/council_store.py` |
| `queue_entry` | Mirror of a mirror: authoritative source is `data/execution_queue.json`, same as the SQLite `queue_entry` mirror row above. | `command_center/db/queue_store.py` |

Recovery for the whole family is uniform: drop and re-run the relevant mirror
sync — every row here is re-derivable from the SQLite (or, for `queue_entry`,
JSON) authority, so a corrupted PostgreSQL mirror is an inconvenience, never a
loss.

### B. Server-native tables (no SQLite counterpart — PostgreSQL is the sole authority)

| Tables | Authority | Writer | Recovery |
|---|---|---|---|
| `backlog_task`, `backlog_dependency`, `backlog_evidence`, `backlog_event`, `backlog_writer_lease`, `backlog_task_remediation`, `backlog_scan_cursor` (+ view `backlog_eligible`) | The structured backlog/planner store (BO-S1/S2/S3): task intake, triage, dependency edges, remediation evidence, the planner's dispatch and lease protocol. Every state transition is a status-machine step inside a SECURITY DEFINER function — there is no SQL path that jumps a state directly. | The `backlog_*` PL/pgSQL functions defined across `command_center/db/sql/0005`–`0015` (`backlog_upsert_task`, `backlog_transition`, `backlog_record_evidence`, `backlog_record_remediation`, `backlog_add_dependency`, `backlog_lease_acquire/heartbeat/release`, `backlog_dispatch`, `backlog_ingest_results`, `backlog_return_to_pool`, `backlog_resume_deferred`, `backlog_scan_claim`, `backlog_triage`), called only through `command_center/db/backlog_store.py`. | Re-derivable from `command_center/db/backlog_export.py`'s Markdown projection plus `backlog_event`'s append-only audit trail; no separate backup path yet. |
| `work_item`, `work_attempt`, `work_result`, `work_event` (+ views `work_item_public`, `work_attempt_public`, `work_dlq`) | The worker queue-claim protocol (`0002_queue_claim`): one exclusive claim per item, a durable result before acknowledgement, an audited dead-letter exit. `aicc_worker` holds **no table privilege at all** here — see `roles.py` — so the functions are the only route to a state change. | `queue_claim`/`queue_heartbeat`/`queue_complete`/`queue_fail` (the claim path, called from `command_center/db/work_queue_store.py`) and `queue_reap`/`queue_redrive` (the recovery path, called from `command_center/db/work_queue_admin.py`); both function sets are PL/pgSQL in `command_center/db/sql/0002_queue_claim.up.sql`. | Lapsed leases self-heal via `queue_reap()`; `work_dlq` is the audited exit for items that exhaust their attempt budget. |
| `principal`, `principal_credential`, `principal_event`, `enrollment_ticket`, `worker_host_fingerprint` (+ views `principal_credential_public`, `enrollment_ticket_public`) | The fleet identity/enrollment protocol (`0003_worker_enrollment`): admitting an execution host, minting and rotating its credential, revoking one an incident retires. `principal_credential` and `enrollment_ticket` are granted to **nobody** directly — they hold the capability hashes themselves; the `_public` views are the only read path. | `enroll_mint_ticket`/`enroll_revoke_ticket`/`enroll_sweep_expired`/`identity_revoke_principal` (operator levers, called from `command_center/db/fleet_admin.py`); `enroll_redeem_ticket`/`identity_assert`/`identity_current_credential`/`identity_sweep_expired` (host bootstrap, called from `command_center/worker/daemon.py`); `enroll_rotate_self` (self-service rotation, called from `command_center/ops/credential_rotation.py`). All are PL/pgSQL in `command_center/db/sql/0003_worker_enrollment.up.sql`. | `worker_host_fingerprint` is the forensic trail an operator classifies a re-enrollment against; no other recovery path — a lost row is a lost history entry, not a lost admission decision (`principal.state` is what gates access). |
| `run_finalization_claim` | Declared placeholder only: migration `0004_run_finalized_at` creates the table but grants **no** role any privilege on it (see `roles.py`'s `_FINALIZATION_CLAIM_TABLES`) — a future PostgreSQL authority must expose a dedicated CAS function, the same shape `runtime/db/execution.py`'s SQLite `run_finalization_claim` already has (see the SQLite execution-lifecycle row above), before this table has a writer. | None yet — unreachable by every role. | N/A until a writer exists. |
| `schema_migration` | The migration runner's own version ledger — which numbered migrations have been applied to this database. | `command_center/db/migrations.py` (the runner itself; no other module writes it). | Re-derivable by re-running the migration set against a fresh database; the SQL files under `command_center/db/sql/` are the source of truth for what the ledger should contain. |

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

## Deployed truth

The deployed AICC is whatever exact-SHA checkout a given instance runs from
(e.g. `ai-command-center-production-8dd3b1f`, staging worktrees) — verified by
the product itself (Git Center reports repo root/HEAD). A dashboard may only
claim accepted/deployed SHAs backed by `run_provenance`; anything else renders
**Unknown**. `ROADMAP_STATE.md` and GitHub issues/PRs are projections for
humans: on conflict, remote GitHub state + runtime.db provenance win.
