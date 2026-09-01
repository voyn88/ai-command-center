# Changelog

All notable changes to AI Command Center are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
This project does not yet follow strict semantic versioning tags in Git; versions below refer to
functional application milestones of `app.py`.

## [Unreleased]

### Added — dependency proof before compensation (`VOYN-W0-AICC-SRV-04b-DEPENDENCY-FIRST`)
- `docs/adr/0011-claim-does-not-depend-on-repository-lease.md`: proves, against
  the current code and test suite, that the execution-attempt claim
  (`0002_queue_claim`) has no dependency on the AIOS platform repository
  lease — so no failure-compensation layer is needed in the claim protocol.
  Records where a real dependency does exist (the worktree mutation site,
  `worktree_lease`/`writer_lease`) and why it is correctly fail-closed there.
- `command_center/db/sql/0002_queue_claim.up.sql`: corrected a stale header
  comment — `principal` is no longer "a design proposal, not a deployed
  table" (`0003_worker_enrollment` created it); the independence conclusion
  is unaffected since `0002` still has no reference to it and no join exists
  from `work_attempt.claimed_by_role`.

### Added (SRV-05 slice 2)
- `command_center/worker/payloads.py` — versioned `agent_run` payload contract
  (v1): refusals as data, timeout bounded by the queue's visibility ceiling,
  provenance defaults to untrusted.
- `command_center/worker/handlers.py` — the payload→execution bridge through
  the existing `agent_runner.run_claude_code` (sandbox profiles, credential
  scrubbing and timeouts stay the runner's decisions); untrusted mutating
  payloads are refused, not silently downgraded; results travel as bounded
  tails.
- `deploy/systemd/aicc-worker.service` — declared cgroup resource envelope
  (MemoryMax/MemoryHigh/CPUQuota/TasksMax); sandbox-directive acceptance
  stays measurement-from-inside per SRV-05-B.

### Added — the headless worker service (`VOYN-W0-AICC-SRV-05`, slice 1)

- `command_center/db/work_queue_store.py`: the first Python surface over the
  `0002_queue_claim` protocol — until now `queue_claim`/`queue_heartbeat`/
  `queue_complete`/`queue_fail` had no caller outside tests. Token generated
  locally, only its SHA-256 travels on claim; refusals are data, not
  exceptions.
- `command_center/worker/`: the claim-execute-report daemon. Heartbeat runs
  beside the handler and stops the work when the lease is lost; SIGTERM
  finishes the item in hand and claims no more; an unknown payload kind is a
  non-retryable failure; auth loss exits non-zero and leaves restart pacing to
  systemd. Handlers are a registry — the bridge from a claimed payload to a
  real agent run is deliberately a follow-up slice, because the `execution`
  payload schema and its producer do not exist yet.
- `deploy/systemd/aicc-worker.service`: the first systemd unit in the repo.
  `TimeoutStopSec` outlives the visibility window so a healthy handler is
  never killed mid-item; hardened (`NoNewPrivileges`, `ProtectSystem=strict`).
- Test conftest guards: two autouse fixtures hard-imported streamlit (directly
  and via `app.py`), turning every test on a headless host red at setup — the
  exact host the worker ships to. Both are now guarded, with the reason
  recorded at the guard.

### Security — container deployment no longer exposes the console (`VOYN-W0-AICC-STREAMLIT-EXPOSED-NO-AUTH`)

An earlier audit (`9761459`, BLOCKER-1) pinned the Streamlit console to localhost for the bare
`streamlit run` and `scripts/start-ui.sh` paths, but no test guarded that decision and the container
deployment path reintroduced the same exposure: `scripts/aml-entrypoint.sh` defaulted
`--server.address` to `0.0.0.0` and `docker-compose.aml.yml` published the port unqualified, so a
routine `docker compose up` put an unauthenticated console that performs privileged git/gh and
subprocess operations on every host interface — below any host firewall rule, since Docker installs
its own.

- `scripts/aml-entrypoint.sh` no longer has a default bind address. It exits `78` (`EX_CONFIG`) with
  an explanatory message unless `STREAMLIT_SERVER_ADDRESS` is set, so the choice cannot be inherited
  unseen.
- `docker-compose.aml.yml` publishes on `${AML_BIND_HOST:-127.0.0.1}` instead of every interface, and
  states the container-internal `0.0.0.0` explicitly with the reason it is correct there.
- `tests/test_deployment_exposure.py` gates all four launch paths, executing the entrypoint rather
  than pattern-matching it.

This closes the *exposure*, not the underlying absence of authentication: the console still has no
auth layer, which is tracked separately as `AUTH-HTTP-01`. Widening `AML_BIND_HOST` therefore still
means publishing an unauthenticated privileged surface.

### H1 sprint history

**H1** is the committed-next horizon defined by
[`docs/roadmap/MASTER_PRODUCT_ROADMAP.md`](docs/roadmap/MASTER_PRODUCT_ROADMAP.md) under
`DR-ROADMAP-AUTHORITY-001`. It commits to one goal — a **native-desktop, local-first, single-user
developer control plane** that reaches and then exceeds the Streamlit feature set, with fail-closed
safety on every privileged action — across three tracks: **Desktop Increment 1** (15 rows),
**Audit remediation** (13 rows), and **Governance** (6 rows).

This section is the chronological index for that horizon. Every date is derived from the commit
history on `main` (first appearance of the relevant module or document), not from planning
documents; the detailed entries for each item are the sections below and in the released versions.
Work that ran alongside H1 but is not one of its three tracks is listed separately.

#### Timeline

| Date | Milestone | Track |
|---|---|---|
| 2026-07-15 | `1.0.0` initial Streamlit application; `1.1.0` (Sprint 2) Executive Dashboard, Command Palette, Focus Mode, Timeline, AI Agents, Smart Tasks, Git Center, Workspace Launcher | Pre-H1 baseline |
| 2026-07-16 | `1.2.0` `command_center/` package, Project Chat, Claude Code runner, report parser; **Sprint 1** v2 Session Supervisor + `runtime.db` (ADR 0003) | Pre-H1 baseline |
| 2026-07-17…18 | **Sprint 3** Workspace Home — read model, redaction stage, `git_info`/`artifacts` extraction, Workspace Home page | Platform |
| 2026-07-18 | **Desktop D0** — the canonical `docs/desktop/` documentation set (vision, architecture, IA, design directions, design system, Workspace Home spec, platform behavior, frozen D1–D4 scope, implementation roadmap). Documentation only | Desktop |
| 2026-07-19 | Execution queue, Kanban state separation, upgraded recommendations | Platform |
| 2026-07-20 | Portfolio task planning and safe launch | Platform |
| 2026-07-21 | Founder Functional Audit `9761459` recorded | Audit remediation |
| 2026-07-22 | Autonomous Task Completion Pipeline (`AICC-AUTONOMY-001`, ADR 0004) | Platform |
| 2026-07-23 | Autonomy Proposal Foundation (`AICC-AUTONOMY-002`, ADR 0005) | Platform |
| 2026-07-27 | **D1A–D1C** — the native PySide6 shell lands: `QApplication` assembly, `AppShell` main window, sidebar/top bar, nine-section navigation, theme controller, settings and window-geometry persistence | Desktop |
| 2026-07-28 | `MASTER_PRODUCT_ROADMAP.md` — the `AICC-D1-001` epic decomposed into 15 rows (11 increments + 4 gates) | Governance |
| 2026-07-29 | **D2A** application adapter, **D2B** `QThreadPool` worker framework, **D2C** status/card/row components and live-data wiring, Russian UI + i18n registry with an automated language gate | Desktop |
| 2026-07-29 | Audit batch: Copilot executor fails closed for untrusted tasks (`SEC-1`/`D-01`); full read-modify-write lock on project/portfolio config (`AR-5`); Done tasks not backed by a verified completion reported (`DATA-D1`) | Audit remediation |
| 2026-07-30 | **D2D** edge states, loading skeletons, accessibility, and a BANK/LEGAL redaction regression test; **D3A** Projects page with repository paths; **D3B** persistent settings form and platform preference abstraction; read-only AIOS Core status and provider-readiness boundary | Desktop |
| 2026-07-31 | **D4A** unsigned macOS bundle and **D4B** unsigned Windows 11 x64 bundle via PyInstaller; self-contained Windows runbook | Desktop |
| 2026-08-01 | **D1 final gate**: macOS Apple Silicon PASS recorded on real hardware; Windows interactive leg still blocked (no hardware). `windows-latest` CI job added for the automated half | Desktop |
| 2026-08-03 | `run-desktop` project skill (verified macOS launch recipe); live workspace data resolved correctly inside the packaged app; autonomous daily-audit publication and shutdown fenced | Desktop / Governance |
| 2026-08-03…06 | AML Service phases 1–7 — risk scoring, rule engine, evidence store, case management, 115-ФЗ country pack, Docker, bank acceptance package | Alongside H1 |
| 2026-08-04 | Report-derived child tasks stamped `untrusted_import` (`SEC-D-02`); PID-reuse recovery covered and single-host lock scope documented | Audit remediation |
| 2026-08-06 | **Sprint 4** AIOS Tasks backend behind `AICC_TASKS_BACKEND`; ESF/AML project registry; D2 Native Workspace Home tests + `ErrorState` widget | Platform / Desktop |
| 2026-08-07 | Task-aware executor preflight; load-aware executor selection; `TasksStoreUnreadable` instead of a silent empty read; audit-closure verdict turned into an executable gate with W1/W2 remediation confirmed merged | Audit remediation / Governance |

#### Track 1 — Desktop Increment 1

The D1A→D4 sequence is the approved decomposition of the `AICC-D1-001` epic and closes the §2.1
desktop parity gate. As of 2026-08-07 the **code** for D1A through D4B has landed on `main`:

- `command_center/desktop/` is a working PySide6/Qt Widgets client launched with
  `python -m command_center.desktop`. Its startup path imports PySide6 and nothing else — no
  `app.py`, no Streamlit, no HTTP client — enforced by `tests/desktop/test_lifecycle.py` running a
  clean interpreter.
- Three of the nine sections are active (Home, Projects, Settings); the other six render visibly
  disabled so the sidebar never reflows between increments.
- Home is a native Workspace Home over `command_center.application.WorkspaceHomeAdapter`, a thin
  wrapper that returns `build_workspace_home_snapshot`'s output unchanged, inheriting every
  BANK/LEGAL redaction guarantee verbatim. It loads through the D2B worker framework, so the GUI
  thread is never blocked.
- The client is read-only except for repository-path configuration, theme/density preferences, and
  window geometry — binding decisions 11 and 12 of `DESKTOP_INCREMENT_1.md`.
- `tests/desktop/` is an offscreen pytest-qt suite: **175 passed** as of 2026-08-07.
- Packaging produces **unsigned development bundles** for macOS Apple Silicon and Windows 11 x64.
  No signing, notarization, or auto-update exists.

**Gates remain open.** `AICC-D1-GATE` is still **Review**, not Done: the interactive Windows 11 x64
acceptance pass has never been performed on real hardware, and that gate's forbidden-scope note
required it to close before `AICC-D2A` began. The D2/D3/D4 implementations landed anyway, so the
verification gates lag the merged code rather than leading it.

#### Track 2 — Audit remediation

The Still-Open rows of Founder Functional Audit `9761459`, closing the §2.2 safety gate and §2.5
audit-closure gate. Landed across 2026-07-29 → 2026-08-07: fail-closed handling of untrusted tasks
in the Copilot executor, provenance stamping on report-derived child tasks, a full read-modify-write
lock on project and portfolio configuration, per-warning launch confirmation (each warning
acknowledged under its own stable issue code, with the launch blocked until every one is ticked),
Done tasks reported when not backed by a verified completion, and a store-read failure that now
raises `TasksStoreUnreadable` instead of silently returning an empty list.

The closure verdict itself was made **executable** on 2026-08-07 rather than left as prose, and the
W1/W2 remediation set was confirmed merged.

#### Track 3 — Governance

The `§8` required follow-ups F1–F5 of the authority record, closing the §2.3 data-integrity gate and
§2.4 documentation-truth gate: the canonical master roadmap and its machine-readable companion
(2026-07-28), audit reconciliation and current-state updates, an AIOS boundary fitness baseline, and
fencing of autonomous daily-audit publication and shutdown.

#### Alongside H1 — not one of the three tracks

The AML Service (phases 1–7, 115-ФЗ compliance, Docker packaging, bank acceptance package) and the
ESF/AML project registry additions landed during the H1 window but are outside the horizon's three
committed tracks — recorded here so the timeline is not read as an H1 scope claim.

#### Known status drift

`MASTER_PRODUCT_ROADMAP.md` is a planning snapshot reconciled against `main` @ `bd9f05b` on
2026-07-28 and still lists `AICC-D2A` through `AICC-D4-GATE` as **Backlog**. The corresponding code
merged between 2026-07-29 and 2026-07-31. `docs/desktop/README.md` and `CURRENT_STATE.md` likewise
still describe the desktop client as a pure shell with no data wiring, or as documentation and design
work only. The code, its tests, and this changelog are the current authority; those three documents
need reconciliation.

### HTTP authentication for the mutating API surfaces (VOYN-W0-AICC-AUTH-HTTP-01) — 2026-08-15

Both FastAPI applications served no authentication at all. Every mutating route
now requires a verified platform principal and an explicit AICC-local grant.

#### Added
- **`command_center/http_auth/`** — `identity.py` (forwards the caller's platform
  bearer credential to `GET /api/v1/whoami`; stores no credential, hashes
  nothing, holds no key; fails closed with `503` when the authority is
  unreachable, which is deliberately distinct from the `401` for a rejected
  credential; no cache, so a revoked principal is refused on its next request),
  `authz.py` (a closed, deny-by-default operation inventory and a
  configuration-driven grant map — a 200 from `whoami` is authentication, never
  permission), and `routing.py` (the table of all 29 mutating routes, the
  dependency, and `validate_routing`).
- **A boot check.** `validate_routing` runs in both app factories: a mutating
  route with no routing entry, no mounted dependency, or an operation outside
  the inventory stops the process from starting, as does an unparseable grant
  file. It also refuses a zero-route inventory, because a route walker that
  inspects nothing must not report success.
- **`tests/http_auth/`** — 84 checks, including an unauthenticated sweep of all
  29 routes, and `tests/http_auth/negative_control.py`, which removes each
  control in turn on a throwaway copy of the tree and requires the suite to go
  red (15 mutants, 15 killed, 0 survived).

#### Changed
- **The mutating surface is 29 routes across two apps, not two.**
  `command_center/api/app.py` mounts 27 of them while its package docstring
  still called the application read-only; the docstrings are corrected
  (`VOYN-W0-AICC-AUTH-HTTP-01a`).
- **`actor` is gone from the dispatch write bodies.** Not validated — made
  impossible, following `queue_claim()`: the field is deleted, the request
  models set `extra="forbid"` so a forged actor is a `422` rather than a silent
  ignore, and `dispatch.service.assign` / `dispatch.policy_config.update_policy`
  take a `Principal` and have no `actor` parameter to pass.
  `PUT /api/v1/dispatch/policy` no longer accepts an unwrapped body as
  `changes`: that form was indistinguishable from a body carrying an
  unexpected top-level key.

#### Known limitations
- **Read paths remain unauthenticated** — out of scope by acceptance criteria,
  not by cost (measured: 47–76 ms median per verification, a ceiling of roughly
  105–169 authentications/second). Filed as `VOYN-W0-AICC-AUTH-HTTP-02`.
- **Seven routes still accept a client-supplied identity field** (`voter_id`,
  `owner` ×2, `actor` ×4). They are authenticated and authorized like every
  other route; removing the fields needs per-endpoint product decisions, so each
  is a signed carve-out in `routing.CLIENT_IDENTITY_CARVE_OUTS` with a reason
  and a task (`VOYN-W0-AICC-AUTH-HTTP-01b`), and a test refuses any *unsigned*
  one.
- **The Streamlit console is untouched** and remains the most exposed surface
  (`VOYN-W0-AICC-STREAMLIT-EXPOSURE-01`).

### AIOS Tasks backend (Sprint 4) — 2026-08-06

Feature flag `AICC_TASKS_BACKEND=json|aios` selects the tasks persistence layer at runtime.
Default is `json` (no behaviour change). Set to `aios` and provide `AICC_AIOS_URL` + `AICC_AIOS_TOKEN`
to route all task reads/writes through the AIOS Tasks API.

#### Added
- **`command_center/application/aios_tasks.py`**: `aicc_dict_to_create_request` / `aios_task_to_aicc_dict`
  pure mapping functions; `AIOSIdMap` (local JSON file for AICC-id ↔ AIOS-uuid correlation);
  `AIOSTasksRepository` (read/create/update/upsert/upsert_all via `aios_sdk.AIOSClient`).
- **`tasks_repository.get_repository(root)`** factory: returns `JSONTasksRepository` (default) or
  `AIOSTasksRepository` based on `AICC_TASKS_BACKEND`. AIOS variant is lazily imported so
  the JSON path carries zero new overhead.
- **`scripts/migrate_tasks_to_aios.py`**: one-shot migration of `data/tasks.json` into AIOS;
  writes `data/.aios_id_map.json` for continuity; dry-run mode via `--dry-run`.
- **`JSONTasksRepository.upsert_all(tasks)`**: atomic batch write that replaces the previous
  individual-upsert loop in `app.py:upsert_tasks()`.
- **Tests**: `tests/test_aios_tasks_adapter.py`, `tests/test_aios_tasks_repository.py`,
  `tests/test_tasks_backend_routing.py` — skipped via `pytest.importorskip("aios_sdk")`
  when the local SDK path dep is unavailable (CI).

#### Known limitations (AIOS v1)
- C1: Titles longer than 512 chars are silently truncated on create.
- C2: AICC-specific fields (`duration_estimate`, `assignee`, etc.) round-trip as notes/tags only.
- C3: `AIOSIdMap` is per-process; multi-worker Streamlit deployments need a shared store.
- I3: AIOS auth token is not refreshed mid-session (assumed long-lived).
- I4: `list_tasks()` fetches only the first page (AIOS v1 has no cursor pagination).
- I5: `aios_sdk` is a local path dep; not in `requirements.txt` — CI skips AIOS tests.

### D1 final gate — cross-platform smoke pass (partial)

- **Verification record added**: `docs/desktop/D1_FINAL_GATE_SMOKE_TEST.md` records the D1 final gate
  (`docs/desktop/IMPLEMENTATION_ROADMAP.md` §"D1 final gate") smoke pass against
  `DESKTOP_INCREMENT_1.md` §2's acceptance criteria. macOS Apple Silicon (real hardware): pass —
  `pytest-qt` desktop suite green (28/28), `ruff check .` clean, real native-Qt launch with no
  `streamlit`/`app.py` import on the startup path, and window-geometry/theme persistence verified
  across a simulated restart. Windows 11 x64: not performed — no such machine was reachable from
  this session, so the gate remains open (`AICC-D1-GATE` stays **Review**, not **Done**) pending a
  Windows-hardware pass.

### Version contract

- **Canonical application version**: `command_center.__version__` now exposes the
  current `2.0.0` release line. The historical `v2.0.0-sprint1` tag remains an
  immutable prerelease milestone; the final `v2.0.0` tag is created only from a
  validated commit after this change reaches `main`.

### Integrated runtime safety and architecture reconciliation

#### Added

- **CI workflow**: Python 3.14 validation for pull requests and `main` pushes, with committed-diff
  whitespace checks, Ruff, byte compilation of `command_center scripts tests app.py`, and the
  complete pytest suite. Actions are SHA-pinned, the token is read-only, and superseded runs are
  cancelled.
- **Fail-closed task workspace provisioning**: normal task-v2 launch surfaces provision or attach
  an isolated branch/worktree only after explicit confirmation, then pass a persisted
  `WorkspaceSpec` through a second Supervisor verification immediately before spawn. Source
  repository, exact launch path, branch, worktree isolation, and status are verified without a
  network fetch; low-level ad-hoc runtime calls remain a separate boundary.
- **Deterministic scheduler planner**: `runtime/scheduler.py` produces explainable
  `ASSIGN`/`DEFER`/`BLOCKED` decisions from immutable work, agent, load, dependency, capability,
  capacity, and retry inputs. It is a read-only planner: it creates no claim, lease, queue entry,
  run, poller, or automatic launch.

#### Fixed

- **Execution queue concurrency**: application-owned enqueue, dequeue, reevaluation, Portfolio
  insertion/rollback, and launch-result commits now hold a bounded same-host OS advisory lock
  across the complete load-transform-save cycle. Atomic replacement remains the write primitive;
  process launch is never performed while the queue lock is held, and raw load/save helpers remain
  intentionally uncoordinated primitives.
- **Launch and scheduler races**: workspace verification is registered before lifecycle evidence
  can trigger reconciliation; confirmation precedes worktree mutation; remote-tracking branches,
  persisted provisioning outcomes, active task IDs, deterministic agent tie-breaking, and corrupt
  retry state now fail safely.
- **Autonomy authority (schema 11)**: migration 7 adds canonical immutable
  `proposal.parameters_json`; malformed policies close completely; proposal authority and evidence
  freeze at assessment; CAS is checked before lifecycle guards; plans carry an exact action digest;
  dispatch rechecks policy, kind, payload, evidence digest, blockers, and staleness; confirmations
  must bind to a matching persisted result. Only TASK_CREATION and TASK_EXECUTION are currently
  dispatchable; priority, dependency, and merge plans remain advisory.
- **Runtime documentation**: README, current-state, architecture, changelog, and ADR 0005 are
  reconciled with schema 11, queue locking, workspace provisioning, scheduler and autonomy
  boundaries, and CI.
- **Per-warning launch confirmation** (Founder audit MAJOR-4): the launch confirmation dialog no
  longer clears a dirty working tree and a branch mismatch with one shared "подтверждаю несмотря на
  предупреждения" checkbox. Each warning now renders its own acknowledgement, keyed by its stable
  issue code, and the launch stays blocked — button `disabled=` plus the server-side re-check —
  until every one is ticked. Acknowledgements are also cleared each time the dialog is opened, so a
  previous launch's confirmations are never inherited.

### Portfolio Execution and Intelligence

#### Added
- **Portfolio Execution**: parses ready task cards from a separate Portfolio checkout, validates
  dependencies/conflicts and repository mappings, previews a launch plan, creates or attaches an
  isolated branch/worktree after explicit confirmation, and launches through the asynchronous
  Execution Center. A persisted launch registry and lock files prevent duplicate claims; bounded
  rollback removes only resources created by a failed launch attempt.
- **Portfolio Overview**: read-only project health, dependency waves, cycles, critical path,
  capacity, readiness and deterministic recommendations from Portfolio task cards and current
  launch state.
- **Portfolio batch launch**: an explicit, concurrency-capped orchestration flow with collision
  preflight. It does not introduce autonomous scheduling.

### Autonomy Proposal Foundation (AICC-AUTONOMY-002)

The first safe, explainable autonomy foundation: a persisted, evidence-backed proposal lifecycle
that makes the boundary between **recommendation, approval, and execution** explicit. The
autonomy layer governs decisions but **never executes anything** — `dispatch` records the
boundary crossing and returns a dry-run plan the caller must run explicitly via `start_run`. See
`docs/adr/0005-autonomy-proposal-foundation.md`.

#### Added
- **`command_center/runtime/autonomy.py`**: the pure domain core — proposal state machine
  (`DRAFT → PROPOSED → ELIGIBLE/BLOCKED → AWAITING_APPROVAL → APPROVED → DISPATCHED → EXECUTED`,
  plus `REJECTED`/`WITHDRAWN`) with an explicit transition guard; deterministic `classify_risk`
  and `evaluate_eligibility` (pure, reproducible, hardest-block-first); an attributable,
  immutable `Evidence` model with an order-independent digest; a conservative-by-default
  `AutonomyPolicy` (closed on construction; CRITICAL risk never auto-approved); and side-effect-
  free dry-run `ExecutionPlan`.
- **`command_center/runtime/autonomy_service.py`**: the orchestration engine
  (`create_proposal`, `assess`, `plan`, `approve`, `reject`, `withdraw`, `dispatch`,
  `confirm_execution`, `fail_dispatch`) writing an append-only audit event per move. Dispatch is
  refused unless the proposal is `APPROVED` **and** the policy explicitly enables execution
  dispatch; a refusal is itself audited and leaves the proposal untouched.
- **`runtime.db` migration 6**: `proposal`, `proposal_evidence` (append-only and frozen after
  assessment), `proposal_event` (append-only) tables with CAS-guarded, transition-guarded updates;
  CRUD in `db.py`; reads and
  gates exposed via `ExecutionCenterAPI` (`create_proposal`/`assess_proposal`/`plan_proposal`/
  `approve_proposal`/`reject_proposal`/`withdraw_proposal`/`dispatch_proposal`/
  `confirm_proposal_execution` + projections).
- **Tests**: `tests/test_autonomy_domain.py`, `tests/test_autonomy_db.py`,
  `tests/test_autonomy_service.py`, `tests/test_autonomy_api.py` — policy, risk, state
  transitions, denials, malformed input, the full lifecycle, and reproducibility.
- **`scripts/demo_autonomy_proposals.py`**: four end-to-end scenarios (disabled/blocked,
  human-gate, full dispatch, critical merge) against a throwaway store; launches nothing.

#### Safety
- No silent execution, no automatic merge, no hidden repository modifications, no fabricated
  evidence, no execution without an explicit policy and approval state. Hard denials block;
  eligible actions outside the auto-approval ceiling require a human. Runtime code depends on no
  UI framework.

### Autonomous Task Completion Pipeline (AICC-AUTONOMY-001)

Closes the gap between "Claude process finished" and "the engineering task is completed and
merged into the target branch". See `docs/adr/0004-autonomous-task-completion-pipeline.md` and
`docs/completion-pipeline.md`.

#### Added
- **`command_center/runtime/completion.py`**: the pure domain core — completion state machine
  (`EXECUTION_FINISHED → … → COMPLETED`, plus `VALIDATION_FAILED`/`PR_CLOSED_UNMERGED`/
  `MERGE_BLOCKED`/`REQUIRES_ATTENTION`/`RECOVERY_PENDING`/`RECOVERY_FAILED`), reason codes,
  `CompletionPolicy`, and `CompletionEvaluator` returning a structured `CompletionAssessment`
  (never a bare boolean). Completion is evidence-based: a task is `COMPLETED` only when its
  change is reachable from the target branch — exit code 0 is never sufficient.
- **`command_center/runtime/completion_service.py`**: the restart-safe, idempotent orchestrator
  (`begin_completion`, `advance`, `advance_pending`) that turns evaluator verdicts into real
  side effects (validation, push, PR open/merge, target verification, closed-unmerged recovery)
  with exponential backoff and a full audit trail.
- **`command_center/runtime/repo_state.py`** (read-only git inspection), **`git_ops.py`** (git
  write adapter — never force-pushes), **`github.py`** (first `gh` CLI integration; a closed PR
  is never treated as merged; includes an in-memory `FakeGitHubClient`), **`validation.py`**
  (configurable, allowlisted, bounded validation-plan execution).
- **`runtime.db` migration 5**: `completion`, `completion_validation`, `completion_event` tables
  with CAS-guarded updates; CRUD in `db.py`; reads exposed via `ExecutionCenterAPI`.
- **Supervisor**: `advance_completions()` and an opt-in background autopilot
  (`AICC_COMPLETION_AUTOPILOT`) that advances due completions off the UI thread.
- **`task_sync`**: seeds completion rows for completed runs and projects completion state onto
  the Kanban task (`launch_status`, and on success stage "Merged"/progress 100 +
  `pull_request_status="merged"`).
- **Execution Center UI**: a compact completion panel distinguishing "process finished" from
  "task completed and merged" (state, validation, branch/commit, PR number+state, merge status,
  last-checked, recommended action).
- **`command_center/project_config.py`**: per-project completion policy defaults (merge mode/
  method, PR recovery, validation plan) — conservative by default.
- **`scripts/demo_completion_pipeline.py`**: deterministic Scenario A/B/C demonstration against
  real git + a fake GitHub client.

### Desktop Architecture D0

#### Added
- **`docs/desktop/`**: canonical, implementation-ready documentation set for a native
  PySide6/Qt Widgets desktop application — product vision, target architecture, information
  architecture, design directions (Professional Control Plane approved), design system, a
  Workspace Home native-page spec built on the existing `build_workspace_home_snapshot` read
  model, macOS/Windows platform behavior, frozen Desktop Increment 1 (D1–D4) scope, and a
  commit-sized implementation roadmap. Documentation only — no desktop code, dependencies, or
  packaging exist yet. Next implementation stage: D1A.

### Sprint 3 Increment 1: Workspace Home

Implements `WORKSPACE_HOME_ARCHITECTURE.md` in full (all 10 steps of §17's implementation
plan). That document's own status header ("architecture only, no code changed") is now stale —
the design is implemented, not just approved.

#### Added
- **`command_center/git_info.py`**: per-project git/worktree discovery (`get_status`,
  `get_worktrees`, `get_log`, `get_diff_stat`, `get_branches`, `get_remotes`), extracted from
  `app.py`'s original ROOT-only helpers and parameterized by `cwd: Path`. `app.py`'s Git Center
  and Workspace Launcher pages are now thin wrappers over it (zero behavior change).
- **`command_center/artifacts.py`**: `list_markdown_files`, `project_from_path`,
  `infer_task_type_from_filename`, `read_text` — extracted verbatim from `app.py`, Streamlit-free,
  a leaf module. Every existing `app.py` call site repointed at it.
- **`db.list_runs`/`ExecutionCenterAPI.list_runs`** gained `states` (plural, `IN (...)`) and
  `limit` (SQL `LIMIT`) parameters, additive and backward compatible; `state`+`states` together
  raise `ValueError`. `EXECUTION_CENTER_ACTIVE_STATES` moved to `runtime/db.py` beside
  `TERMINAL_STATES`.
- **`command_center/workspace_home.py`**: the Workspace Home read model
  (`build_workspace_home_snapshot`) and its sensitivity redaction stage
  (`sanitize_workspace_project_entry`) — cross-project rollup of projects, git worktrees, active/
  recent runs (v1.2 + v2, merged and source-tagged), reports, artifacts, and activity, with every
  BANK/LEGAL entry passed through a field allowlist *before* it reaches the renderer.
- **Workspace Home page** (`workspace_home` nav entry): a new, additional page — Dashboard and
  Workspace Launcher are unchanged. Read-only; every Quick Action (Open Project, New Task, Launch
  Run, view Run/Report/Artifact) delegates to the existing gated forms, never mutates directly.
- Tests: `test_git_info.py`, `test_artifacts.py`, `test_workspace_home.py`,
  `test_workspace_home_ui.py`, plus extensions to `test_runtime_db.py`/`test_runtime_api.py` —
  389 tests total (up from 333), including a dual-layer (snapshot + rendered-page) regression
  test that no BANK/LEGAL prompt/log/report-body/raw-path content ever reaches the page.

#### Deviation from the architecture document
- §4's data-source map lists `load_tasks()` (the v1.2 Kanban store, which lives only in `app.py`)
  as a Projects-section input. `workspace_home.py` cannot import `app.py` under any circumstance
  (§6/§9.2, a hard constraint stated three times in the document) and `load_tasks` was not in
  Condition 4's extraction scope, so the per-project task count instead uses
  `ExecutionCenterAPI.list_tasks(project=...)` (v2 SQLite tasks, an explicitly allowed read
  method). This counts v2 orchestration tasks, not v1.2 Kanban cards — recorded in
  `workspace_home.py`'s module docstring.

## [1.2.0] - 2026-07-16

### Added
- **`command_center/` package**: `models`, `storage`, `project_config`, `agent_runner`,
  `report_parser`, `chat_service`, `workflow`, `activity_log` — see
  [Application and domain services](ARCHITECTURE.md#22-application-and-domain-services).
- **Project Chat** (`chat` page): per-project conversations with a provider abstraction (local
  manual mode, Claude Code CLI, optional OpenAI Responses API gated on `OPENAI_API_KEY` +
  `OPENAI_MODEL`); save any message into `reports/`, or convert it into a task.
- **Claude Code runner**: launch Claude Code from a Kanban task, the Agents page, Project Chat, or a
  generated-task preview, with an explicit repository/branch/agent/prompt confirmation step, a
  synchronous timeout-bounded execution, and full stdout/stderr capture.
- **Full report storage**: every completed run's untruncated report is saved under
  `reports/<PROJECT>/`.
- **Structured result extraction** (`report_parser.py`): deterministic verdict/findings/files/
  commit/branch/PR/validation/git-status/next-action parsing with evidence, a confidence level, and
  a manual-correction UI that never discards the original extraction.
- **Create Next Task**: verdict-driven task-type/workflow-stage/objective suggestion on a completed
  run, always requiring review before creating anything and never auto-executing.
- **Run journal** (`runs` page): filterable list of every run plus a full detail view; Executive
  Dashboard gained run metrics (today's runs, success/failure, awaiting remediation/final review,
  approved-for-commit, average duration by agent, open Blocker/High findings).
- **Task workflow fields**: `parent_task_id`, `prior_run_id`, `current_run_id`, `workflow_stage`,
  `latest_verdict`, `report_path`, `repository_path`, `branch`, `agent`, `last_run_at` — additive,
  backfilled on load, parallel to (not a replacement for) the existing Kanban `status`.
- **Project repository configuration**: Projects → "Настройки репозитория" tab; local overrides in
  gitignored `data/project_config.json`; no path ever guessed (only ever a verified-existing git
  repo, shown as a suggestion the user must save).
- **Sensitive-project handling**: BANK/LEGAL show an explicit warning before any agent launch or
  chat call and never auto-attach context files.
- `AICOS` added to the project registry (repository path unconfigured — no known local path).
- `requirements-dev.txt` (adds `pytest`), `.env.example`, and a `tests/` suite (pytest +
  Streamlit `AppTest`) covering storage, migration, path validation, the report parser, next-task
  mapping, report persistence, run filtering, sensitive-project warnings, and refusal to run
  against unconfigured paths or via a shell.

### Changed
- `data/runs.jsonl` and `data/activity.jsonl` use JSON Lines instead of a single JSON array — see
  [Persistence architecture](ARCHITECTURE.md#3-persistence-architecture) for why. `reports/` is now
  gitignored (may contain BANK/LEGAL content).

### Security
- The Claude Code runner never calls git-write subcommands itself, and refuses to run against any
  repository path not present in project configuration.
- Read-only task types (`review`/`final_gate`/`architecture_review`) run with the model's tool set
  restricted to `Read,Grep,Glob` via `--tools` — `Bash` and every file-edit tool are entirely absent
  from that run, not merely pattern-denied. Implementation/remediation task types keep `Bash` but
  have the specific git-write subcommands denied via `--disallowedTools` — see
  [Git and GitHub privileged boundaries](ARCHITECTURE.md#9-git-and-github-privileged-boundaries)
  for what each task-type class enforces.
- Fixed during independent review (F-01/F-02): an earlier version of this control denied specific
  `Bash(git ...)` patterns for read-only task types while leaving the general-purpose `Bash` tool
  available, which left `git apply`/`checkout`/`stash` and plain shell writes unrestricted for task
  types documented as unable to modify any file. Replaced with the `--tools` allowlist above.

## [1.1.0] - 2026-07-15

### Added
- Executive Dashboard: cross-project rollup (totals, active/blocked/completed, workload estimate),
  per-project status parsed from `CURRENT_STATE.md`, priority breakdown chart, workload by owner.
- Command Palette (`Mod+K`): searchable dialog to jump to any page or start a task for a project.
- Focus Mode: single-task distraction-reduced view with a quick status/"mark done" control.
- Timeline: unified, day-grouped, project-filterable feed of task events and file activity.
- AI Agents page: catalog of the task types supported by `scripts/start-task.sh`, with execution
  rules, live usage stats, and a shortcut into the task creator.
- Smart Tasks: task records gained `priority`, `owner`, `estimate_hours`, and `depends_on`;
  Kanban cards show priority/owner/estimate badges and a "Заблокировано" (blocked) badge for
  tasks with unmet dependencies; Kanban gained a priority filter.
- Git Center: expanded read-only Git view with commit history, full changed-file list,
  `git diff --stat` (staged/unstaged), branches, and remotes.
- Workspace Launcher: `git worktree list` overview plus per-project quick-jump cards (in-app
  navigation and copyable file paths).

### Changed
- `data/tasks.json` records are now backfilled with default Smart Tasks fields on load, so task
  files created before this release keep working without migration.
- The former "Git и активность" page was split: Git-only content moved to the new **Git Center**
  page, and the activity log moved to the new **Timeline** page.
- `scripts/start-ui.sh` now forwards its arguments to `streamlit run` (e.g. `--server.port`)
  instead of silently dropping them.

### Fixed
- Cross-page navigation actions (command palette, AI Agents shortcuts, Workspace Launcher,
  Focus Mode exit) no longer raise `StreamlitAPIException` when triggered — navigation targets
  are now staged in `pending_*` session-state keys and applied before the sidebar navigation
  widget is instantiated on the next run, instead of writing directly to an already-instantiated
  widget's key.

## [1.0.0] - 2026-07-15

### Added
- Initial working Streamlit application (`app.py`) launched via `python -m streamlit run app.py`.
- Dashboard: project/task counts, generated/report file counts, latest activity, active tasks
  grouped by project.
- Task creator: form (project, task type, objective, Kanban status) that runs
  `scripts/start-task.sh` as a subprocess (no `shell=True`, fixed argument list, 30s timeout,
  captured stdout/stderr) and records a matching task.
- Kanban board: Backlog / Next / In Progress / Review / Done columns, project filter, status
  change via dropdown, delete, and a task-details expander. Persisted to `data/tasks.json` with
  atomic writes.
- Project browser: per-project status, generated tasks, reports, and context, each with file
  modification time.
- Generated tasks browser and Reports browser: recursive, project-filterable, newest-first,
  markdown preview.
- Global context view: `CURRENT_STATE.md`, `DECISIONS.md`, `INBOX.md`.
- Git status: read-only branch/dirty/modified/untracked/last-commit summary.
- `requirements.txt` and `scripts/start-ui.sh` for one-command startup.
