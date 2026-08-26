# Integration Center (AICC-INT-001)

AICC as the single control center for every repository the operator works
on. This document is the design for **increment 1**: a project registry,
read-only health collectors, and a "Projects" UI surface. Design first, then
code — the implementation in `command_center/integration/` must not exceed
what is written here.

> **Privacy rule.** This repository is public. The registry's *contents*
> (real project names, machine-local paths, remotes) are operator
> configuration and live only in the runtime-local, gitignored store —
> nothing in committed code, docs or tests may enumerate the operator's
> actual projects or paths. A fitness test
> (`tests/architecture/test_integration_privacy_fitness.py`) enforces this.

## Why

An operator developing N repositories in parallel keeps their state in N
terminal windows and N GitHub tabs. The Integration Center gives one page
that answers, per locally-configured project: *is the working tree clean, is
CI green, what PRs are open, when did it last move, and what AICC tasks
target it.*

## Data model — project registry

Store: **`data/integration_registry.json`** (+ `integration_registry.lock`),
gitignored, machine-local. A JSON dict store in the style of
`project_config.json` — *not* a new `runtime.db` table: the registry is
operator configuration (machine-local repo paths), not execution truth, and
adding tables to the runtime store is explicitly out of scope for a
pure-addition increment.

Single writer: **`command_center/integration/registry.py`** (documented in
`docs/AUTHORITY_MAP.md`). Every write goes through `storage.file_lock` +
`storage.atomic_write_json`, mirroring `project_config.py`. No other module
may write the file.

Registry entry (one per project):

| Field            | Type | Meaning |
|------------------|------|---------|
| `id`             | str  | Stable registry key (repo-style slug, e.g. `example-app`). |
| `name`           | str  | Display name. |
| `kind`           | str  | One of `application`, `service`, `library`, `infrastructure`, `other`. |
| `project`        | str  | The `models.PROJECT_IDS` namespace its AICC tasks use. Validated on write — this is the join key to the task board. |
| `repo_path`      | str \| null | Machine-local checkout path (`~` allowed, e.g. `~/path/to/repo`). Null → health shows `unconfigured`, nothing else is collected. |
| `remote`         | str \| null | `owner/repo` for `gh` (falls back to the checkout's `origin` when null). |
| `default_branch` | str  | Branch whose CI state is shown (default `main`). |

The file is seeded on first read with two generic placeholder entries
(`example-app`, `example-lib`, both unconfigured) so the page renders before
anything real is registered; the operator's actual projects enter the store
via `registry.upsert_entry` (programmatic in increment 1 — UI editing is a
later increment) and never leave the local machine.

## Health signals (read-only collectors)

`command_center/integration/collectors.py`. Strictly read-only — the
collectors never mutate a repository, never `git fetch`, never call a
mutating `gh` verb, and write nothing anywhere. Every signal degrades to a
structured `{"available": False, "error": …}` instead of raising: a missing
checkout, a missing `gh` binary, or an offline network must never break the
page.

| Signal | Source | Notes |
|---|---|---|
| Worktree state | `Path` checks + `git_info.get_status` | `unconfigured` / `invalid_path` / `not_git_repo` / `ok` — same vocabulary as Workspace Home. |
| Git status | `command_center.git_info` (same read-only helpers `ui/git_readers` wraps) | branch, dirty, modified/untracked counts, last commit. |
| Last activity | `git log -1 --format=%cI` via `git_info.run_git_command` | Local clone timestamp — no network. |
| CI state | `gh run list --branch <default_branch> --limit 1 --json status,conclusion` | `success` / `failure` / `in_progress` / `unknown`. |
| Open PRs | `gh pr list --state open --json number` | Count only in increment 1. |

`gh` calls run with an explicit timeout, `cwd=repo_path`, and are isolated in
one private `_run_gh` seam so tests mock a single function. Collection is
on-demand (button / first page visit, cached in `st.session_state`), never a
background thread — the supervisor and scheduler are untouched.

## Cross-project tasks

No new task store and no schema change. A "task for project X" **is** an
ordinary `tasks_repository` record whose existing `project` field is the
registry entry's `project` namespace — the registry only joins onto it:

- The Projects page filters `load_all()` by `entry["project"]` for the
  drill-down (open = status ≠ Done), exactly like the Kanban filters.
- Under `AICC_TASKS_BACKEND=aios`, the same records flow through
  `AIOSTasksRepository` / `aios_tasks.AiosTasksClient` unchanged — `project`
  is already part of the task payload the AIOS backend round-trips, so the
  registry works identically over both backends and needs no knowledge of
  which one is active.
- Recent runs come from `runtime.runs_read.list_unified_runs` filtered by
  the same `project` value — the existing read model, no new query layer.

## UI surface

`command_center/ui/integration_center.py`, registered in `app.py`'s NAV as
`integration` ("Integration Center"). One page, two levels:

1. **Registry list** — one card per entry: name, kind badge, worktree-state
   badge, dirty/clean badge, CI badge, open-PR count, last activity, open
   task count. A "Собрать статус" button triggers collection; results are
   cached in `st.session_state` so reruns don't hammer `gh`.
2. **Drill-down** — selecting an entry shows its open AICC tasks (title,
   status, priority) and its recent runs (from `list_unified_runs`).

The module follows the `command_center.ui` boundary: rendering only; all
logic lives in `integration/`.

## Out of scope for increment 1

- **Writes from the UI** (editing repo paths/remotes) — registry API exists
  (`upsert_entry`), UI stays read-only.
- **Cross-project task creation / launch** from this page — tasks are
  created where they are today; the page only *shows* them.
- **Background/scheduled collection**, health history, alerting.
- **Any runtime.db schema change, supervisor change, or new engine** — the
  boundary baseline must not grow categories.
- **Multiple task-backend namespaces** — one backend, `project` field as
  namespace, as today.
