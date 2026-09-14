# AI Command Center — Master Product Roadmap

- **Status**: Canonical roadmap for horizon **H1 (committed next)**.
- **Authority**: built under `DR-ROADMAP-AUTHORITY-001`
  ([`FINAL_GOAL_AND_ROADMAP_AUTHORITY.md`](FINAL_GOAL_AND_ROADMAP_AUTHORITY.md)) and the completed
  backlog reconciliation
  ([`FOUNDER_FUNCTIONAL_AUDIT_9761459_RECONCILIATION.md`](../audits/FOUNDER_FUNCTIONAL_AUDIT_9761459_RECONCILIATION.md)).
- **Machine-readable companion**: [`MASTER_ROADMAP_TASKS.json`](MASTER_ROADMAP_TASKS.json) — 40 rows,
  every derived field computed, not hand-typed.
- **Autonomous backlog import package**:
  [`AICC_AUTONOMY_P00_BACKLOG.md`](AICC_AUTONOMY_P00_BACKLOG.md) — the executable P00/P1 layer
  to import before running the backlog unattended.
- **Reconciled against**: `main` @ `bd9f05b`, branch `docs/canonical-master-roadmap`, 2026-07-28.
- **Scope of this document**: planning only. **Nothing was imported and nothing was launched.**
  No runtime code, no `data/tasks.json`, no worktree and no branch was created. The only validation
  performed was a read-only importer dry-run with `AICC_DATA_DIR` redirected to a temporary
  directory (§9).

---

## 1. What this roadmap commits to

The committed final goal is unchanged from `DR-ROADMAP-AUTHORITY-001` §1: a **native-desktop,
local-first, single-user developer control plane** that reaches and then exceeds today's Streamlit
feature set, with **fail-closed safety on every privileged action**.

This roadmap is the executable form of that goal for **H1 only**. It contains three tracks:

| Track | Rows | What it delivers | Success measure it closes |
|---|---:|---|---|
| **Desktop Increment 1** (`desktop-increment-1`) | 15 | The D1A→D4 sequence — the approved decomposition of the program roadmap's `AICC-D1-001` epic | §2.1 Desktop parity gate |
| **Audit remediation** (`audit-remediation`) | 13 | The Still-Open rows of the Founder Functional Audit `9761459` | §2.2 Safety gate, §2.5 Audit-closure gate |
| **Governance** (`governance`) | 6 | The `§8` required follow-ups F1–F5 of the authority record | §2.3 Data-integrity gate, §2.4 Documentation-truth gate |

Everything else that exists in the repository as "a plan" is deliberately **not** here. See §7.

---

## 2. Epic decomposition — `AICC-D1-001` → D1A…D4

The program roadmap carried a single flat task, `AICC-D1-001` "Desktop shell and Workspace Home",
as one P0 epic. `DR-ROADMAP-AUTHORITY-001` §7 **A1** settled that the nine sequenced sub-increments
in `docs/desktop/IMPLEMENTATION_ROADMAP.md` **are** its definition of done, not a competing plan
(Reconciliation Conflict 5). This roadmap performs that decomposition: one epic becomes **15 rows**
— eleven implementation increments and four verification gates.

| Row | Stage | Status | Priority | Type | Hard deps |
|---|---|---|---|---|---|
| `AICC-D1A` — Dependency and package skeleton | D1 | **Done** | High | implementation | — |
| `AICC-D1B` — Main window and application lifecycle | D1 | **Done** | High | implementation | D1A |
| `AICC-D1C` — Navigation and themes | D1 | **Done** | High | implementation | D1B |
| `AICC-D1-GATE` — cross-platform acceptance pass | D1 | **Review** | High | final_gate | D1A, D1B, D1C |
| `AICC-D2A` — Application service adapter | D2 | Backlog | High | implementation | D1-GATE |
| `AICC-D2B` — Async worker framework | D2 | Backlog | High | implementation | D2A |
| `AICC-D2C` — Workspace Home layout | D2 | Backlog | High | implementation | D2A, D2B |
| `AICC-D2D` — Edge states and accessibility | D2 | Backlog | High | implementation | D2C |
| `AICC-D2-GATE` — Native Workspace Home acceptance | D2 | Backlog | High | final_gate | D2A–D2D |
| `AICC-D3A` — Projects page | D3 | Backlog | Medium | implementation | D2-GATE |
| `AICC-D3B` — Settings and platform integration | D3 | Backlog | Medium | implementation | D2-GATE |
| `AICC-D3-GATE` — Projects/Settings acceptance | D3 | Backlog | Medium | final_gate | D3A, D3B |
| `AICC-D4A` — macOS packaging | D4 | Backlog | Medium | implementation | D3-GATE |
| `AICC-D4B` — Windows packaging | D4 | Backlog | Medium | implementation | D3-GATE |
| `AICC-D4-GATE` — Desktop Increment 1 closure | D4 | Backlog | Medium | final_gate | D4A, D4B |

### 2.1 The D1 final gate is open — this is a new finding

`docs/desktop/DESKTOP_INCREMENT_1.md:5` says "D1 … has shipped", and the code confirms it: the
PySide6 dependency, the nine-section shell, theming and the `tests/desktop/` suite all exist at
`bd9f05b`. But `IMPLEMENTATION_ROADMAP.md`'s own **D1 final gate** additionally requires a
"manual smoke pass on **both platforms**, recorded". **No such record exists** — a search of
`docs/`, `reports/` and `CHANGELOG.md` returns nothing for a macOS or Windows D1 smoke pass.

D1A–D1C are therefore recorded **Done** and the gate **Review**, not Done. This matters because
`AICC-D2A` depends on it: the gate, not D2A, is the true head of the desktop critical path, and it
is the cheapest row in the entire package — a verification action with no code and no file
conflicts.

### 2.2 Two dependency edges were recomputed, not copied

- **`AICC-D3B` (Settings/platform)** — `IMPLEMENTATION_ROADMAP.md` lists its dependency as "D3A",
  then immediately adds "may proceed in parallel with D3A if no shared file conflicts arise, since
  Projects and Settings touch different pages". The two file lists confirm the touchpoints are
  disjoint (`pages/projects.py` + `application/projects_adapter.py` vs `platform/` +
  `pages/settings_page.py`). The hard predecessor is therefore recomputed to **`AICC-D2-GATE`**,
  and D3A/D3B are scheduled as a real parallel pair.
- **`AICC-D4B` (Windows packaging)** — the roadmap already states D4B "may proceed in parallel with
  D4A"; both depend on `AICC-D3-GATE` and touch disjoint directories.

---

## 3. Normalization to live Command vocabularies

Every value in the package validates against runtime ground truth (authority tier 1), never against
a document.

| Field | Live vocabulary | Applied |
|---|---|---|
| `project` | `command_center/models.py` `PROJECT_IDS` (9 ids) | **Every row is `AICC`** |
| `status` | `models.KANBAN_STATUSES` | `Backlog`, `Review`, `Done` |
| `priority` | `models.TASK_PRIORITIES` | `Critical`, `High`, `Medium`, `Low` |
| `task_type` | `command_center/artifacts.py` `TASK_TYPES` | `implementation`, `remediation`, `architecture_review`, `review`, `final_gate` |

### 3.1 Project ids

`AICC` is the only project in this package, and that is a scope decision, not a simplification.
`DR-ROADMAP-AUTHORITY-001` §3 puts exactly one product **in scope to build**: AI Command Center.
`AIOS`, `AICOS` and `PRODUCT` are tracked/orchestrated products with their own roadmaps, so the
program roadmap's `AIOS-*`, `AICOS-*` and `PRODUCT-*` rows are not this repository's work and are
not committed here.

The program roadmap's non-resolving ids were disposed of rather than aliased:

| Program-roadmap id | `normalize_project_id` result | Disposition here |
|---|---|---|
| `AI_COMMAND_CENTER` | `None` — the alias table folds case and whitespace, **not** underscores | Remapped to `AICC` |
| `AIOS_PRODUCT` | `None` | Out of scope (§3 — `PRODUCT` is tracked, not built here) |
| `PORTFOLIO`, `PLATFORM` | `None` | **Rejected** — no canonical project home (§7 R3) |

No alias or registry entry was added to make anything import. That is explicitly reserved for an
ADR (§7 R4), and writing that ADR is row `AICC-GOV-F3`.

### 3.2 Priorities

The program roadmap's `P0/P1/P2` scale is not a live vocabulary and was **not** mechanically mapped.
Priorities were assigned from evidence:

- **Critical** — only the five P00 hands-off autonomy hardening rows:
  `AICC-AUTO-P00-SUPERVISOR`, `AICC-AUTO-P00-FAILURE-CLASSIFIER`,
  `AICC-AUTO-P00-SMOKE-ROLLBACK`, `AICC-AUTO-P00-AUTO-SPLIT`, and
  `AICC-AUTO-P00-BUDGETS`. These are the minimum guardrails before launching the backlog
  unattended.
- **High** — the AICC desktop/mobile command-surface epic, the H1 desktop track through
  `AICC-D2-GATE`, the registry/import-integrity task `AICC-GOV-F2`, the registry ADR, and the two
  audit rows carrying safety/containment findings (`W0-006`, `W1-002`) plus the schema row
  `W2-001`.
- **Medium / Low** — carried verbatim from the audit reconciliation for the 13 audit rows, and
  assigned by horizon distance for D3/D4 and the documentation follow-ups.

---

## 4. Dependencies, blocks and the critical path

All of the following are **computed** from `depends_on` plus evidence — reverse edges, readiness,
chain lengths, the critical path and the wave schedule. None is hand-maintained.

### 4.1 Critical path — 10 open rows

```
AICC-D1-GATE → AICC-D2A → AICC-D2B → AICC-D2C → AICC-D2D
             → AICC-D2-GATE → AICC-D3A → AICC-D3-GATE → AICC-D4A → AICC-D4-GATE
```

`AICC-AUDIT-W2-004` is a **co-equal entry** (chain length 10) via a soft edge into `AICC-D2A`, and
`AICC-GOV-F3 → AICC-GOV-F5` (chain 8 and 7) feeds `AICC-D2D`. Scheduling that ignores those two
soft chains delays the desktop track by several waves even though the hard graph looks clear.

### 4.2 Soft precedence — two edges that are not hard dependencies but do gate

A **soft** edge means: starting out of order is legal, but the successor's acceptance criterion
would be validated against a state that is about to change.

| Predecessor | Successor | Why |
|---|---|---|
| `AICC-AUDIT-W2-004` (Project Intelligence on Workspace Home) | `AICC-D2A` | D2A's acceptance criterion is that the adapter returns "the same snapshot shape `build_workspace_home_snapshot` already returns". W2-004 changes that surface. Building the adapter first means rework. |
| `AICC-GOV-F5` (correct the "six projects" language) | `AICC-D2D` | D2D implements `WORKSPACE_HOME_SPEC.md` §15's edge-state table, which still says "all-six-unconfigured". Building it before F5 encodes a six-project assertion against a nine-id registry. |

### 4.3 Dropped and rewritten edges

The audit reconciliation dropped five dependency edges whose targets are Done or Superseded
(`W1-007→W0-004`, `W2-001→W0-005`, `W3-002→W3-001`, `W4-003→W1-008`, `W4-004→W4-002`); those
remain dropped and are preserved per-row in `dropped_dependencies`. The single surviving audit edge
is `AICC-AUDIT-W2-002 → AICC-AUDIT-W2-001`.

One dependency list is **computed**: `AICC-GOV-F4B` (the refreshed Founder Audit) depends on all 13
audit remediation rows, because the audit-closure gate cannot be honestly re-run before its own
findings are addressed.

### 4.4 Ready to start now — 16 rows

`ready_to_start` is true when every hard dependency is `Done`, no soft precedence is unmet, and the
finding was re-verified as still open against `bd9f05b`.

`AICC-GOV-F2`, `AICC-GOV-F3`, `AICC-GOV-F4A`, `AICC-D1-GATE`, and the audit rows `W0-006`, `W1-002`,
`W1-004`, `W1-005`, `W1-006`, `W1-007`, `W1-009`, `W2-001`, `W2-004`, `W2-006`, `W4-003`, `W4-004`.

Sixteen rows are ready and the live capacity cap is **two**. That gap is what §6 exists to resolve —
readiness is not a schedule.

---

## 5. Conflicts

### 5.1 Structural conflict — one duplicated epic, folded

`AICC-AUDIT-W3-002` ("Портировать Workspace Home на desktop") was classified **Still Open** by the
reconciliation with the note "This is stage **D2** of the frozen scope". Decomposing `AICC-D1-001`
into D1A→D4 makes the same work appear twice. It is therefore **folded into `AICC-D2A`/`D2B`/`D2C`/
`D2D`**, which carry it at finer granularity with explicit acceptance criteria, and it is **not** a
separate row. The fold is recorded in the package's `folded` block and in each D2 row's `folds_in`,
so traceability back to the audit id survives. This is why the audit contributes **13** rows here
and not the 14 it converted.

### 5.2 File-level conflicts — the real constraint on parallelism

Global Operating Rule 2 is *one agent = one task = one branch = one worktree*. Two rows that edit
the same file therefore run on separate branches in separate worktrees and collide at merge, no
matter how much concurrency is configured. Computed from each row's declared components:

| File | Contending rows |
|---|---|
| `app.py` | `W1-004`, `W1-007`, `W1-009`, `W2-002`, `W4-004` |
| `command_center/portfolio_launch.py` | `W1-005`, `W1-006` |
| `command_center/models.py` | `W2-001`, `W2-002` |
| `command_center/runtime/reports.py` | `W0-006`, `W4-003` |
| `command_center/agent_runner.py` | `W0-006`, `W1-009` |
| `command_center/desktop/pages/home.py` | `D2C`, `D2D` |

`app.py` is the dominant constraint: five independent rows want to edit one ~285 KB file. The
schedule in §6 never places two of them in the same wave. Note this is a **scheduling** constraint,
not a dependency — any two of those rows may be done in either order, just not at the same time.

### 5.3 Conflicts that were resolved rather than scheduled around

- `AICC-AUDIT-W2-006` (git ahead/behind) overlaps the program roadmap's H2 `AICC-GIT-001` (Git
  Center) and the audit's own MINOR-4/NIT-1. Only the audit row is committed; the H2 concept stays
  a triage input for `AICC-GOV-F4A`.
- `AICC-AUDIT-W4-004` (founder approval surface) originally depended on `W4-002`, which the
  reconciliation classified **Superseded** by the shipped `runtime/scheduler.py` +
  `task_pipeline.tick` design. The edge is dropped and the row now stands on its own as the missing
  confirmation surface over the *shipped* autopilot.

---

## 6. Capacity and worktree constraints

### 6.1 Capacity — from live configuration

| Setting | Value | Source |
|---|---:|---|
| `max_global_concurrency` (default) | **2** | `pipeline_settings.DEFAULT_MAX_GLOBAL_CONCURRENCY` |
| `max_agent_concurrency` (default) | **2** | `pipeline_settings.DEFAULT_MAX_AGENT_CONCURRENCY` |
| Configurable bounds | `[1, 16]` | `pipeline_settings.MIN_CONCURRENCY` / `MAX_CONCURRENCY` |

**Capacity sensitivity — raising the cap stops paying at 4.** Re-running the same scheduler at
different caps:

| `max_global_concurrency` | Waves | Conflict-bound waves |
|---:|---:|---:|
| 2 *(live default)* | 16 | 0 |
| 3 | 11 | 0 |
| 4 | 10 | 1 |
| 6 | 10 | 2 |

The floor is **10 waves — the critical-path length** (§4.1). At the live default of 2, every wave
except the last is capacity-bound; the file conflicts of §5.2 shape *which* rows pair but never
shorten the plan. From a cap of 4 upward the `app.py` group starts binding instead, and no further
concurrency helps. The practical reading: going from 2 to 3 is the single highest-value capacity
change available (16 → 11 waves); going past 4 is wasted.

### 6.2 Worktree constraints

- **One branch, one worktree.** `workspace_provisioning._conflicting_worktree` rejects a branch
  already checked out in another worktree of the same repository. Every row in the package carries
  a distinct branch; none collides with any of the 136 existing branches.
- **Main-line protection.** `workspace_provisioning.MAIN_BRANCH_NAMES` / `is_feature_task` keep
  feature and audit work out of the primary working tree. The only rows whose `branch` is `main` are
  the already-merged `Done` rows (D1A–D1C), which need no worktree.
- **Offline provisioning.** `git worktree add` runs without an implicit network fetch, and an
  ambiguous remote-tracking match fails closed.
- **Naming.** `repository_path` = `/Users/dmitrijcernikov/Projects/ai-command-center`;
  `workspace_path` = `<repo>-worktrees/<branch with "/" replaced by "-">`, matching the convention
  already in `git worktree list`.
- **Hygiene, and it is not cosmetic.** **77 worktrees are already registered** for this repository
  (25 under the `-worktrees/` root, 7 daily-audit worktrees, 1 prunable). Run `git worktree prune`
  and reclaim finished worktrees **before** scheduling a wave, or provisioning competes for disk
  with abandoned trees.

### 6.3 Parallel schedule — 19 waves at the live cap of 2

Waves are a greedy topological schedule honouring, in order: the P00 autonomy cleanup founder
override, longest remaining chain, priority, then id for determinism — subject to hard deps, soft
precedence, file conflicts and the concurrency cap of 2.

| Wave | Slot 1 | Slot 2 | Bound by |
|---|---|---|---|
| P00A | `AICC-AUTO-P00-SUPERVISOR` *(Critical)* | `AICC-AUTO-P00-FAILURE-CLASSIFIER` *(Critical)* | capacity |
| P00B | `AICC-AUTO-P00-SMOKE-ROLLBACK` *(Critical)* | `AICC-AUTO-P00-AUTO-SPLIT` *(Critical)* | dependencies and capacity |
| P00C | `AICC-AUTO-P00-BUDGETS` *(Critical)* | `AICC-APP-H1-COMMAND-SURFACE` *(High)* | autonomy cleanup dependency |
| P01 | `AICC-GOV-F2` | `AICC-D1-GATE` *(CP head)* | capacity |
| P02 | `AICC-AUDIT-W2-004` | `AICC-GOV-F3` | capacity |
| P03 | `AICC-D2A` *(CP)* | `AICC-GOV-F5` | capacity |
| P04 | `AICC-D2B` *(CP)* | `AICC-AUDIT-W2-001` | capacity |
| P05 | `AICC-D2C` *(CP)* | `AICC-AUDIT-W0-006` | capacity |
| P06 | `AICC-D2D` *(CP)* | `AICC-AUDIT-W1-002` | capacity |
| P07 | `AICC-D2-GATE` *(CP)* | `AICC-AUDIT-W1-004` | capacity |
| P08 | `AICC-D3A` *(CP)* | `AICC-D3B` | capacity |
| P09 | `AICC-D3-GATE` *(CP)* | `AICC-AUDIT-W1-005` | capacity |
| P10 | `AICC-AUDIT-W1-006` | `AICC-AUDIT-W1-007` | capacity |
| P11 | `AICC-AUDIT-W2-002` | `AICC-D4A` *(CP)* | capacity |
| P12 | `AICC-D4B` | `AICC-AUDIT-W1-009` | capacity |
| P13 | `AICC-AUDIT-W2-006` | `AICC-AUDIT-W4-003` | capacity |
| P14 | `AICC-AUDIT-W4-004` | `AICC-D4-GATE` *(CP closes)* | capacity |
| P15 | `AICC-GOV-F1` | `AICC-GOV-F4A` | capacity |
| P16 | `AICC-GOV-F4B` | — | dependencies — nothing else is eligible |

The P00 autonomy cleanup and the app command-surface seed close before the previous P01 wave starts.
Desktop Increment 1 still closes at **P14**; the audit-closure gate at **P16**. Each row's assignment
is also carried in the JSON as `parallel_group` / `parallel_slot`.

---

## 7. Deliberately excluded

| Excluded | Why | Where it goes |
|---|---|---|
| `AICC-D2-001..004`, `AICC-GIT-001`, `AICC-INT-001` | H2 concepts — accepted in principle, committed only per item after triage | Inputs to `AICC-GOV-F4A` |
| ADR 0001 "Tier B" list | Same — untriaged backlog | Inputs to `AICC-GOV-F4A` |
| `AIOS-DIST-*`, `AICC-DIST-*`, `FED-*`, `MARKET-*`, `*-ENT-*`, `AICC-SELF-*`, `GLOBAL-001` | H3 aspirational — direction, not commitment (§6 H3) | Not scheduled |
| `PORTFOLIO-*`, `PLATFORM`-scoped rows | No canonical project home (§7 R3) | Rejected |
| `AIOS-*`, `AICOS-*`, `PRODUCT-*` | Separate products with their own roadmaps; tracked here, not built here (§3) | Not this repository's work |
| `roadmap/program/import_program_roadmap.py`, `ready_tasks.py` | Superseded on arrival by `scripts/import_tasks.py` (§7 R2) | **Do not run** |

### 7.1 Two backlogs the authority record never dispositioned — flagged, not adopted

`DR-ROADMAP-AUTHORITY-001` §7 dispositions three backlogs. Building this roadmap surfaced two more
in the repository that it does not mention, so neither is committed here and both need a founder
decision:

1. **`docs/ux/IMPLEMENTATION_ROADMAP.md`** — a seven-increment Streamlit UX track (UX-1…UX-7),
   partially shipped (`command_center/ui/theme.py` exists; `ui/app_shell.py` does not). UX-7
   ("Native Desktop Migration") overlaps the desktop track in this roadmap.
2. **`docs/roadmap/AICC_DESKTOP_AUTOPILOT_TASKS.json`** — a 22-row package of `AICC-DESKTOP-00x`
   tasks, every one already at status `Review` and pointing at the
   `codex/pr34-lifecycle-remediation` branch. Its ids do not collide with this package, but its
   subject matter (autopilot, scheduler, queue) overlaps the H2 concepts.

Recommendation: fold both into `AICC-GOV-F4A`'s triage rather than leaving a fourth and fifth
untracked "what's next" list — that is exactly the condition Reconciliation Conflict 6 named.

---

## 8. Row index

Full field detail — prompts, definitions of done, forbidden scope, per-row evidence and every
computed field — is in [`MASTER_ROADMAP_TASKS.json`](MASTER_ROADMAP_TASKS.json).

### Autonomy P00 (5)

| Id | Title | Status | Priority | Ready |
|---|---|---|---|---|
| `AICC-AUTO-P00-SUPERVISOR` | Backlog supervisor selects and advances the next safest work | Backlog | **Critical** | yes |
| `AICC-AUTO-P00-FAILURE-CLASSIFIER` | Failure classifier separates flaky infra from real code defects | Backlog | **Critical** | yes |
| `AICC-AUTO-P00-SMOKE-ROLLBACK` | Post-merge smoke with automatic stop or rollback path | Backlog | **Critical** | no (P00 supervisor/classifier) |
| `AICC-AUTO-P00-AUTO-SPLIT` | Auto-split oversized backlog items into safe slices | Backlog | **Critical** | no (P00 supervisor) |
| `AICC-AUTO-P00-BUDGETS` | Autonomy budgets, rate limits, and kill switches | Backlog | **Critical** | no (P00 supervisor/classifier) |

### AICC App Command Surface (1)

| Id | Title | Status | Priority | Ready |
|---|---|---|---|---|
| `AICC-APP-H1-COMMAND-SURFACE` | AICC desktop and mobile command surface for all operational data | Backlog | High | no (P00 autonomy controls) |

### Governance (6)

| Id | Title | Status | Priority | Ready |
|---|---|---|---|---|
| `AICC-GOV-F2` | Restore `data/tasks.json` project-id integrity | Backlog | High | yes |
| `AICC-GOV-F3` | Write the missing project-registry ADR | Backlog | High | yes |
| `AICC-GOV-F1` | Refresh `README.md` / `CHANGELOG.md` against `main` | Backlog | Medium | no (F3) |
| `AICC-GOV-F4A` | Triage ADR 0001 Tier B + program-roadmap H2 concepts | Backlog | Medium | yes |
| `AICC-GOV-F4B` | Audit-closure gate: refreshed Founder Audit | Backlog | Medium | no (13 rows) |
| `AICC-GOV-F5` | Correct the "six projects" language in `docs/desktop/` | Backlog | Low | no (F3) |

> **`AICC-GOV-F2` carries a measurement caveat.** Reconciliation Conflict 2 measured 26 of 56 live
> rows with non-resolving `project` values. This checkout holds only `data/*.example.*` files, so
> that figure is the last recorded measurement, **not** a current one — the row's first action is to
> re-measure the live store. The per-row remapping table needs founder approval before it is applied,
> and it must go through the validating import pipeline, never a direct JSON write.

### Audit remediation (13)

| Id | Title | Status | Priority | Ready |
|---|---|---|---|---|
| `AICC-AUDIT-W0-006` | Containment check for `project` in report paths | Backlog | High | yes |
| `AICC-AUDIT-W1-002` | Align `scripts/start-task.sh` with the full `PROJECT_IDS` registry | Backlog | High | yes |
| `AICC-AUDIT-W2-001` | Single canonical task schema | Backlog | High | yes |
| `AICC-AUDIT-W1-004` | Separate branch-mismatch and dirty-tree acknowledgements | Backlog | Medium | yes |
| `AICC-AUDIT-W1-005` | Guard git worktree/branch operations against `main` | Backlog | Medium | yes |
| `AICC-AUDIT-W1-006` | Recovery from a stuck Portfolio claim-lock | Backlog | Medium | yes |
| `AICC-AUDIT-W1-007` | Confirmation before task deletion | Backlog | Medium | yes |
| `AICC-AUDIT-W2-002` | Dependency-cycle detection | Backlog | Medium | no (`W2-001`) |
| `AICC-AUDIT-W1-009` | Pre-flight check for the `claude` binary | Backlog | Low | yes |
| `AICC-AUDIT-W2-004` | Project Intelligence / Recommendations on Workspace Home | Backlog | Low | yes |
| `AICC-AUDIT-W2-006` | ahead/behind and `git fetch` in `git_info.py` | Backlog | Low | yes |
| `AICC-AUDIT-W4-003` | Automatic run-result collector into Reports/Timeline | Backlog | Low | yes |
| `AICC-AUDIT-W4-004` | Founder confirmation UI for orchestrator-initiated launches | Backlog | Low | yes |

All 13 findings were **re-verified as still open against `bd9f05b`** while building this roadmap,
not carried on trust from the reconciliation's `5eed19c` baseline.

### Desktop Increment 1 (15)

See the table in §2.

---

## 9. Verification performed

- **Every still-open audit finding re-checked** against `bd9f05b` — e.g. `runtime/reports.py:49`
  still interpolates `project` into `REPORTS_ROOT` with no allowlist; `scripts/start-task.sh` still
  accepts only `AIOS|BANK|LEGAL`; `app.py:1269` still deletes on first click;
  `agent_runner.claude_cli_available` still has exactly one caller (`chat_service.py:109`);
  `git_info.py` still exposes no ahead/behind or fetch; `desktop/pages/home.py` is still an
  `EmptyState`.
- **Desktop D1A–D1C confirmed Done** — `requirements-desktop.txt` (PySide6), `requirements-dev.txt:17`
  (pytest-qt), nine sections in `desktop/sections.py`, five `tests/desktop/` suites.
- **D2–D4 confirmed not started** — `command_center/application/`, `command_center/platform/`,
  `desktop/workers.py` and `packaging/` do not exist.
- **Vocabulary check** — every `project`/`status`/`priority`/`task_type` value read from live code.
- **Branch-collision check** — the package's 31 non-`main` branches are distinct from each other,
  map to 31 distinct worktree paths, and collide with none of the 137 existing refs.
- **Importer dry-run**, read-only, with `AICC_DATA_DIR` redirected to a temp directory:

```
AICC_DATA_DIR=$(mktemp -d) python3 scripts/import_tasks.py \
  docs/roadmap/MASTER_ROADMAP_TASKS.json --dry-run
→ Total tasks: 34 | New: 34 | Duplicates: 0 | Errors: 0 | Warnings: 0 | exit 0
```

This proves schema validity against an empty store. It does **not** prove id-uniqueness against the
live `data/tasks.json`; re-validate there before any apply.

## 10. If and when this is imported

**It has not been, and this document does not authorize it.** When a founder chooses to:

1. Resolve `AICC-GOV-F2` first — importing into a store that already fails the integrity gate
   compounds the problem the gate exists to catch.
2. Import **this** package or the older
   `FOUNDER_AUDIT_9761459_STILL_OPEN_IMPORT_PACKAGE.json`, **never both** — 13 ids are shared by
   design for dedup stability.
3. Decide whether to filter `status == "Done"` rows; they are present for traceability, not because
   the Kanban needs them.
4. Use `scripts/import_tasks.py … --apply` or the in-app uploader. Never write `tasks.json` directly.
