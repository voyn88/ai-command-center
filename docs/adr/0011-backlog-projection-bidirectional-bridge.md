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

**Known gap, not a shape problem:** the rendered record's `status` field
must carry the *planning* vocabulary (`AI-Reco`/`PO-Review`/`PO-Approved`)
`backlog_client.BacklogRecommendation.is_approved` checks by exact literal
match — not `backlog_task.status`'s *execution* vocabulary
(`OPEN`/`IN_PROGRESS`/...) — or `is_approved` and everything built on it
(`execution_queue`, the panel's "Approved" metric) reads as permanently
empty for an export-generated file. `backlog_export._planning_status`
translates one to the other (`EXECUTABLE_STATUSES` → `PO-Approved`,
everything else → `PO-Review`), at the cost of losing execution-status
granularity in this field. That granularity lives on the master file's *other* record
surface, the one `backlog_client.parse_rich_records` reads (consumed by
`native_gateway/projection_producer.py` for Kanban lanes and the wave-goal
card).

**Gap closed 2026-09-21 by option (a) below: section `0C. Execution
status`.** The safety analysis of 2026-09-05 stands exactly as written —
`backlog_client._RICH_LINE` (`- **VOYN-<id>** | <wave> | <status> |
<priority> |`) is a strict subset of `backlog_parser`'s
`_TASK_LINE`/`_RECORD_SHAPED` match (bold `**VOYN-...**` id followed by
`| `), so any line the rich-record reader accepts the importer accepts too,
and parses fully as a real task rather than as a reported `unparsed` line.
The two readers were built to share that one convention on purpose, so no
variant of the bold-id/pipe shape can satisfy one and not the other. What
changed is that the exporter no longer tries to use that shape. It renders
the surface under its own marker instead — `- VOYN_TASK_STATUS | id=... |
wave=... | status=... | priority=... | slug=...`, a plain *unbolded* list
item matching neither importer pattern, the same move `VOYN_RECOMMENDATION`
already made for 0B records. The line is invisible to `backlog-import`, not
merely rejected by it, so this ADR's "never share a line shape" argument
below holds unchanged and is now proved over both rendered sections by the
same re-import test. `parse_rich_records` reads the new shape alongside the
hand-authored bold one, and for the migration window where a task could
carry both, **the machine record wins**: `backlog_task` is canonical and a
`VOYN_TASK_STATUS` line is a direct reading of it, while a bold line is
owner-typed input the store may already have moved past — preferring the
authored line would let a stale hand edit mask live execution state, which
is the staleness this ADR's export half exists to end. Option (b),
outliving the revisit date, is no longer the cheaper path because it is no
longer needed; the bold-line shape can still be adopted directly once
`backlog-import` retires, but nothing depends on that happening.

Two consequences worth recording, both caught by widening the vocabulary
rather than by the shape work:

- `backlog_client.RICH_STATUSES` had drifted from the store's own
  `backlog_parser.STATUSES` (it omitted `DECIDED`). Harmless while the
  surface was hand-authored — a human typing `DECIDED` got `UNKNOWN` and
  noticed — but once a rendered record carries a column value, a divergence
  is a status the store holds and every reader silently mislabels. The two
  sets are now pinned equal by test.
- `projection_producer._RICH_STATE` subscripts rather than `.get`s, so the
  newly reachable `DECIDED` would have been a `KeyError` taking down the
  whole projection build. It has a lane, and a test keeps the map total over
  the reader's vocabulary.

Running both directions at once would be a dual-write hazard if they ever
shared a line shape or a file path; they do neither. Feeding a
`backlog-export` render back through `backlog-import` is inert, but not
because the two jobs agree on field values: `parse_backlog` only matches
bold task lines, and neither rendered shape — `- VOYN_RECOMMENDATION | ...`
(0B) nor `- VOYN_TASK_STATUS | ...` (0C) — matches it at all: both are
invisible to the importer, not merely unparsed
(`tests/db/test_backlog_export.py::test_reimporting_a_projection_through_the_real_importer_is_a_no_op`
proves both `tasks == []` and `unparsed == []` for a whole re-parsed export,
covering both sections). In
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

Half of that precondition is now enforced rather than merely asked for.
`backlog-import` refuses any file whose header carries
`backlog_export.GENERATED_MARKER` (`is_generated_projection`), because
"inert" is not the same as "safe to do": aimed at a rendering, the import
would have *succeeded* — parsing zero tasks, printing
`inserted 0, updated 0, unchanged 0`, exiting 0 — and
`ops/aicc_backlog_publish.py`, which only inspects the exit code, would have
reported a healthy publish every five minutes while nothing the owner typed
reached the store. A repointed path now fails loudly on the first tick. The
other half — the owner editing the generated file in place — stays a
convention, but is no longer entirely undetectable. The rendered header says
"do not edit" at the top of the file, and stamps when it was rendered and from
how many store rows; both halves of that stamp are now read by machine
(`backlog_client.parse_generated_stamp` → `Projection.stamp`), not just by a
human scrolling past:

- The render time is what the console's freshness metric shows, replacing the
  file's `mtime`. That substitution is the point: `mtime` answers "when did
  *this host* last write these bytes" and is reset to now by any `cp`, `scp`,
  checkout or container build, so a projection whose export tick died a week
  ago reads as seconds old the moment it moves — the same silent staleness
  this ADR's export half was built to end, re-entering through the freshness
  indicator itself. A stamp inside the text travels with the text. Past
  `PROJECTION_STALE_AFTER` (15 min = three missed ticks; one missed tick is
  ordinary jitter and alarming on it would train the owner to ignore the
  alarm) the panel says the tick is dead instead of showing an age.
- The row count gives the convention a partial enforcement it did not have.
  A file straight off a tick carries exactly as many record lines as its
  header claims, so a count that no longer matches means record lines were
  added or removed after the render, and the panel says the file was edited.
  This catches inserted and deleted records; it does *not* catch a field
  edited in place, which changes no count. Partial, and stated as partial —
  the convention still stands, it is simply no longer trace-free in the case
  where a hand edit changes what the panel totals.

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

**The date is enforced, not just recorded.**
`tests/test_backlog_bridge_retirement.py` parses the `**Target date:**` line
above — this ADR stays the single source of truth for it — and fails the
suite from that date onward for as long as the import side still exists
(`ops/aicc_backlog_publish.py`, its launchd job, the `backlog-import`
subcommand). It also pins that every file quoting the date quotes *this*
one. Without that check the date had the same defect as the alternative
rejected below: nothing would have looked at it. The test stops firing on
its own once the import side is deleted, and is deleted in that same
commit; extending the window means moving the date here, which is exactly
the explicit owner decision this section asks for.

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
