# AICC Transactional Authority Map (NIGHT-W9-AICC-AUTHORITY)

One documented source of truth per store and per field family: **exactly one
writer module**, everyone else reads. A fitness test
(`tests/architecture/test_authority_map.py`) fails when a store appears under
`data/` that this map does not name — a new store cannot ship undocumented.

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

## Merge enforcement authority (VOYN-W0-AICC-BRANCH-PROTECTION-LIMIT)

GitHub-native branch protection is **not** the enforcement point for `main`.
Confirmed directly against the live API on 2026-09-02 via
`gh api repos/<owner>/<repo>/branches/main/protection`:
`required_approving_review_count=0`, `required_status_checks` is absent, and
`enforce_admins=false`. The current plan/repository has no branch protection
or ruleset configured, so GitHub itself will accept a merge to `main` with no
passing check and no review — anyone with direct write access can merge
around every gate described below through the GitHub UI/API.

Several parts of this codebase were written assuming GitHub-side enforcement
existed: `.github/workflows/acceptance-gate.yml`; the merge-queue handling in
`command_center/orchestrator/review_merge.py` (its comments describe "the
required Acceptance-gate check" and the merge queue refusing a red run); and
`scripts/assert_independent_acceptance.py`'s design note, which documents a
required-status-check contract. That contract is **not configured at the
GitHub level today** — whatever those flows deliver is only as strong as
"nobody merges outside them," which GitHub is not enforcing.

The enforcement that is actually real is **application-level, not
GitHub-level**:

* Only `command_center/runtime/completion_service.py` (via `git_ops` and
  `GitHubClient`) ever pushes or merges — agents and their CLIs never hold
  push/merge credentials. See
  [`docs/adr/0010-agent-publisher-principal-isolation.md`](adr/0010-agent-publisher-principal-isolation.md):
  the queue worker/guarded publisher is a distinct Unix principal from the
  isolated, credential-scrubbed per-run agent unit.
* It refuses to merge when checks are failing or a required review is
  absent — evaluated by `CompletionEvaluator`/`CompletionPolicy`
  (`command_center/runtime/completion.py`); review independence specifically
  is evaluated by the acceptance-gate CI check
  (`scripts/assert_independent_acceptance.py`), which exists because
  GitHub's own `required_approving_review_count` cannot express "approved by
  an identity that is not the author" for this repository's reviewer
  identity model.

This is a discipline enforced by *who holds credentials and what code they
run*, not a GitHub setting, so it does not stop a human with direct
repository write access from merging manually or an admin from disabling the
workflow. Until real required checks are visible in
`gh api repos/<owner>/<repo>/branches/main/protection`
(`required_status_checks` populated and `required_approving_review_count >= 1`,
or an equivalent ruleset), **no documentation or dashboard in this repository
may describe GitHub branch protection as an active control.** The completion
pipeline / guarded publisher above is the only enforcement point that may be
claimed as real. `CURRENT_STATE.md` carries the standing operational note;
this section is the source of truth for the underlying authority claim.

## Deployed truth

The deployed AICC is whatever exact-SHA checkout a given instance runs from
(e.g. `ai-command-center-production-8dd3b1f`, staging worktrees) — verified by
the product itself (Git Center reports repo root/HEAD). A dashboard may only
claim accepted/deployed SHAs backed by `run_provenance`; anything else renders
**Unknown**. `ROADMAP_STATE.md` and GitHub issues/PRs are projections for
humans: on conflict, remote GitHub state + runtime.db provenance win.
