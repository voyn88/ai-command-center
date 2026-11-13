# AI Command Center — Desktop Implementation Roadmap

Status: **D1–D4 foundation complete; P2 operational sections implemented
2026-08-08.** This document preserves the historical sequence of
`DESKTOP_INCREMENT_1.md`'s four stages (D1–D4) as small, independently
reviewable, commit-sized steps. The sections below are historical scope and
acceptance records, not an open-work claim.

## P2 — Native operational sections

- Sessions and Execution read the canonical runtime database.
- Git reads repository/worktree status without writes and redacts sensitive
  project paths.
- Artifacts and Reports reuse the sanitized Workspace Home read models.
- Agents combines provider readiness with live scheduler load.
- Every fetch uses the shared cooperative Qt worker pattern; active-page refresh
  does not block navigation.
- Acceptance evidence: application-adapter tests, navigation/rendering tests,
  Russian UI gate, full desktop suite, and a real production-data read journey.

Every increment below follows the same shape: scope, dependencies, files expected, acceptance
criteria, test requirements, forbidden scope. An increment is sized to be reviewable as one pull
request; an increment that grows beyond that during implementation should be split further, not
merged as one large change.

## D1A — Dependency and package skeleton

- **Scope**: add `PySide6` as a new, separate desktop dependency (`requirements-desktop.txt` —
  not merged into `requirements.txt`); create the empty `command_center/desktop/` package
  skeleton (`__init__.py`, `__main__.py`, module layout stubs with docstrings only, no widget
  logic yet); add `pytest-qt` (the selected Qt test tooling, `ARCHITECTURE.md` §17) to
  `requirements-dev.txt`.
- **Dependencies**: none (first step).
- **Files expected**: `requirements-desktop.txt`, `command_center/desktop/__init__.py`,
  `command_center/desktop/__main__.py`, a small number of empty module stubs,
  `requirements-dev.txt` extended with `pytest-qt`.
- **Acceptance criteria**: `import command_center.desktop` succeeds in the existing virtual
  environment after installing the new requirements file; the existing test suite is unaffected
  (`command_center/desktop` is not yet imported by anything that runs today).
- **Test requirements**: a single smoke test importing `command_center.desktop` and asserting no
  side effects (no `QApplication` constructed at import time).
- **Forbidden scope**: no widget code, no window, no `app.py` changes.

## D1B — Main window and application lifecycle

- **Scope**: implement the one-`QApplication`, one-main-window lifecycle described in
  `ARCHITECTURE.md` §3 — `AppShell` with an empty content area, window geometry persistence via a
  minimal `QSettings` wrapper (a stand-in for `command_center.platform`'s full contract, which
  lands at D3A/D3B).
- **Dependencies**: D1A.
- **Files expected**: `command_center/desktop/__main__.py`, `command_center/desktop/main_window.py`.
- **Acceptance criteria**: launching the entry point shows an empty native window; closing and
  relaunching restores the previous window geometry.
- **Test requirements**: `pytest-qt` test constructing the main window under an offscreen
  `QApplication` and asserting geometry persistence round-trips through `QSettings`.
- **Forbidden scope**: no sidebar, no navigation, no theming yet.

## D1C — Navigation and themes

- **Scope**: implement `Sidebar`, `NavigationItem` (all nine sections, six disabled per
  `INFORMATION_ARCHITECTURE.md` §2.1), `TopBar` (with the reserved status area and refresh-action
  placeholder per `DESIGN_DIRECTIONS.md` §5), placeholder page widgets for Home/Projects/
  Settings, and Light/Dark/System theme switching using the tokens in `DESIGN_SYSTEM.md` §1–§2.
- **Dependencies**: D1B.
- **Files expected**: `command_center/desktop/sidebar.py`, `.../navigation_item.py`,
  `.../top_bar.py`, `.../pages/{home,projects,settings}.py` (placeholder content), a style/token
  module implementing `DESIGN_SYSTEM.md`'s §1 tokens.
- **Acceptance criteria**: all nine sections render (three active-placeholder, six disabled);
  navigation switches the visible page; disabled items are inert and skipped in Tab order; theme
  switching changes the visible palette and persists across restart.
- **Test requirements**: `pytest-qt` tests for navigation switching, disabled-item inertness and
  Tab-order exclusion, and theme persistence.
- **Forbidden scope**: no real data on any page.

## D1 final gate

- **Scope**: the checkpoint verifying `DESKTOP_INCREMENT_1.md` §2's D1 acceptance criteria as a
  whole, across D1A–D1C, on a real macOS and a real Windows machine (not only CI/offscreen
  testing).
- **Dependencies**: D1A, D1B, D1C.
- **Files expected**: none new — this is a verification gate, not a code increment.
- **Acceptance criteria**: `DESKTOP_INCREMENT_1.md` §2's full acceptance-criteria list passes on
  both target platforms.
- **Test requirements**: full `pytest-qt` suite green; manual smoke pass on both platforms,
  recorded.
- **Forbidden scope**: no scope creep into D2 content while closing this gate.

## D2A — Application service adapter

- **Scope**: create `command_center/application/`; implement the Workspace Home adapter wrapping
  `build_workspace_home_snapshot`, constructing the one application-wide `ExecutionCenterAPI`
  (`ARCHITECTURE.md` §3, §5).
- **Dependencies**: D1 final gate.
- **Files expected**: `command_center/application/__init__.py`,
  `command_center/application/workspace_home_adapter.py`.
- **Acceptance criteria**: the adapter is callable from plain `pytest` with no `QApplication`,
  returning the same snapshot shape `build_workspace_home_snapshot` already returns.
- **Test requirements**: `pytest` tests reusing the existing `tests/test_workspace_home.py`
  scenarios through the adapter, confirming the adapter adds no transformation that could drop or
  alter a field.
- **Forbidden scope**: no Qt import in this package; no widget code yet.

## D2B — Async worker framework

- **Scope**: implement the `QThreadPool`/`QRunnable`/signal pattern from `ARCHITECTURE.md` §10,
  generic enough to wrap any `command_center.application` adapter call, plus the cooperative-
  cancellation flag from §11.
- **Dependencies**: D2A.
- **Files expected**: `command_center/desktop/workers.py`.
- **Acceptance criteria**: a sample adapter call executed through the worker framework returns its
  result via a Qt signal on the GUI thread, verified not to block the GUI thread during execution.
- **Test requirements**: `pytest-qt` test verifying the GUI thread remains responsive
  (e.g. processes a queued event) while a deliberately slow adapter call runs on the worker
  thread.
- **Forbidden scope**: no forceful thread termination; no second threading mechanism introduced
  alongside `QThreadPool`.

## D2C — Workspace Home layout

- **Scope**: implement the Home page's real layout per `WORKSPACE_HOME_SPEC.md` §1–§9 — wide/
  medium/minimum layouts, `MetricCard`/`ProjectCard`/`RunSummary`/`ActivityItem`/`ArtifactRow`/
  `ReportRow`/`WorktreeRow`/`StatusBadge`, wired through D2A/D2B.
- **Dependencies**: D2A, D2B.
- **Files expected**: `command_center/desktop/pages/home.py` (real implementation, replacing the
  D1C placeholder), the component widgets listed above under `command_center/desktop/components/`.
- **Acceptance criteria**: `WORKSPACE_HOME_SPEC.md` §1–§9 render correctly for a populated
  scenario (≥1 configured project, ≥1 run, ≥1 artifact, ≥1 report, ≥1 activity event).
- **Test requirements**: `pytest-qt` rendering tests for the populated scenario.
- **Forbidden scope**: no edge-state handling yet (that is D2D); no Quick Actions beyond
  navigation-only ones already approved in `WORKSPACE_HOME_SPEC.md` §9.

## D2D — Edge states and accessibility

- **Scope**: implement `EmptyState`, `ErrorState`, `LoadingSkeleton` per `DESIGN_SYSTEM.md` §7.15–
  §7.17; wire every edge case in `WORKSPACE_HOME_SPEC.md` §15 (unconfigured, invalid path,
  non-git, detached HEAD, every canonical-registry project unconfigured, per-project failure
  isolation; see [ADR 0009](../adr/0009-canonical-project-registry-and-validating-task-import.md));
  implement the accessible-name/description wiring from `DESIGN_SYSTEM.md` §4 for every Home
  component; verify the dual-layer BANK/LEGAL redaction regression (`WORKSPACE_HOME_SPEC.md` §10).
- **Dependencies**: D2C.
- **Files expected**: updates to the D2C component files; `EmptyState`/`ErrorState`/
  `LoadingSkeleton` widget files.
- **Acceptance criteria**: `WORKSPACE_HOME_SPEC.md` §15's full edge-state table renders correctly;
  the BANK/LEGAL dual-layer test (snapshot-level + rendered-widget-level) passes.
- **Test requirements**: `pytest-qt` tests for every §15 edge case and the dual-layer redaction
  regression, mirroring `tests/test_workspace_home_ui.py`'s existing structure.
- **Forbidden scope**: no new redaction logic in `command_center.desktop`/`command_center.application`
  — every redaction guarantee is inherited from `command_center.workspace_home` unchanged.

## D2 final gate

- **Scope**: verify `DESKTOP_INCREMENT_1.md` §3's D2 acceptance criteria as a whole.
- **Dependencies**: D2A, D2B, D2C, D2D.
- **Files expected**: none new.
- **Acceptance criteria**: `DESKTOP_INCREMENT_1.md` §3's full list passes.
- **Test requirements**: full `pytest`/`pytest-qt` suite green.
- **Forbidden scope**: no scope creep into D3 content while closing this gate.

## D3A — Projects

- **Scope**: implement the Projects page (repository-path viewing/editing) and its
  `command_center.application` adapter wrapping existing `project_config` functions verbatim.
- **Dependencies**: D2 final gate.
- **Files expected**: `command_center/desktop/pages/projects.py`,
  `command_center/application/projects_adapter.py`.
- **Acceptance criteria**: `DESKTOP_INCREMENT_1.md` §4's Projects-related acceptance criteria
  pass.
- **Test requirements**: adapter-level `pytest` tests reusing `tests/test_project_config.py`
  scenarios; `pytest-qt` tests for the Projects page's edit/validate/save flow.
- **Forbidden scope**: no new path-validation logic outside `project_config.validate_repository_path`.

## D3B — Settings and platform integration

- **Scope**: create `command_center/platform/` with the full contract from `PLATFORM_BEHAVIOR.md`
  §3; implement the Settings page (`SettingsForm`) for theme/density/window-geometry/workspace
  preferences, replacing D1B/D1C's minimal `QSettings` stand-in with the real
  `command_center.platform` abstraction.
- **Dependencies**: D3A (may proceed in parallel with D3A if no shared file conflicts arise, since
  Projects and Settings touch different pages).
- **Files expected**: `command_center/platform/__init__.py` and per-concern modules
  (`file_manager.py`, `theme.py`, `paths.py`); `command_center/desktop/pages/settings.py`.
- **Acceptance criteria**: `DESKTOP_INCREMENT_1.md` §4's Settings/platform-related acceptance
  criteria pass on both target platforms.
- **Test requirements**: `command_center.platform` `pytest` tests (monkeypatched per platform);
  `pytest-qt` tests for the Settings persistence round-trip.
- **Forbidden scope**: no OS-specific branching introduced anywhere outside
  `command_center.platform` (`ARCHITECTURE.md` §8.1).

## D3 final gate

- **Scope**: verify `DESKTOP_INCREMENT_1.md` §4's D3 acceptance criteria as a whole, on both
  target platforms.
- **Dependencies**: D3A, D3B.
- **Files expected**: none new.
- **Acceptance criteria**: `DESKTOP_INCREMENT_1.md` §4's full list passes on both platforms.
- **Test requirements**: full `pytest`/`pytest-qt` suite green; manual cross-platform smoke pass
  for Projects/Settings specifically.
- **Forbidden scope**: no scope creep into D4 packaging concerns while closing this gate.

## D4A — macOS packaging

- **Scope**: produce an unsigned development `.app`/DMG via PyInstaller (`ARCHITECTURE.md` §16);
  verify resource-path resolution works identically in a packaged and an unpacked run (via
  `command_center.platform`, not ad-hoc branching).
- **Dependencies**: D3 final gate.
- **Files expected**: `packaging/macos/` (PyInstaller spec file and any bundle metadata).
- **Acceptance criteria**: `DESKTOP_INCREMENT_1.md` §5's macOS acceptance criteria pass on a clean
  Apple Silicon machine.
- **Test requirements**: manual clean-machine smoke checklist, recorded.
- **Forbidden scope**: no signing/notarization (§1 of `DESKTOP_INCREMENT_1.md`).

## D4B — Windows packaging

- **Scope**: produce an unsigned development `.exe`/installer via PyInstaller (`ARCHITECTURE.md`
  §16); verify resource-path resolution on Windows.
- **Dependencies**: D3 final gate (may proceed in parallel with D4A).
- **Files expected**: `packaging/windows/` (PyInstaller spec file and any installer metadata).
- **Acceptance criteria**: `DESKTOP_INCREMENT_1.md` §5's Windows acceptance criteria pass on a
  clean Windows 11 x64 machine.
- **Test requirements**: manual clean-machine smoke checklist, recorded.
- **Forbidden scope**: no production signing (§1 of `DESKTOP_INCREMENT_1.md`).

## D4 final gate

- **Scope**: verify `DESKTOP_INCREMENT_1.md` §6's final Desktop Increment 1 acceptance criteria,
  in full, across both platforms.
- **Dependencies**: D4A, D4B.
- **Files expected**: none new.
- **Acceptance criteria**: `DESKTOP_INCREMENT_1.md` §6's complete list passes. This is the gate
  that closes Desktop Increment 1 itself.
- **Test requirements**: full `pytest`/`pytest-qt` suite green; both platforms' clean-machine
  smoke checklists recorded as passing.
- **Forbidden scope**: no work toward any post-Increment-1 feature (run start/cancel, streaming,
  embedded terminal, signing, server mode, SSO, AICOS interfaces) begins as part of closing this
  gate.
