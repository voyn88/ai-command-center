# AI Command Center — Current State

Updated: 2026-08-07

## 0. AI Command Center platform

Status: Active, local Streamlit implementation

Current position:
- `app.py` hosts the implemented Streamlit control application: 20 page handlers, 16 shown in the
  sidebar (chat, generated files, reports, and context open inside the project view).
- `data/tasks.json` is the default planning and Kanban store. `AICC_TASKS_BACKEND=aios` routes
  all task reads/writes through the AIOS Tasks API instead (requires `AICC_AIOS_URL` + `AICC_AIOS_TOKEN`).
  `tasks_repository.get_repository()` is the factory; `scripts/migrate_tasks_to_aios.py` provides
  one-shot migration. See CHANGELOG [Unreleased] §"AIOS Tasks backend (Sprint 4)" for limitations.
- `data/runtime.db` schema 11 is authoritative for asynchronous execution, completion, the
  persisted autonomy-proposal lifecycle, the execution-provider fields, the independent-review
  verdict, and the `queue_entry` mirror (ADR 0007 dual-write).
- Execution Center provides process supervision, streaming events, cancellation, timeouts and
  restart reconciliation; live process handles remain owned by the hosting Python process.
  Process-group supervision is fail-closed to POSIX `waitid(WNOWAIT)`, keeps the launch-time PGID
  pinned until descendants are drained, and separates OS exit from terminal-state/report
  finalization so slow persistence cannot cause a false timeout or late signal.
- Normal task-v2 launches require explicit confirmation before any provisioning, may create an
  isolated worktree offline, and fail closed unless source repository, expected branch, worktree
  isolation and configured status policy verify before process launch. On `origin/main` the
  worktree-isolation check still takes its source repository from the *project* config, so a
  project spanning more than one repository can still produce a false
  `workspace_belongs_to_repository` failure. The fix that uses the task's own `repository_path`
  when the workspace comes from the task (`launch_service.prepare_task_launch`, `744a09c`) exists
  only on the unpushed local branch — see the divergence note under Current boundaries.
- Application-owned execution-queue mutations hold a same-host cooperative OS advisory lock across
  the complete persisted read-modify-write cycle; raw queue primitives and lock-free reads remain.
- `ExecutionCenterAPI.plan_schedule` provides deterministic, explainable, read-only scheduling
  decisions. It creates no durable claim, queue entry or run and has no background driver.
- The autonomy proposal domain/API persists evidence, policy, approval and dispatch-boundary state.
  An operator approve/reject inbox (`ui/proposals_panel.py`) surfaces and decides proposals, but
  there are no automated evidence collectors, project policy resolver, background driver or executor.
- Portfolio Execution and Portfolio Overview provide guarded worktree launch plus read-only
  dependency, health, capacity and recommendation views.
- The persisted completion pipeline supports validation, push, pull-request and merge workflows;
  completion autopilot and automatic merge policies are opt-in and disabled by conservative
  defaults.
- Checked-in CI validates committed-diff whitespace, Ruff, byte compilation and pytest on Python
  3.14. Informational, non-blocking `mypy` and coverage steps run alongside the deterministic
  quartet but do not gate the merge.
- Runtime history retention is **off by default**: `AICC_RUNTIME_RETENTION_DAYS=<N>` prunes
  `run_event` rows for terminal runs older than `N` days on startup, and
  `AICC_RUNTIME_VACUUM_ON_START=1` reclaims disk with `VACUUM` afterward.
- `data/chats.json` and `data/activity.jsonl` remain active application stores alongside SQLite;
  legacy synchronous execution and the `data/runs.jsonl` journal also remain present.
- Founder Functional Audit `9761459` is **closed** (2026-08-07), merge-verified against
  `origin/main` @ `fb3da7f`. Of its 14 Still Open rows, 7 are **merged** and each was re-read in
  the code on `origin/main` (report-path containment, per-warning launch acknowledgement,
  Portfolio protected-branch guard, task-delete confirmation, `claude` pre-flight, Workspace Home
  intelligence, git ahead/behind + fetch); 1 is merged but still partial (Portfolio stale-claim
  recovery: service half only, no production call site); 1 is folded into the desktop D2 stage
  tasks; and 5 remain open. The seven merged rows are now `Done` in
  `docs/roadmap/MASTER_ROADMAP_TASKS.json` — until this pass all 13 `AICC-AUDIT-W*` rows read
  `Backlog` regardless of what had shipped. See
  `docs/audits/FOUNDER_FUNCTIONAL_AUDIT_9761459_STATUS.md` §"Merge verification".
- **The audit closure is enforced, not just written down.**
  `tests/architecture/test_audit_closure_fitness.py` (parsers and git probes in
  `tests/architecture/audit_closure.py`) parses the status document's merge-verification table and
  checks it against `MASTER_ROADMAP_TASKS.json` and against the code on the pinned commit
  `fb3da7f`: statuses must agree row for row, every `Done` row must cite one of the nine evidence
  commits, each evidence commit must be an ancestor of the pinned commit, each merged row's symbol
  must be readable there, the two probeable still-open rows must still be open, `W1-006` must keep
  no production call site for `recover_stale_claim`, and `W3-002`'s `AICC-D2*` fold targets must
  exist. The gate is mutation-tested — verified red under five mutations of the real artifacts and
  green when restored. This closes the reporting gap that let a documentation task pass validation
  as "1/1 commands passed" (the default `compileall`) while asserting anything at all about the
  repository. `tests/architecture/` — **17 passed**.
- Audit evidence set on `origin/main` @ `fb3da7f`: `tests/test_launch.py`,
  `tests/test_git_info.py`, `tests/test_runtime_report_path_containment.py`,
  `tests/test_report_path_containment.py`, `tests/test_workspace_home_ui.py`,
  `tests/test_portfolio_launch.py` — **170 passed, 0 failed** (ambient `AICC_*` stripped,
  `AICC_DATA_DIR`/`AICC_REPORTS_ROOT` redirected).

Current boundaries:
- Five audit remediations remain outstanding and are the known functional gaps: `scripts/start-task.sh`
  accepts 3 of the 11 registered project ids; there is no canonical task schema; there is no
  dependency-cycle detection on task writes (cycles surface only as a read-only `break_cycle`
  recommendation in Portfolio Overview); run results reach a task's Timeline only page-driven or
  under `AICC_BACKGROUND_SYNC`; and the autopilot has no founder batch-confirmation surface before
  it launches.
- Portfolio now refuses `main`/`master` (and their case variants) as a task branch before
  `git worktree add` runs — `portfolio_launch.PROTECTED_BRANCH_NAMES`, checked on the
  `launch_portfolio_task` → `build_launch_plan` → `resolve_branch` path, which returns the blocker
  before `create_worktree` is called. Stale-claim recovery is still service-only:
  `recover_stale_claim` exists but has no production call site, so an orphaned claim is still
  cleared by deleting `data/portfolio_locks/<task_id>.lock` by hand.
- `data/tasks.json` is out of sync with the roadmap for the audit-remediation track: it holds 7 of
  13 `AICC-AUDIT-W*` rows, and only 2 of those 7 both are correct and read correctly. Three carry a
  status contradicting `origin/main` (`W0-006` and `W1-004` sit in `Backlog` though shipped;
  `W1-005` is still `In Progress` against a closed PR though the fix has shipped) and two more
  (`W1-007`, `W2-004`) are `Done` and correct but read as failed in the UI. `launch_status` is
  `"Requires Attention"` on 6 of the 7 and carries no signal on this track. The roadmap JSON side
  of this gap is now closed (the seven merged rows are `Done`); the `data/tasks.json` side is not.
  Reconciliation is tracked as `AICC-GOV-F2`; a refreshed audit against current `main` is tracked
  as `AICC-GOV-F4B`.
- **Local `main` has diverged from `origin/main`** (re-checked after `git fetch`, 2026-08-07):
  local `main` holds 3 unpushed commits (`744a09c`, `c41e9bd`, `9553fd6`) and `origin/main`
  `fb3da7f` holds 8 the local branch lacks; neither is an ancestor of the other. Nothing on the
  audit-remediation track depends on this — every closure claim is verified against `fb3da7f`, not
  against the local branch — but the divergence itself is unresolved and grows with each local
  commit. Two consequences:
  - The previously recorded "known regression" — `app.py:3339` calling the removed
    `execution_queue.reconcile_missing_run_links` — is **fixed on `origin/main`** by `b2134c4`,
    which restores the function (`execution_queue.py:907`). It still reproduces on the local
    branch, which predates the fix, and clears when that branch is brought up to date. No task
    needed.
  - The local-only fix `744a09c` (task-level `repository_path` for workspace isolation) is **not
    on `origin/main`**: `prepare_task_launch` there still takes `source_repository_path` from the
    project config only. It needs to be pushed or re-landed before it can be described as
    delivered.
- Normal task launches require explicit user action. Scheduler `ASSIGN` results are point-in-time
  advice, not persisted claims; task-id/capacity decisions may race before the separate launch, and
  only exact-workspace exclusion is enforced transactionally by the runtime launch path.
- Fail-closed workspace verification is scoped to normal task-v2 callers that supply
  `WorkspaceSpec`; low-level/ad-hoc launches preserve their separate behavior.
- Whether the current plan exposes working branch protection/rulesets on `main` is not verified —
  three classic-protection fields were checked and found permissive, but rulesets, bypass actors
  and every other field are unaudited. The actual merge gate is application code
  (`merge_once`/`_pr_is_mergeable`), independent of GitHub's tier either way. See
  [docs/operations/GITHUB_MERGE_ENFORCEMENT_DECISION.md](docs/operations/GITHUB_MERGE_ENFORCEMENT_DECISION.md)
  for the full audit status and the accepted-risk decision.
- Git worktree creation, push, pull-request creation and merge are privileged capabilities with
  confirmation or policy safeguards.
- The native PySide6 desktop client remains documentation and design work only.
- The runtime is local and process-hosted, not a distributed or production-ready worker platform.

## 1. AIOS

Status: Active

Current position:
- P0 completed.
- P1 API, authentication, OpenAPI contract and SDK work in progress.
- Product, specifications, architecture and commercial streams run in parallel.

Current priority:
- Complete the active P1 development sequence.
- Keep one agent = one worktree = one branch.
- Require independent review before commit and merge.

Next decision:
- Confirm the exact next P1 task from the current repository state.

---

## 2. Bank Strategy

Status: Active

Current objective:
- Implement the new process across the entire organization.
- Provide the Management Board with measurable monthly implementation control.

Current priority:
- Finalize one executive slide.
- Define 30/60/90-day milestones.
- Define monthly metrics, owners, deviations and corrective actions.

Next deliverable:
- Board implementation and execution-control dashboard.

---

## 3. Legal

Status: Active

Current areas:
- Claim and supporting documents.
- Criminal-case evidence.
- Correspondence and payment analysis.
- Evidence preservation and procedural submissions.

Operating rule:
- Separate evidence extraction, calculations, legal analysis and document drafting.

---

## 4. Business

Status: Active

Current areas:
- Open Book logistics model.
- Investment and loan documentation.
- Corporate and counterparty matters.

---

## 5. Personal

Status: Ongoing

Use for:
- Personal communications.
- Automotive issues.
- Scheduling and household tasks.

---

# Global Operating Rules

1. One project must not be mixed with another project.
2. One agent = one task = one branch = one worktree.
3. Every technical implementation requires independent review.
4. No commit, push, pull-request creation or merge without explicit authorization.
5. Every task must have a measurable Definition of Done.
6. Closed architectural decisions must not be reconsidered without new evidence.
7. Current-state files are the primary source of project context.
