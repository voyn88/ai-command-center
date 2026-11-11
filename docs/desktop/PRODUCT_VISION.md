# AI Command Center — Desktop Product Vision

Status: **Approved; implementation underway (D1 shipped).** This document defines the product
purpose and boundary for the native desktop application described by the rest of
`docs/desktop/`. Desktop Increment 1's native shell has shipped in `command_center.desktop`; the
data-wiring, platform, and packaging increments remain ahead — see `DESKTOP_INCREMENT_1.md` for
what Desktop Increment 1 ships and what is still outstanding.

## 1. Product purpose

AI Command Center is a **local-first, single-user developer control plane** for managing
development projects, repositories, AI-agent sessions, execution runs, reports, and artifacts
across the canonical nine-project registry: `AICC`, `AIOS`, `AICOS`, `PRODUCT`, `ECOSYSTEM`,
`BANK`, `LEGAL`, `BUSINESS`, and `PERSONAL` ([ADR 0009](../adr/0009-canonical-project-registry-and-validating-task-import.md)).
It is the tool a developer opens every day to see the state of their work: which repositories
have uncommitted changes, which agent runs are active or finished, what a run's report said,
and what to do next.

It is a **development tool**, not an operational platform. It does not process production
traffic, does not serve other users, and does not sit in any production request path.

## 2. Target user

A single developer — the same person who configures repository paths, launches Claude Code
runs, and reads the resulting reports today through the existing Streamlit application. There
is no multi-user concept, no team dashboard, no shared server. The desktop application is
built for one person working on one machine, exactly like the Streamlit application it is
built to eventually replace for daily use.

## 3. Primary daily workflows

- Open the application and see, at a glance, the health of every configured project:
  repository status, active agent runs, recent run outcomes, and recent artifacts/reports.
- Configure or update a project's repository path.
- Drill into a project to see its worktrees, recent runs, reports, and artifacts.
- Review a completed agent run's report and its parsed verdict/findings.
- Adjust window, theme, and workspace preferences.

Workflows this product does **not** perform on a developer's behalf: banking transaction
processing, customer-data handling, production incident response, or any AML case work — see
§5.

## 4. Product boundary

AI Command Center manages **development projects, repositories, AI-agent sessions, execution
runs, reports, and artifacts.** Everything the desktop application shows or configures is
scoped to that: project metadata, git worktree/status information, the run/session/report
records already produced by `command_center.runtime`, and generated task/report files under
`generated/` and `reports/`.

It is explicitly **not** described, marketed, or built as an enterprise AML operating platform.
`BANK` and `LEGAL` are the sensitive subset of the canonical nine-project registry recorded in
[ADR 0009](../adr/0009-canonical-project-registry-and-validating-task-import.md) — like any
other project, they are tracked by the control plane, with sensitivity redaction already
implemented in `command_center.workspace_home`; they are not compliance systems in their own
right.

## 5. Relationship to AIOS and AICOS

- **AIOS** and **AICOS** are two entries in the canonical nine-project registry recorded in
  [ADR 0009](../adr/0009-canonical-project-registry-and-validating-task-import.md), tracked in
  exactly the same way as the other seven entries: a repository path, a set of agent runs,
  reports, and artifacts. AI Command Center does not implement AIOS or AICOS logic; it
  observes and orchestrates work *about* those repositories, the same way it does for every
  other project.
- **AI Command Center is not AICOS.** AICOS is the operational AML workplace — a separate
  system with its own production concerns, its own users, and its own data. AI Command Center
  does not connect to it as a runtime dependency, does not read or write its production data,
  and does not stand in for any part of its operational surface. A developer working on the
  AICOS *codebase* uses AI Command Center the same way they would for any other project in the
  registry: to track repository state, launch and review agent runs, and read reports — nothing
  about that workflow gives AI Command Center access to AICOS's production systems.
- AI Command Center does **not** connect directly to banking systems, data marts, queues,
  enterprise buses, or customer data, for any project, in any increment described by this
  documentation set.

## 6. Local-first philosophy

Every fact the application shows comes from the local filesystem and the local SQLite/JSON
runtime store already owned by `command_center.runtime` and `command_center/*` — there is no
server-side component, no hosted backend, and no account system. The desktop application
changes *how* this local state is presented (a native window instead of a browser tab); it does
not change *where* the state lives or who else can see it. This mirrors the existing Streamlit
application's scope (README.md, "Scope and limitations": "Local-only tool: no authentication,
no network services, no database" beyond the local SQLite/JSON store already in place).

## 7. Native desktop rationale

The application's **daily-use interface is desktop-native, not browser-based.** Streamlit's
top-to-bottom rerun model, its dependency on a local HTTP server and a browser tab, and its
limited affordances for window chrome, native menus, and system integration are a reasonable
fit for a fast-moving internal tool, but not for a control plane a developer keeps open all day
alongside their editor and terminal. A native window:

- starts and feels like every other desktop tool already on the developer's machine (Dock/Start
  Menu presence, native window management, system theme integration);
- does not require a browser tab to stay open, and is not subject to browser tab-suspension or
  accidental navigation;
- can eventually host affordances (embedded terminal, live session views, multi-window
  workflows) that a browser-rendered Streamlit page cannot support well.

Native desktop is a **product decision**, not a rewrite for its own sake — see §9 for how this
coexists with the existing Streamlit application during the transition.

## 8. Success criteria

Desktop Increment 1 (see `DESKTOP_INCREMENT_1.md`) is successful if:

- a developer can launch the desktop application on macOS (Apple Silicon) or Windows 11 (x64)
  without installing a separate Python interpreter, starting a browser, or starting a local HTTP
  server;
- the application reuses `command_center.runtime` and existing read models rather than
  reimplementing their logic;
- Workspace Home's native rendering reaches functional parity with the existing Streamlit
  Workspace Home page for the sections in scope (Projects, repository/worktree state, recent
  runs, artifacts, reports), preserving every sensitivity/redaction guarantee already
  implemented;
- repository-path configuration, theme, window, and preference changes persist across restarts
  using platform-native settings storage.

The long-term product is successful if the native desktop application becomes the primary daily
interface, with the Streamlit application retained only as a fallback until native parity is
deliberately achieved (§9) and a decision to retire it is made — not automatically, and not as
part of this documentation increment.

## 9. Non-goals

The following are explicitly **not** goals of the product direction this document describes,
for Desktop Increment 1 or its immediately following increments (see `DESKTOP_INCREMENT_1.md`
§"Out of scope" for the authoritative, binding list):

- Replacing or deprecating the existing Streamlit application on any fixed timeline. Existing
  Streamlit functionality **remains available** until native parity is deliberately achieved —
  this is a standing decision, not a placeholder.
- Multi-user, team, or server-hosted operation of any kind.
- Direct integration with banking systems, data marts, queues, enterprise buses, or customer
  data.
- Becoming, replacing, or interfacing with AICOS as an operational AML workplace.
- Mutating git state, starting/cancelling agent runs, or any other write beyond
  repository-path configuration, theme/window preferences, and window geometry (see
  `DESKTOP_INCREMENT_1.md` for the exact read/write boundary).
- Production code signing, notarization, auto-update, or a server/SSO mode.

## 10. Long-term product direction

Beyond Desktop Increment 1, the native application is expected to grow into full parity with,
and eventually beyond, today's Streamlit feature set: starting and cancelling agent runs,
streaming live session output, multi-session control, and richer mission-control-style
affordances (see `DESIGN_DIRECTIONS.md` for the two forward-compatible affordances this
increment reserves space for without building them yet). Each step beyond Increment 1 is its own
reviewed increment, following the sequencing in `IMPLEMENTATION_ROADMAP.md` — this document
records direction, not a committed schedule past D0.
