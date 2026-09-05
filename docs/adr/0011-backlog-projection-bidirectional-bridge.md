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
  `backlog_task` back into the same markdown shape, so the console's Master
  Backlog panel and any other markdown reader see live store state instead
  of freezing at the last hand-authored snapshot (measured live 2026-09-03:
  a freshly booted console rendered a file two weeks stale).

Running both directions at once is a dual-write hazard by construction: a
render written by `backlog-export` and then re-imported by
`backlog-import` must be a no-op, or the two jobs fight over field values
the store does not hold (`backlog_export` intentionally renders unmapped
narrative fields as `-` rather than inventing prose, precisely so a
round trip cannot manufacture content). The bridge is safe only as long as
the owner treats the file as *input* and the rendered projection as
*output*, and never edits the generated file directly.

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
