# AI Command Center — Desktop Increment 1 (Frozen Scope)

Status: **Frozen scope for D1 through D4; D1 implemented.** This document is the binding definition
of what "Desktop Increment 1" means for this product: four stages (D1–D4), each independently
gated. D1 (Native Shell Prototype, §2) has shipped; D2–D4 are not yet implemented — see
`IMPLEMENTATION_ROADMAP.md` for the smaller, commit-sized steps within each stage.

## 0. Desktop Increment 1 read/write boundary (binding decision 11)

Across all of D1–D4, the desktop application is **read-only except**:

- repository-path configuration (via existing `command_center.project_config` functions);
- theme and window preferences;
- window geometry.

## 1. Out of scope for all of D1–D4 (binding decision 12)

None of the following are built in any stage below: starting agents, run cancellation,
streaming/live output, git writes of any kind, multi-session control, an embedded terminal,
auto-update, production code signing/notarization, a server mode, SSO, or any AICOS interface.
A stage below that appears to need one of these must instead be reduced in scope or deferred to
a post-Increment-1 increment — it is not grounds for silently expanding this list.

## 2. D1 — Native Shell Prototype

**Objective**: prove the PySide6/Qt Widgets shell boots, renders navigation, and switches themes,
with no real data wiring — the smallest possible artifact that is a genuine native window rather
than a browser tab.

**Allowed files/packages**: new `command_center/desktop/` package (main window, sidebar, top
bar, navigation scaffolding, placeholder page widgets, theme-switch wiring); the desktop entry
point is `command_center/desktop/__main__.py`, launched as `python -m command_center.desktop`
(`ARCHITECTURE.md` §16) — no separate top-level launch script. No changes to `app.py` or any
existing `command_center/*` module.

**Dependencies**: `PySide6` added to a new, separate desktop dependency list (not merged into
`requirements.txt`, which stays Streamlit-only until a later decision to unify — see
`IMPLEMENTATION_ROADMAP.md` D1A for the exact dependency-file mechanics).

**Acceptance criteria**:
- The application launches as a single native window with `AppShell`, `Sidebar` (all nine
  sections per `INFORMATION_ARCHITECTURE.md` §1, with Sessions/Execution/Git/Artifacts/Reports/
  Agents rendered disabled per §2.1), and `TopBar`.
- Clicking Home/Projects/Settings switches the visible placeholder page; clicking a disabled item
  does nothing (§2.1 of `INFORMATION_ARCHITECTURE.md`).
- Theme switching (Light/Dark/System) visibly changes the window's palette, following the tokens
  in `DESIGN_SYSTEM.md` §1–§2.
- Window geometry persists across a restart (`QSettings`, via a minimal stand-in for
  `command_center.platform` if D3's full contract is not yet built — see
  `IMPLEMENTATION_ROADMAP.md` D1B for the exact sequencing).
- No import of `app.py`, `streamlit`, or any HTTP/browser dependency anywhere on the startup path
  (`ARCHITECTURE.md` §8.1).

**Tests**: `pytest-qt`-based widget tests (offscreen `QApplication`) verifying the shell renders,
navigation switches the active page, disabled items are inert, and theme switching updates the
active palette token.

**Explicit non-goals**: no real Workspace Home data wiring (placeholder content only); no
repository-path configuration; no settings beyond theme/window geometry.

**Primary risks**: PySide6 packaging/import friction on a fresh developer machine (mitigated by
resolving this early, before any other stage depends on it); Qt stylesheet/token-system
mismatches between platforms discovered only at this stage (mitigated by manual smoke-testing on
both target platforms before D1's gate closes, not deferred to D4).

## 3. D2 — Native Workspace Home

**Objective**: wire the Home page to real data via `WORKSPACE_HOME_SPEC.md`, using existing read
models exclusively.

**Allowed files/packages**: new `command_center/application/` package (Workspace Home adapter,
`QThreadPool`/`QRunnable` worker plumbing per `ARCHITECTURE.md` §10); `command_center/desktop/`'s
Home page widget and its child components (`MetricCard`, `ProjectCard`, `RunSummary`,
`ActivityItem`, `ArtifactRow`, `ReportRow`, `WorktreeRow`, `EmptyState`, `ErrorState`,
`LoadingSkeleton`, `StatusBadge`, per `DESIGN_SYSTEM.md`). No changes to
`command_center.workspace_home` or any other existing read model unless a gap is found — and if
one is, it is an additive, reviewed change to that existing module, not a fork of its logic
(`ARCHITECTURE.md` §7).

**Dependencies**: D1 complete (shell, navigation, theming).

**Acceptance criteria**: every item in `WORKSPACE_HOME_SPEC.md` §15's edge-state table renders
correctly, including the default in which every canonical-registry project is unconfigured, as
recorded in [ADR 0009](../adr/0009-canonical-project-registry-and-validating-task-import.md);
the dual-layer BANK/LEGAL redaction regression (`WORKSPACE_HOME_SPEC.md` §10) passes; manual
refresh (§12) works via a
`QThreadPool` worker with no GUI-thread blocking; `task_count` is labeled "Tasks" (never
"Kanban"/"Open tasks," per `WORKSPACE_HOME_SPEC.md` §11); loading/stale-data indication (§13/§14
of `WORKSPACE_HOME_SPEC.md`) is visible.

**Tests**: adapter-level `pytest` tests (no `QApplication` required, per `ARCHITECTURE.md` §17)
covering the same scenarios `tests/test_workspace_home.py` already covers, reused rather than
reinvented; `pytest-qt` rendering tests covering empty/populated/BANK-LEGAL-only states and the
per-project failure-isolation case.

**Explicit non-goals**: no run start/cancel Quick Actions (not available in D1, per
`WORKSPACE_HOME_SPEC.md` §9); no worktree mutation; no new snapshot fields.

**Primary risks**: accidentally reintroducing a redaction gap while adapting the existing dict
shape into Qt view models (mitigated by the dual-layer test requirement above, mirroring the
existing Streamlit page's own test strategy); background-thread/GUI-thread boundary violations
(mitigated by code review against `ARCHITECTURE.md` §9–§10 at this stage's gate).

## 4. D3 — Projects and Settings

**Objective**: activate Projects (repository-path configuration) and Settings (preferences,
theme, window geometry, platform integration).

**Allowed files/packages**: `command_center/desktop/`'s Projects and Settings page widgets
(`SettingsForm`, per `DESIGN_SYSTEM.md`); the new `command_center/platform/` package
(`PLATFORM_BEHAVIOR.md`'s full contract); `command_center/application/`'s Projects adapter
(wrapping existing `project_config.load_project_configs`/`save_repository_path`/
`validate_repository_path` — no new validation logic duplicated in the adapter).

**Dependencies**: D1 and D2 complete.

**Acceptance criteria**: a user can view and edit a project's `repository_path` through the
native Projects page, validated through the existing `project_config.validate_repository_path`
(no new validation logic); Settings persists theme/density/window preferences via
`command_center.platform`'s `QSettings`-backed contract; Finder/Explorer reveal
(`reveal_in_file_manager`) works on both target platforms for a project's configured repository
path.

**Tests**: adapter-level `pytest` tests for the Projects adapter (path validation success/failure
cases, reusing `tests/test_project_config.py`'s existing scenarios rather than re-deriving them);
`command_center.platform` tests using `pytest` monkeypatching to simulate both platforms;
`pytest-qt` tests for the Settings form's persistence round-trip.

**Explicit non-goals**: no task/run creation from Projects; no non-path settings that imply a
write beyond theme/window/workspace preferences (§0).

**Primary risks**: `QSettings` behavior differing subtly between the macOS and Windows native
backends (mitigated by testing the persistence round-trip on both platforms before this stage's
gate closes, not assumed identical from one platform's testing alone).

## 5. D4 — Packaging

**Objective**: produce installable, unsigned development builds for both target platforms and
verify them on a clean machine.

**Allowed files/packages**: **PyInstaller** spec files (`ARCHITECTURE.md` §16) under
`packaging/macos/` and `packaging/windows/` (`IMPLEMENTATION_ROADMAP.md` D4A/D4B); no changes to
`command_center/desktop/`, `command_center/application/`, or `command_center/platform/` beyond
what packaging itself requires (e.g. resource-path resolution that must differ between a
packaged and an unpacked run — handled inside `command_center.platform`, not scattered
elsewhere).

**Dependencies**: D1, D2, and D3 complete.

**Acceptance criteria**: a macOS Apple Silicon development `.app`/DMG launches on a clean machine
(no developer's Python environment, no `.venv`) and reaches the same shell/Home/Projects/Settings
functionality as the source checkout; a Windows 11 x64 development `.exe`/installer does the same
on a clean Windows machine; neither packaged build requires a browser, a local HTTP server, or a
separately-installed Python interpreter to run (binding decision 8).

**Tests**: manual clean-machine smoke tests on both platforms (launch, navigate all three active
pages, configure a repository path, change theme, restart and confirm persistence, quit
cleanly) — recorded as a checklist result, not automated in D4 itself (automating clean-machine
packaging verification is a reasonable future improvement, not required for this stage's gate).

**Explicit non-goals**: no code signing, no notarization, no auto-update mechanism, no installer
telemetry/analytics.

**Primary risks**: PySide6's native Qt library bundling behaving differently across the two
packaging toolchains (mitigated by testing both platforms' packaged builds before this stage's
gate closes, never assumed from source-checkout testing alone); Gatekeeper/SmartScreen friction
being mistaken for a packaging bug rather than the expected unsigned-build behavior documented in
`PLATFORM_BEHAVIOR.md` §1/§2.

## 6. Final acceptance criteria for Desktop Increment 1

Desktop Increment 1 (D1 through D4, taken together) is complete when **all** of the following
hold:

1. A macOS Apple Silicon and a Windows 11 x64 development build both launch without a browser, a
   local HTTP server, or a separate Python installation.
2. The native application reuses `command_center.runtime` and existing read models — no forked
   or duplicated business logic anywhere in `command_center.desktop`/`command_center.application`.
3. Home, Projects, and Settings are fully active; Sessions, Execution, Git, Artifacts, Reports,
   and Agents are visibly present but disabled, per `INFORMATION_ARCHITECTURE.md` §2.
4. Every BANK/LEGAL sensitivity guarantee already implemented in
   `command_center.workspace_home` is preserved on the native page, verified by the same
   dual-layer test strategy the existing Streamlit page uses.
5. The desktop application performs no write beyond repository-path configuration, theme/window
   preferences, and window geometry (§0).
6. The existing Streamlit application (`app.py`) is unchanged and continues to work exactly as it
   does today — Desktop Increment 1 adds a new consumer of `command_center/*`, it does not modify
   or retire the existing one.
7. `ruff check .` and the full `pytest` suite remain green, with the desktop test suite
   (`pytest-qt`-based) passing alongside the existing suite.
8. Manual clean-machine smoke tests (D4) pass on both target platforms.

Desktop Increment 1 does **not** require: starting/cancelling agent runs, an embedded terminal,
production signing, a server mode, or any AICOS interface (§1) — those remain out of scope until
a future, separately-reviewed increment.
