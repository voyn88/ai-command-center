# ADR-0011: the backlog markdown bridge is bidirectional only for the migration window

Status: accepted for `VOYN-W0-BACKLOG-ORCHESTRATOR` (BO-S4).

## Context

The machine invariant behind the whole backlog-orchestrator task is that
Markdown is a **projection** and the PostgreSQL store (`backlog_task` et al,
BO-S1) is **canonical**. Two independent writers currently cross that
boundary in opposite directions, and both are legitimate only because the
migration is not finished:

- **Import** (`ops/aicc_backlog_publish.py` + `backlog-import`, BO-S1):
  the owner still authors `VOYN_TASKS_BACKLOG.md` by hand on their own
  machine; a launchd job pushes it to the control host every 5 minutes and
  imports it into the store. This is how new tasks and hand edits enter the
  system today — the planner, executors and console all read the store, not
  the file, but the store still has no other writer for owner-authored
  content.
- **Export** (`command_center/db/backlog_export.py` + `backlog-export`,
  BO-S4, scheduled by `deploy/systemd/aicc-backlog-export.timer`): renders
  `backlog_task` back into the `backlog_client.parse_recommendations`
  record shape (`- VOYN_RECOMMENDATION | key=value | ...`), so the console's
  Master Backlog panel and any other reader of that format see live store
  state instead of freezing at the last hand-authored snapshot (measured
  live 2026-09-03: a freshly booted console rendered a file two weeks
  stale). This is a *different* line shape from the bold task lines
  (`- **ID** | ...`) `backlog-import`'s parser (`backlog_parser.parse_backlog`)
  recognizes — see below.

Running both directions at once would be a dual-write hazard if they ever
shared a line shape or a file path; they do neither. Feeding a
`backlog-export` render back through `backlog-import` is inert, but not
because the two jobs agree on field values: `parse_backlog` only matches
bold task lines, and a rendered `VOYN_RECOMMENDATION` line does not match
that shape at all — it is invisible to the importer, not merely unparsed
(`tests/db/test_backlog_export.py::test_reimporting_a_projection_through_the_real_importer_is_a_no_op`
proves both `tasks == []` and `unparsed == []` for a re-parsed export). In
production the two also never touch the same file: import reads a
digest-staged copy of the owner's own machine's file
(`ops/aicc_backlog_publish.py`), never `$AICC_MASTER_BACKLOG`, which only
`backlog-export` writes. `backlog_export` still renders unmapped narrative
fields (`effect`/`effort`/`acceptance`/`evidence`/`file_scope`/
`parallel_domain`) as `-` rather than inventing prose — that is about the
projection itself not fabricating content for the console, and is a
property worth keeping regardless, but it is not what makes a re-import
safe. The bridge is safe only as long as the owner treats their own file as
*input* and the rendered `$AICC_MASTER_BACKLOG` as *output*, and never
edits the generated file directly or repoints `backlog-import` at it.

## Decision

Both directions stay live for the migration window, and neither is allowed
to become permanent by default:

- Import remains the only path for the owner to author new tasks or edit
  existing ones, until task authoring moves into a store-backed surface
  (the console's own writers, or voice/`S6`) that no longer needs a
  hand-edited file at all.
- Export remains the only path that keeps markdown readers current, and is
  the justification for import's continued existence — without it, killing
  import would blind every markdown reader immediately.

**Revisit condition:** once `backlog-export`'s rendered file has been the
*only* file the owner opens (i.e. zero direct edits to a copy that did not
originate from an export tick) for two consecutive weeks, `backlog-import`,
`ops/aicc_backlog_publish.py` and the launchd job that drives it are
deleted outright — not deprecated, not feature-flagged — and this ADR is
superseded to record single-direction (export-only) projection as final.

**Target date:** 2026-11-01. If the revisit condition has not been met by
then, an owner decision is required (extend with a new explicit date, or
replace hand-editing with a store-backed authoring surface) rather than
letting the bridge continue silently past its window.

## Rejected alternatives

- **Export only, freeze import immediately:** would strand the owner's
  current authoring workflow (a markdown file on their own machine) with no
  replacement in place, before BO-S2/S2a's dispatch or a voice/console
  authoring surface reaches parity.
- **Merge on conflict instead of import-wins:** `backlog-import` already
  treats duplicate ids as first-occurrence-wins and reports the rest; a
  merge policy across two writers on the same fields would need the store
  to track per-field provenance, which does not exist and is out of scope
  for closing this migration.
- **No explicit date, condition only:** matches ADR 0007's step-4 gate
  ("a session with no divergence"), but a bridge with no calendar backstop
  has no forcing function if the condition is simply never checked — hence
  both a condition and a date here.
