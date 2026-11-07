# SRV-07h — non-SQLite authorities inventory

SRV-07's own formulation covers "SQLite/JSON/JSONL" sources, but the coverage
machinery that grew alongside it (`tests/db/test_mirror_coverage.py`'s Gate
A/B, built for `VOYN-W0-AICC-SRV-01B`) only ever asked about SQLite-authority
tables. `queue_entry` is the one JSON-authority case that got a contract of
its own (`command_center/db/queue_store.py`, whole-list replacement, a
`position` column the JSON authority does not have) — and until this task,
it was also the *only* JSON/JSONL store anyone had recorded a verdict for.
Every other store in `docs/AUTHORITY_MAP.md` had no documented position on
whether SRV-07 touches it.

This document is the inventory. The machine-checked version of it is
`tests/architecture/test_srv07_json_authority_scope.py`, which reads
`docs/AUTHORITY_MAP.md`'s JSON and JSONL sections and requires every store
named there to carry a signed verdict — `excluded` (out of SRV-07's scope,
with a reason) or `owned` (a specific task, existing or new, is responsible).
A store with neither is exactly the shape of gap this task exists to close:
present, live, and silently uncounted. Document describes, test enforces —
the same relationship `docs/srv01b-schema-map.md` has with
`tests/db/test_schema_correspondence.py`.

## Why "importer, not schema designer" is the dividing line

SRV-07's target is the accepted 33-table PostgreSQL schema
(`docs/postgres-foundation.md`, `docs/srv01b-schema-map.md`): it converts and
loads rows against tables that already exist. A JSON/JSONL store with no
matching table in that schema has nowhere for an *importer* to write —
moving it to PostgreSQL first needs a schema-design decision, which is
different work from SRV-07's own charter. That split — not "is this data
important" — is what separates `excluded` from `owned` below. Importance
decides whether a schema-design task is worth opening; it does not put the
store inside SRV-07 itself.

## The inventory

| Store | Verdict | Task | Why |
| --- | --- | --- | --- |
| `data/execution_queue.json` | **owned** (done) | `VOYN-W0-AICC-QUEUE-ENTRY-PARITY` | The trigger case: JSON stays authoritative by design, mirrored into `queue_entry` via its own contract, not SRV-07's convert-and-load shape. |
| `data/runs.jsonl` | excluded | — | Frozen: nothing writes it; it is the one-way read source `runtime/legacy_import.py` already folds into `run`, a table the shared mirror contract covers. No live JSON authority left to import separately. |
| `data/tasks.json` | **owned** (new) | `VOYN-W0-AICC-TASKBOARD-PG-MIGRATION` | Kanban/product board, fleet-visible, actively written. No matching table exists; needs schema design before any import. |
| `data/portfolio_launches.json` | **owned** (new) | `VOYN-W0-AICC-PORTFOLIO-LAUNCH-PG-MIGRATION` | Launch/provisioning history, fleet-relevant, active writer, no matching table. |
| `data/activity.jsonl` | **owned** (new) | `VOYN-W0-AICC-ACTIVITY-LOG-PG-MIGRATION` | Operator-visible activity feed, audit-adjacent, no matching table. |
| `data/chats.json` | excluded | — | `docs/AUTHORITY_MAP.md`'s own verdict: "Non-critical; loss is acceptable by design." No durability motivation to justify a schema-design task. |
| `data/pipeline_settings.json` | excluded | — | Single-machine operator config by design (ADR 0003 reserves execution-state storage for something else); fail-closed local-file semantics are the point. No table targets it. |
| `data/dispatch_policy.json` | excluded | — | As `pipeline_settings.json`: "Policy only — never execution truth" per its own docstring. |
| `data/project_config.json` | excluded | — | This machine's own repository filesystem paths; meaningless as a shared row without a host key the design has no use for. Host-local by construction. |
| `data/portfolio_config.json` | excluded | — | As `project_config.json` — its own docstring says it mirrors that convention exactly. (Previously missing from `docs/AUTHORITY_MAP.md`; added alongside this inventory.) |
| `data/integration_registry.json` | excluded | — | "Operator configuration, never execution truth" per its own docstring; gitignored, machine-local repository paths and `gh` remotes. |

Eleven stores, eleven verdicts: one already delivered, three new tasks opened,
seven excluded with a reason. `test_the_domain_table_total_is_reported_not_left_to_a_silent_subtraction`
in `tests/db/test_mirror_coverage.py` closes the parallel gap on the SQLite
side — `queue_entry` no longer sits in the same exclusion registry as tables
that never had an SQLite source at all, and the gate now states the covered
total (32 shared-contract + 1 own-contract = 33) as an assertion instead of
leaving it as an implicit fact about two dict sizes.

## What this inventory does not cover, and why

`docs/AUTHORITY_MAP.md`'s own "Other" section already gives a verdict to
`data/runs.db` (orphan, tracked for removal), `data/_founder_reset_backup/`
and `data/backups/` (operator-made snapshots, never read by product code),
`data/daily-audit.*.log`/`data/audits/` (disposable diagnostics), and
`data/task_pipeline.lock` (content-free advisory lock) — none of these are
authorities, so `tests/architecture/test_srv07_json_authority_scope.py`
deliberately scans only the JSON and JSONL sections, not "Other."

A few more JSON/JSONL files exist outside `data/` entirely, so they are
outside `docs/AUTHORITY_MAP.md`'s own enforced domain (and this inventory's):
`command_center/design/tokens.json` is a packaged design-system asset, not
runtime data; `~/.aicc-self-deploy-provenance.jsonl` (`deployment/self_deploy.py`)
and the `aicc-task-workspace.json` marker (`workspace_provisioning.py`) are
host-local operational artifacts of the machine running the deploy or holding
the workspace, not fleet state; and `runtime/maintenance.py`'s
`run-events-*.jsonl.gz` archives are derived backup exports of `run_event`,
already itself a mirrored, covered table — a copy of covered data, not a
second authority.

## Reconciliation for the three new tasks (not performed here)

Same shape as `docs/srv01b-schema-map.md`'s own reconciliation section: this
task inventories and opens the work, it does not do it. Each of
`VOYN-W0-AICC-TASKBOARD-PG-MIGRATION`, `VOYN-W0-AICC-PORTFOLIO-LAUNCH-PG-MIGRATION`,
and `VOYN-W0-AICC-ACTIVITY-LOG-PG-MIGRATION` needs, before any row moves:

1. a schema for the target table(s), reviewed the way `0001_initial.up.sql`
   was;
2. a decision on write path — dual-write mirror like `PostgresTableMirror`,
   or a bespoke contract like `queue_store.py`, depending on whether the
   store's own update shape (whole-file replace, in-place row update,
   append-only) fits the shared contract;
3. the same reconciliation discipline `docs/srv01b-schema-map.md` lays out for
   the SQLite side — row counts, key sets, no dangling references, a sampled
   column comparison — before any cutover.
