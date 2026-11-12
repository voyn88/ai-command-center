# AI Command Center — Desktop Workspace Home Spec

Status: **Target spec for the native Home page (D2), built entirely on existing read models.**
The shipped D1 shell renders only a placeholder Home page; the data wiring this spec describes
lands in D2, once the `command_center.application` adapter exists.
The native Home page is a `command_center.desktop` renderer over
`command_center.workspace_home.build_workspace_home_snapshot`, called through a
`command_center.application` adapter (`ARCHITECTURE.md` §5) — the same snapshot function the
existing Streamlit Workspace Home page already renders. **No new snapshot fields are required by
this spec.** Every section below maps to a field already present in the dict returned by
`build_workspace_home_snapshot` (`command_center/workspace_home.py`), verified by reading that
module directly.

This document does not redefine Workspace Home's architecture — `WORKSPACE_HOME_ARCHITECTURE.md`
(at the repository root, alongside `ARCHITECTURE.md`, not under `docs/desktop/`) remains
authoritative for the read model itself, the redaction stage, and the service boundary. Every
other reference to `WORKSPACE_HOME_ARCHITECTURE.md` in `docs/desktop/` points to that same
repository-root file. This document defines how the native desktop page presents that same
snapshot.

## 1. Layouts

### 1.1 Wide layout (≥ 1280px content width)

Three-column grid: `ProjectCard`s in a responsive grid (up to 3 per row), Active Runs / Recent
Runs side-by-side below the project grid, Artifacts / Reports side-by-side below that, Recent
Activity as a single full-width column at the bottom. The header `MetricCard` strip spans the
full width above all sections.

### 1.2 Medium layout (768–1279px content width)

Two-column grid for `ProjectCard`s (up to 2 per row); Active Runs and Recent Runs stack
vertically instead of side-by-side; Artifacts and Reports stack vertically; Recent Activity
remains full-width at the bottom. The header `MetricCard` strip wraps to two rows if needed
rather than shrinking metric text below `type.caption` size.

### 1.3 Minimum supported layout (600px content width)

Single-column stack, in this fixed order: header `MetricCard` strip (wrapped), `ProjectCard`s
(one per row), Active Runs, Recent Runs, Artifacts, Reports, Recent Activity. Below 600px, the
window itself enforces a minimum content width (set at implementation time in
`command_center.desktop`'s main window) rather than continuing to reflow indefinitely — Desktop
Increment 1 does not target arbitrarily small window sizes.

## 2. Project cards

One `ProjectCard` per entry in `snapshot["projects"]`, in `command_center.models.PROJECT_IDS`
order (`AIOS`, `AICOS`, `BANK`, `LEGAL`, `BUSINESS`, `PERSONAL` — the existing, fixed order;
never re-sorted by state or alphabetically, matching the existing Streamlit page). Each card
shows, from the corresponding project dict (`id`, `display_name`, `sensitive`,
`repository_path`, `repository_state`, `task_count`, `active_run_count`):

- `display_name` as the card title.
- A `sensitive` `StatusBadge` variant (§7.9 of `DESIGN_SYSTEM.md`) if `sensitive` is `true`.
- Repository health via `repository_state` (§3).
- `task_count` labeled exactly **"Tasks"**, never "Kanban tasks" or "Open tasks" — see §11 for
  why this label matters.
- `active_run_count` labeled **"Active runs"**.
- A "Configure repository path" action when `repository_state == "unconfigured"`.

## 3. Repository health

`repository_state` (from `_discover_worktrees` in `command_center/workspace_home.py`) is exactly
one of four values; the native page maps each to a specific `StatusBadge` state and label — this
mapping must not drift, since it is the only signal a user has for why a project's worktree
section is empty:

| `repository_state` | `StatusBadge` semantic token | Label |
|---|---|---|
| `"unconfigured"` | `status.neutral` | "Not configured" |
| `"invalid_path"` | `status.warning` | "Path no longer valid" |
| `"not_git_repo"` | `status.warning` | "Not a git repository" |
| `"ok"` | `status.success` | "Configured" |

No fifth state exists in the snapshot today; if a future change to `_discover_worktrees`
introduces one, this table must be updated in the same change (see `ARCHITECTURE.md` §7 on
reusing existing core without silently diverging from it).

## 4. Worktree status

For a project whose `repository_state == "ok"`, `snapshot["worktrees_by_project"][project_id]["worktrees"]`
is rendered as a list of `WorktreeRow`s (path, branch, short HEAD). Branch is shown exactly as
`git_info.get_status`/`get_worktrees` returns it — including the literal string
**"(detached HEAD)"** when a worktree has no current branch (`git_info.py`'s own convention,
`branch.stdout.strip() if ... else "(detached HEAD)"`). The native page does not invent a
different detached-HEAD label — reusing the exact existing string keeps the desktop and
Streamlit pages consistent and avoids introducing a second source of truth for this state.

For any other `repository_state`, no `WorktreeRow` list is rendered; instead the project's
repository-health badge (§3) is the only signal shown, with the matching next action (e.g.
"Configure repository path" for `unconfigured`) rather than an empty list with no explanation.

## 5. Active runs

`snapshot["active_runs"]` — v2 runs only, in `{"PREPARED", "QUEUED", "RUNNING"}`
(`command_center.runtime.db.EXECUTION_CENTER_ACTIVE_STATES`), already source-tagged and already
sanitized for BANK/LEGAL (§10). Rendered as `RunSummary` rows, most recent first (the snapshot is
already sorted by `created_at` descending). A run's `StatusBadge` uses `status.active` for
`RUNNING` and `status.info` for `PREPARED`/`QUEUED` (§1.10 of `DESIGN_SYSTEM.md`).

## 6. Recent runs / activity

- **Recent Runs** (`snapshot["recent_runs"]`) — merged v1.2 + v2 terminal runs, source-tagged.
  Every row's identity is the tuple `(source, run_id)`, never bare `run_id`
  (`WORKSPACE_HOME_ARCHITECTURE.md` §8/F5) — the native page's list-model row keys must use this
  same composite key, exactly like the Streamlit page's Qt-equivalent widget keys.
- **Recent Activity** (`snapshot["recent_activity"]`) — real `activity_log` events folded with
  activity rows *derived* from the already-fetched Recent Runs list (§11). Rendered as
  `ActivityItem` rows, most recent first.

## 7. Artifact summaries

`snapshot["artifacts"]` — bounded list of generated-task-file entries (`project`, `task_type`,
`created_at`, `nav_target`, plus `path` for non-sensitive projects). Rendered as `ArtifactRow`s.
For a sensitive project's entries, `path` is absent from the dict entirely (already stripped by
`sanitize_workspace_project_entry` before the snapshot is built) — the native renderer has no
raw path to accidentally display for those rows (§10).

## 8. Report summaries

`snapshot["reports"]` — bounded list of report entries (`run_id`, `source`, `project`, `verdict`,
`severity_counts`, `created_at`, plus `report_path` for non-sensitive projects, and `run_id`/
`source` as `None` for an unmatched report file with no linked run, per
`_unmatched_report_entry`). Rendered as `ReportRow`s with a verdict `StatusBadge`; an unmatched
report (`run_id is None`) renders without a verdict badge rather than a misleading blank one.

## 9. Approved Quick Actions

Only the Quick Actions already defined in `WORKSPACE_HOME_ARCHITECTURE.md` §11 are ports to
native equivalents — this spec introduces no new mutation surface:

| Action | Native equivalent |
|---|---|
| New Task | Not available in Desktop Increment 1 — task creation is out of scope (`DESKTOP_INCREMENT_1.md`) |
| Launch Agent Run | Not available in Desktop Increment 1 — starting runs is out of scope |
| Open Project | Navigates to the Projects page, scoped to that project (`INFORMATION_ARCHITECTURE.md` §8) |
| Resume/View Session | Not available in Desktop Increment 1 — Sessions/Execution pages are placeholders only |
| View Report / Artifact | Navigates to the redacted-safe `nav_target` (project + section) — not a raw file open, matching the existing Streamlit behavior for sensitive projects |
| Configure repository path | Navigates to Projects' repository-path field for that project |

Every Quick Action available in D1 is read/navigation-only, consistent with binding decision 11
(D1 is read-only except repository-path configuration, theme/window preferences, and window
geometry).

## 10. BANK/LEGAL sensitivity handling

The native Home page inherits every guarantee `WORKSPACE_HOME_ARCHITECTURE.md` §5.1/§13 already
establishes, unchanged:

- **Redaction happens before rendering, inside `command_center.workspace_home`, never in
  `command_center.desktop`.** The native renderer, like the Streamlit renderer, receives only
  already-sanitized data for BANK/LEGAL entries — it has no code path back to raw run/report/
  artifact/activity rows, and needs no sensitivity-awareness of its own beyond showing the
  `sensitive` `StatusBadge` on `ProjectCard`.
- For BANK/LEGAL, runs/reports/artifacts/activity entries carry **only** the allowlisted fields
  defined in `command_center/workspace_home.py`'s `_RUN_ALLOWED_FIELDS`/`_REPORT_ALLOWED_FIELDS`/
  `_ARTIFACT_ALLOWED_FIELDS`/`_ACTIVITY_ALLOWED_FIELDS` — the native page must not attempt to
  render a field outside those sets for a sensitive project's entry, because that field will
  simply not be present in the dict to render.
- The native page's own test suite (`pytest-qt`-based, `ARCHITECTURE.md` §17) must include the
  same dual-layer regression the Streamlit page's `test_workspace_home_ui.py` already covers:
  one assertion on the snapshot dict itself (shared with `test_workspace_home.py`, not
  re-derived), and one assertion on the rendered widget tree/text for a BANK/LEGAL-only scenario.

## 11. Semantics that must not regress

These are existing, already-shipped behaviors the native page must preserve exactly — getting
any of these wrong would be a functional regression relative to the Streamlit page, not a neutral
redesign choice:

- **`task_count` is the v2 orchestration task count** (`ExecutionCenterAPI.list_tasks(project=...)`),
  **not** the v1.2 Kanban board's open-task count — this is a recorded, deliberate deviation from
  `WORKSPACE_HOME_ARCHITECTURE.md` §4's original data-source map (see that module's own docstring
  in `command_center/workspace_home.py`). The native page's label for this field must be
  **"Tasks"**, and must never say **"Kanban"** or **"Open tasks"** anywhere in its UI, tooltip, or
  accessible name — either of those phrasings would misrepresent what the number counts.
- **Synthetic (derived) activity rows are v2-only.** `_derive_activity_from_v2_runs` only ever
  synthesizes rows from `source == "v2"` runs; v1.2 activity is never synthesized because it is
  already written to `activity.jsonl` directly. The native page must not add a second derivation
  path that also synthesizes v1.2 activity rows — doing so would double-count v1.2 events already
  present in the real `activity_log` feed.
- **Workspace Home must not reintroduce v1.2 duplicate activity.** Before this design's Sprint 3
  Increment, an earlier version of the product's activity handling had a known duplicate-activity
  issue; the current fold (real `activity_log` events + v2-derived rows only, sorted once by
  `ts`) is the fix. The native page must reuse the already-merged `snapshot["recent_activity"]`
  list as-is, not re-merge or re-derive activity itself from raw runs.
- **Redaction happens before rendering** — restated from §10 because it is the single
  most safety-critical invariant in this document: the native renderer must never be handed, and
  must never attempt to fetch, raw pre-redaction data for a sensitive project.
- **`failure_reason` is only the safe, constrained field already approved** — per
  `command_center/workspace_home.py`'s own comment on `_RUN_ALLOWED_FIELDS`, this field is
  allowed in the sensitive-project allowlist *only* because the Supervisor constrains it, at its
  one call site, to exactly `None` or the literal string `"timeout"` — never raw exception text.
  The native page must render this field as a plain status label (e.g. "Timed out") and must
  never treat it as free text safe to display verbatim without that constraint being true; if a
  future change to the Supervisor ever allows other values in this field, this document and
  `WORKSPACE_HOME_ARCHITECTURE.md` §5.1 must be re-reviewed together before the native page
  renders it unchanged.

## 12. Manual refresh

A single "Refresh" action re-invokes `build_workspace_home_snapshot` through the
`command_center.application` Workspace Home adapter, on a `QThreadPool` worker
(`ARCHITECTURE.md` §10) — never on the GUI thread. There is no automatic polling or
auto-refresh in Desktop Increment 1, matching `INFORMATION_ARCHITECTURE.md` §6 and
`WORKSPACE_HOME_ARCHITECTURE.md` §19's existing "no auto-refresh on Home" non-goal.

## 13. Loading state

On first render and on every manual refresh, the page shows `LoadingSkeleton` (§7.17 of
`DESIGN_SYSTEM.md`) in place of whichever sections are still awaiting the in-flight snapshot
fetch, sized to match the density-appropriate row heights of the content they stand in for, and
removed entirely once the snapshot (or an error) arrives — never left visible alongside partial
real content.

## 14. Stale-data indication

Because refresh is manual only (§12), the page shows a small "Last updated" timestamp (derived
from the moment the last successful snapshot fetch completed, held in
`command_center.application`'s adapter state — not a new snapshot field) next to the Refresh
action, so a user can judge staleness without the page claiming to be live. This is a
presentation-layer timestamp only; it introduces no new data source and no polling.

## 15. Edge-state handling

Every edge state below is an existing, already-handled case in
`command_center/workspace_home.py`/`command_center/project_config.py`/`command_center/git_info.py`
— the native page's job is to render the existing signal correctly, not to detect a new
condition:

| Edge case | Existing signal | Native rendering |
|---|---|---|
| Malformed `data/project_config.json` | `project_config.load_project_configs()`'s existing parse handling (falls back to defaults per project on a bad entry) | `ProjectCard` renders using whatever `load_project_configs` returns — no separate "malformed config" UI state is introduced; this is handled entirely inside the existing module |
| Unconfigured repository | `repository_state == "unconfigured"` | §3 — "Not configured" badge + "Configure repository path" action |
| Invalid path | `repository_state == "invalid_path"` | §3 — "Path no longer valid" badge |
| Non-git repository | `repository_state == "not_git_repo"` | §3 — "Not a git repository" badge |
| Detached HEAD | `git_info`'s `"(detached HEAD)"` branch string | §4 — rendered verbatim in the `WorktreeRow`'s branch field |
| Empty install (every project in the canonical registry unconfigured; [ADR 0009](../adr/0009-canonical-project-registry-and-validating-task-import.md)) | the default fresh-checkout state (`WORKSPACE_HOME_ARCHITECTURE.md` §7.1) | Every `ProjectCard` shows "Not configured"; this is the **primary** scenario the page must be verified against, not a fallback edge case |
| Per-project failure isolation | `build_workspace_home_snapshot`'s per-project loop already isolates each project's data gathering | One project's git-discovery or report-join failure must not raise past its own `ProjectCard`/section — every other project's cards and sections render normally. Verified by the native page's own test suite injecting a single-project failure into a multi-project scenario. |

## 16. Non-goals for this spec

Matching `WORKSPACE_HOME_ARCHITECTURE.md` §19 and `DESKTOP_INCREMENT_1.md`: no worktree
mutation, no run start/cancel from Home, no auto-refresh/live polling, no new snapshot fields, no
new redaction logic beyond what `command_center.workspace_home` already implements, no merging or
deprecating of any existing Streamlit page.
