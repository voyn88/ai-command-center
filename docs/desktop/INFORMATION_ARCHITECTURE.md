# AI Command Center — Desktop Information Architecture

Status: **All nine top-level sections active.** The original D1 subset is retained
below as historical scope; P2 activated Sessions, Execution, Git, Artifacts,
Reports and Agents through non-blocking application adapters on 2026-08-08.

## 1. Eventual navigation (target, all increments)

Nine top-level sections, in sidebar order:

| # | Section | Purpose |
|---|---|---|
| 1 | Home | Cross-project rollup — native equivalent of the existing Streamlit Workspace Home |
| 2 | Projects | Per-project detail: repository status, worktrees, configuration |
| 3 | Sessions | v2 Execution Center sessions |
| 4 | Execution | Live and historical agent runs |
| 5 | Git | Read-only git status/log/diff/branches/remotes/worktrees, per project |
| 6 | Artifacts | Generated task files (`generated/<PROJECT>/*.md`) |
| 7 | Reports | Run reports (`reports/<PROJECT>/*.md`), parsed verdict/findings |
| 8 | Agents | Agent/task-type catalog and usage stats |
| 9 | Settings | Repository paths, theme, window, and workspace preferences |

This is the **target** structure existing to give every later increment a stable place to land.
It is not a commitment that Sessions/Execution/Git/Artifacts/Reports/Agents ship in any specific
increment beyond what `IMPLEMENTATION_ROADMAP.md` schedules.

## 2. Desktop Increment 1 navigation

Desktop Increment 1 originally activated three sections. P2 now activates all
nine while preserving the same navigation order:

| Section | D1 state | Notes |
|---|---|---|
| Home | Active (from D2) | Placeholder-only in D1 itself (see `DESKTOP_INCREMENT_1.md` D1); real data wiring lands in D2 |
| Projects | Active (from D3) | Repository-path configuration; placeholder-only until D3 |
| Settings | Active (from D3) | Theme, window, preferences; placeholder-only until D3 |
| Sessions, Execution, Git, Artifacts, Reports, Agents | **Active from P2** | Live runtime/read-model data, non-blocking refresh, Russian empty/error states |

D1 itself (the shell prototype, see `DESKTOP_INCREMENT_1.md`) renders all nine navigation
entries but wires no real data to any of them — Home/Projects/Settings become genuinely active
in D2/D3 as their respective increments land. This document's "active" column reflects the
increment in which each page stops being a placeholder, not D1's literal first commit.

### 2.1 Disabled-section rendering rule

A future section **may** be shown disabled in the sidebar rather than hidden entirely, when
doing so improves orientation — specifically: showing the full eventual navigation set from day
one communicates the product's scope and prevents the sidebar from visibly growing/reflowing
between increments. A disabled `NavigationItem` (see `DESIGN_SYSTEM.md`):

- renders at reduced opacity, with the same icon and label as its eventual active state;
- is not focusable via keyboard Tab order (it is not an interactive control while disabled);
- shows a tooltip on hover: "Available in a future release" (exact copy may be refined at
  implementation time, but the section must never claim a specific version/date it cannot
  guarantee);
- never navigates anywhere when clicked — there is no dead page behind it.

Hiding a future section entirely (instead of showing it disabled) is equally acceptable and left
to implementation-time judgment per section; this document does not mandate one treatment over
the other, only that whichever is chosen must not present a broken or empty page as if it were
finished.

## 3. Navigation hierarchy

- **Top level**: the nine sections in §1, exactly one active at a time, selected via the
  sidebar (`Sidebar`/`NavigationItem`, see `DESIGN_SYSTEM.md`).
- **Second level**: within a section, a page may show sub-views (e.g. Projects → a specific
  project's detail) via in-page navigation (tabs, a master-detail split, or a breadcrumb) rather
  than additional sidebar entries. The sidebar itself never grows a second level of nesting.
- There is no modal "page" navigation — dialogs (`Dialog`, see `DESIGN_SYSTEM.md`) are used only
  for short-lived, dismissible interactions (confirmations, settings sub-forms), never as a
  substitute for a sidebar section.

## 4. Page responsibilities

| Page | Responsibility | Responsibility it does NOT have |
|---|---|---|
| Home | Cross-project rollup: read and display, via `command_center.application`'s Workspace Home adapter | No editing, no run control |
| Projects | List projects, show per-project repository/worktree state, host repository-path configuration | No task/run creation in D1 |
| Settings | Theme, density, window-geometry reset, workspace preferences | No repository-path editing (that lives on Projects) |
| Sessions | Runtime sessions and latest execution state | No hidden mutation |
| Execution | Active and recent runs | No implicit launch or cancellation |
| Git | Repository/worktree status with BANK/LEGAL path redaction | No Git writes |
| Artifacts/Reports | Sanitized workspace read models | No reconstruction of redacted paths |
| Agents | Provider readiness and live running count | No direct provider launch |

## 5. Project selection

A **global project switcher** (`ProjectSwitcher`, see `DESIGN_SYSTEM.md`) lives in the `TopBar`
and scopes any page that is per-project (Projects, and eventually Sessions/Execution/Git/
Artifacts/Reports). Home is explicitly cross-project and is not scoped by the switcher — it
always shows the full rollup across every configured project, matching the existing Streamlit
Workspace Home page's behavior. Selecting a project in the switcher persists as a window-scoped
preference (not written to `data/project_config.json`, which holds repository *paths*, not UI
selection state).

## 6. Global refresh

A single, discoverable "Refresh" action (top bar) re-runs the active page's data fetch through
`command_center.application` (§10, `ARCHITECTURE.md`) — mirroring the existing Streamlit
application's `st.rerun()`-driven manual refresh pattern for Workspace Home
(`WORKSPACE_HOME_ARCHITECTURE.md` §12: "a manual 'Refresh' button... is sufficient"). Desktop
Increment 1 introduces no automatic polling or live auto-refresh; refresh is always
user-initiated, exactly like today's Streamlit page.

## 7. Future command/search entry

A reserved top-bar affordance (an icon/button, initially non-functional or hidden entirely
behind a feature check) marks where a future command-palette-style entry point (mirroring the
existing Streamlit application's `Mod+K` command palette) will live once built. Desktop
Increment 1 does not implement command/search functionality — this section records where it
will attach, not a commitment to ship it in D1.

## 8. Drill-down behavior

Selecting a project card on Home (once real data lands in D2) navigates to that project's detail
view on the Projects page, with the `ProjectSwitcher` updated to match — a single, consistent
drill-down path rather than a separate detail view owned by Home itself. Home never renders a
project's full detail inline; it always defers to Projects for that.

## 9. No raw internal identifiers in normal user flows

Run ids (`run_id`), session ids, and SQLite row identifiers are internal implementation details
of `command_center.runtime` and must never be the primary label a user sees in normal
navigation or drill-down flows — a run is identified to the user by project, task type, state,
and timestamp (matching the existing Streamlit Runs page's convention), with the raw id
available only in a detail view or as copyable metadata, not as the headline label of a list
row. This mirrors `WORKSPACE_HOME_ARCHITECTURE.md` §3's existing rule that Home never exposes
"raw internal IDs or SQLite concepts" as primary UI content.

## 10. Keyboard navigation expectations

- Sidebar navigation entries are reachable via Tab, in top-to-bottom order, and activatable via
  Enter/Space — standard Qt `QAbstractButton`/`QListWidget` keyboard behavior, not a custom
  reimplementation.
- A disabled `NavigationItem` (§2.1) is skipped in Tab order.
- Every dialog (`Dialog`, see `DESIGN_SYSTEM.md`) is dismissible via Escape and traps Tab focus
  within itself while open, per standard Qt modal-dialog behavior.
- The global refresh action (§6) has a discoverable keyboard shortcut (platform-appropriate
  accelerator, e.g. `Cmd+R` / `Ctrl+R`), defined at implementation time in
  `DESIGN_SYSTEM.md`'s keyboard-behavior conventions.

## 11. Empty and error-state navigation behavior

- A page that has nothing to show (e.g. Home with every project in the canonical registry
  unconfigured — the expected fresh-install default, per `WORKSPACE_HOME_ARCHITECTURE.md` §7.1
  and [ADR 0009](../adr/0009-canonical-project-registry-and-validating-task-import.md)) renders its `EmptyState`
  component with a clear next action (e.g. "Configure a repository path" → navigates to
  Projects), never a blank pane.
- A page-level failure (e.g. a single project's git discovery failing) renders `ErrorState` for
  the affected region only — per-project/per-section failure isolation, matching
  `WORKSPACE_HOME_ARCHITECTURE.md` §6/§12's existing rule that one project's failure must never
  block rendering of the others. Navigation itself is never blocked by a page-level data error;
  the sidebar and top bar remain interactive even if the current page's content failed to load.
