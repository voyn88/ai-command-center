# VOYN-W0-AICC-SRV-09-READ-POOL — the premise does not hold against this tree

Snapshot: 2026-09-05, against `main` tip `d3aec4c` (branch
`backlog/VOYN-W0-AICC-SRV-09-READ-POOL`). Written instead of a code change,
after investigation showed the requested change would violate a tested
invariant this codebase deliberately enforces. This is the same shape of
problem `SCHEMA_VERSION_DRIFT.md` documents for a different backlog item: a
claim reached the backlog (per the item's own status line, via the
2026-08-20 `backlog_triage` pass, see `VOYN-W0-BACKLOG-RECONCILE-ALL`) that
does not match the state of the code it is about.

## The claim

> Провести `command_center/db/pool.py` на PG-путь чтения: сейчас
> `command_center/runtime/` его не импортирует. Не новая сборка, а проводка
> существующего.

Translated: wire the existing PostgreSQL connection pool onto "the PG read
path" — `command_center/runtime/` doesn't import it today — and this is
plumbing, not new construction. A precondition is cited: without the pool the
read/no-pool cost ratio is ~19.8x, and an "S8" threshold fails on "all four
measured queries" independent of correctness.

## What's actually true, and what isn't

`command_center/runtime/` not importing `command_center.db.pool` is correct
— confirmed by grep, corroborated independently by a second search pass. But
the reason is not "an existing PG read call forgot to route through the
pool." It's that **`command_center/runtime/` contains no PostgreSQL read of
any kind**. Every read in that package goes through `sqlite3` /
`command_center.runtime.db.connect()` (see `runtime/db/core.py`). The only
things imported from `command_center.db.*` inside `runtime/db/*.py` are the
`PostgresXMirror` classes' `upsert()` — one-way, best-effort, silent-on-failure
dual writes (e.g. `runtime/db/wave1.py:_mirror_advisor_proposal`), never a
mirror read. Nothing under `runtime/` calls `list_records()`,
`divergence_against()`, or any other read-side mirror method.

This is not an oversight; it is the currently-accepted architecture, and it
is enforced by tests that fail deliberately if the wiring this ticket asks
for is introduced:

- `tests/db/test_queue_store.py::test_the_read_path_reads_the_authority_and_no_mirror`
  inspects the source of `execution_queue.load_queue` and asserts it contains
  none of `mirror`, `list_entries`, `runtime_db`, `postgres`, `queue_store`.
- `tests/db/test_owner_item_store.py::test_sqlite_remains_the_authority_for_owner_items`
  does the same for `wave1.create_owner_item` / `get_owner_item` /
  `list_owner_items` against `postgres`, `owner_item_store`, `list_records`.

Both tests state the reason inline: *"Read paths are switched only after
reconciliation and the rollback and backup/restore drills — not as a side
effect of a mirror landing."* `command_center/db/digest_item_store.py` states
the same policy for its table: *"SQLite is the authority, this is a
dual-write, reads are not switched, and the cutover waits on reconciliation
plus the rollback and backup/restore drills."* `docs/postgres-foundation.md`
and `docs/operations/SCHEMA_VERSION_DRIFT.md` agree: the SQLite and
PostgreSQL schemas are two independently-versioned stores by design, "until
`VOYN-W0-AICC-SRV-01b` moves it onto this seam."

Where PostgreSQL reads *do* exist in this repo — `PostgresTableMirror.
list_records()` (`command_center/db/table_mirror.py`), used by reconciliation
tests and `scripts/mirror_slice_checks.py` — they already route through
`command_center.db.pool.connection()` via `_connection()`. There is no
bypass to fix there either.

The cited numbers — "~19.8x", threshold "S8", "four measured queries" — do
not appear anywhere in this repository: not in code, tests, docs, or
scripts. They cannot be checked against this tree; whatever produced them is
external to this checkout.

## Why this isn't a wiring fix

The ticket frames itself as plumbing ("Не новая сборка, а проводка
существующего" — not new construction, just wiring up what already exists).
That framing is what fails here: there is no existing PG read call under
`runtime/` to route through the pool. Making `runtime/` import
`command_center.db.pool` on a read path requires *first writing* a new
PostgreSQL read into a runtime read function — which is precisely the
SRV-01b read cutover, gated (by the two tests above, and by the stated
policy in every mirror store's docstring) on reconciliation plus rollback and
backup/restore drills having happened first. Those drills are a separate,
already-tracked, deliberately larger piece of work, not a side effect of
adding an import.

## Recommendation

Do not force this import to close the ticket. Either:

1. Re-scope the item to what it can honestly be today — auditing that every
   *existing* PostgreSQL read (the mirror/reconciliation surface) already
   uses the pool, which is already true and needs no change — or
2. Fold it into the tracked SRV-01b read-cutover work once reconciliation and
   the rollback/backup/restore drills referenced by the guard tests have
   actually run, at which point wiring `runtime/`'s (now-new) read calls to
   the pool is a real, small step inside that larger change.

No source changes accompany this note. The two guard tests above were run
unmodified to confirm current behavior; they were not touched.
