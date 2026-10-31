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

## Resolution — option 1, mechanised (2026-09-09)

Option 1 was taken, with one change to it. "Already true and needs no change"
was the finding, but it understated the risk: the property held only by
convention. Nothing in the tree checked that a PostgreSQL connection came from
the pool, so the survey above was accurate on the day it was written and had
no way to stay accurate. The cost the item is worried about is real; the way it
returns is not a store that forgot the pool, it is the *next* store, written by
copying a driver example that says `psycopg.connect(dsn)`.

`tests/architecture/pool_routing.py` and `test_pool_routing_fitness.py` turn
the convention into a gate, with three rules over the AST of `command_center/`:

1. **No unpooled connection** — no driver `connect()` call outside the one
   declared exemption.
2. **No second pool** — only `db/pool.py` may build one. `pool.open_pool()` at
   startup is explicitly *not* a violation; reaching past it to
   `adapter.open_pool()` or `psycopg_pool.ConnectionPool` is.
3. **The fallback stays the pool** — a store that offers
   `connection_factory=None` must still resolve `pool.connection()` when the
   caller omits it. Rules 1 and 2 do not cover this: a store can lose the pool
   without ever naming a driver, and every test injects a factory, so nothing
   else would fail.

The rules are keyed on what a name is *bound* to in the file, not on spelling,
which is what keeps the desktop's several hundred Qt `signal.connect(...)`
calls and `runtime/db/core.py`'s SQLite `db.connect(db_path)` out of the
results while still catching `import psycopg as pg; pg.connect(dsn)`,
`from psycopg import connect as _open`, and the `importlib.import_module`
form. `sqlite3` is out of scope by design: the rule is about PostgreSQL's
backend-per-connection cost, and the authority store has no pool to bypass.

The gate imports no driver and no `command_center.db` module, so it runs in
the serverless configuration — which, per `tests/db/mirror_discovery.py`, is
exactly where the declaration checks are the only ones still running.

Two findings from building it, both recorded in the tests rather than only
here:

- **One exemption exists and is pinned.**
  `command_center/ops/credential_rotation.py` probes a *candidate* credential
  before installing it. The pool is built from the credential currently in
  force, so routing that probe through it would test the old password and
  report the new one healthy. It is one connection per rotation, not per
  query. `test_the_only_unpooled_connection_is_the_credential_probe` pins the
  allow-list to exactly that file — including a check that removing the
  exemption makes the file a violation again, so it cannot pass by having
  quietly stopped connecting.
- **Rule 3 asks only about a default the caller can omit.** The first draft
  flagged `orchestrator/planner.py`, whose `Planner(connection_factory)` takes
  the factory as a *required* argument; its caller in `db/cli.py` passes a
  connection from `pool.connection()`. Satisfying the draft would have meant
  either giving `Planner` a pool fallback nothing calls or exempting it by
  name, both worse than narrowing the rule to the case it protects — the
  behaviour a caller gets when it says nothing. A required factory is covered
  by rules 1 and 2 at the caller, which is where the decision actually is.

What this does **not** do is move a read onto the pool, because the survey
above found none left to move, and it does not touch the SRV-01b gating. The
`runtime/` half of the item stays option 2: still blocked, still tracked
there. Verified by mutation rather than by the gate merely being green — a
`psycopg.connect()` planted in `db/work_queue_read.py`, an
`adapter.open_pool()` planted in `worker/__main__.py`, and `db/backlog_store.py`
stripped of its fallback were each caught by the rule that owns them, and the
tree restored after each. The two guard tests quoted above were re-run
unmodified and still pass; they were not touched.

## Hardening pass — the gate had holes in the shapes it was built for (2026-09-09)

The gate above was re-examined against the argument it was written from: the
cost returns through *the next store, written by copying a driver example*. On
that test it was not yet doing its job. Four holes, each found by asking what a
copied example actually looks like rather than by reading the code for style:

1. **`psycopg.Connection.connect(dsn)` was invisible to rule 1.** This is
   psycopg 3's documented explicit-class API — the form its own docs lead with
   for anything beyond the one-liner, and `AsyncConnection.connect` with it. The
   rule matched `<driver>.connect(...)` one attribute deep, so the bare function
   was caught and the class method, two deep, was not. The gate reported green
   about a rule it was only half enforcing.
2. **The whole of psycopg2's pooling API was invisible to rule 2.**
   `psycopg2.pool.SimpleConnectionPool`, `ThreadedConnectionPool` and
   `PersistentConnectionPool` are how a second pool gets built in most material
   still on the web, and none of the three names was known to the scanner —
   while `psycopg_pool.ConnectionPool`, the form nobody copies by accident, was.
3. **`import command_center.db.adapter` + `command_center.db.adapter.open_pool(...)`
   escaped rule 2**, where `from command_center.db import adapter` was caught.
   The same held for reaching `aios_db.open_pool` directly, which is the AIOS
   boundary gate's business first but should not need a second gate switched on
   to be recognised as a pool.
4. **Rule 3 failed correct code.** It knew one spelling of the pool import, so a
   store saying `import command_center.db.pool as pool`, `from
   command_center.db.pool import connection`, or `from . import pool` read as a
   store that had *lost* its fallback. That is the failure mode that gets a gate
   deleted rather than fixed: the repair it invites is rewriting a correct
   import until the complaint stops, which teaches that the gate is about
   spelling.

The fix is one change, not four patches. Every `name.attr.attr` chain is now
resolved back through the file's own imports — including `import a.b.c` binding
`a`, relative imports resolved against the file's package, aliases of aliases,
and the literal `importlib` form — to the dotted path it denotes, and the three
rules are predicates over that path (`<driver>.…​.connect`, a raw opener or a
driver pool class, `command_center.db.pool.connection`). The binding-not-
spelling property that keeps Qt's `signal.connect()` and SQLite's
`db.connect()` clean is unchanged; it is applied to the whole chain instead of
its first link. A *reference* now counts as well as a call, because
`partial(psycopg.connect, dsn)` — or a `connection_factory=` argument — hands
the driver to something that will call it later.

Scope widened at the same time, from `command_center/` to every non-test file
in the repository (397 files, all three rules clean). A rule keyed to one
package is evaded by choosing another package, and the backend cost is paid by
the PostgreSQL server, which does not know which directory the connecting
process was started from. `tests/` remains the deliberate exclusion, for the
reason already stated: the suites connect *as each role* to prove the grants,
which is the one thing a pooled connection cannot do.

One exemption was added and deliberately narrowed: `command_center/db/adapter.py`
may name `aios_db.open_pool`, because being the single place that name appears
is the entire reason that file exists. It may not construct a driver pool, and
both directions are pinned. The `credential_rotation.py` exemption is unchanged.

Verified by mutation against the real tree, comparing this scanner to the one
at `349ef11` on the same three edits: a `psycopg.Connection.connect` planted in
`db/work_queue_read.py`, a `psycopg2.pool.ThreadedConnectionPool` planted in
`worker/__main__.py`, and a dotted `command_center.db.adapter.open_pool` planted
in `scripts/mirror_slice_checks.py` were **all three missed at `349ef11`** (the
third because the file was out of scope) and are each caught now, by the rule
that owns them. In the other direction, rewriting `db/backlog_store.py`'s real
import to `import command_center.db.pool as pool` fails the old gate and passes
this one. `tests/architecture` 58 passed; the two SRV-01b guard tests were
re-run unmodified and still pass. No read was moved onto the pool — the survey
above still finds none left to move — and the `runtime/` half of the item stays
option 2, blocked on SRV-01b.

## The precondition, measured in this repository (2026-09-21)

Two things were still true after the gate landed. The gate proves every
PostgreSQL connection in the tree comes from the pool; it says nothing about
what that is worth. And the item's precondition — "~19.8x, and the threshold
fails on all four measured queries independent of correctness" — was still a
number from outside this checkout, which the section above could only report as
uncheckable. A rule whose cost lives in someone else's spreadsheet is a rule
that gets argued away the first time it is inconvenient.

`tests/db/pool_read_cost.py` (the measurement) and
`tests/db/test_pool_read_cost.py` (what pytest runs) close that. Four reads,
timed against a real PostgreSQL through the code the service actually runs,
under two connection policies that differ in one thing only:

* **pooled** — no `connection_factory` passed at all, so each store falls back
  to `pool.connection()` exactly as it does in production (rule 3 of the
  routing gate);
* **unpooled** — `psycopg.connect()` per call, built from the *same* conninfo
  the pool was built from (same role, same `statement_timeout`, same
  autocommit), closed at block exit. Not a straw man: it is the shape a store
  acquires by being written from a driver example, which is the failure mode
  the gate exists for. `tests/` is the one place in this repository allowed to
  open such a connection, which is what makes the comparison possible here.

First run, medians of 120 calls after 15 warm-up calls, 100 seeded rows per
table:

```
read                       unpooled ms  pooled ms  +ms/call   ratio
-------------------------------------------------------------------
backlog.list_tasks              14.637      0.801    13.836   18.3x
backlog.get_task                13.077      0.338    12.739   38.7x
work_queue.list_items           15.416      0.819    14.597   18.8x
queue_mirror.list_entries       14.412      0.818    13.595   17.6x
```

Three consecutive runs on the same box gave 18.3x/20.7x/22.0x
(`list_tasks`), 38.7x/43.8x/60.8x (`get_task`), 18.8x/18.9x/21.6x
(`list_items`) and 17.6x/19.7x/20.8x (`list_entries`): the run-to-run spread
is on the pooled side, whose per-call time is small enough (0.2–0.8 ms) that
scheduler noise is a visible fraction of it, and the widest spread is on the
cheapest read for that reason. The unpooled side is steady to within a
millisecond, which is the point — a connection costs what a connection costs.

Environment: PostgreSQL 16.15 in this task container, loopback TCP,
`scram-sha-256`, no TLS, `pool_min_size=2`. CI runs PostgreSQL 17.6 against the
same test. On the same box a bare `connect()` + `close()` costs 10.9 ms and a
`SELECT 1` on an established session costs 0.157 ms, which is the whole result
in two numbers: **the penalty is per connection, not per query.**

What this does and does not settle:

* The item's ratio is **corroborated in family**, not reproduced: 17.6x–38.7x
  in the run tabled above (17.6x–60.8x across the three) against a cited
  ~19.8x. Absolute milliseconds are this container's
  (its syscall overhead inflates both sides); the ratio is the part that
  travels.
* "**Independent of correctness**" is the load-bearing half, and it holds. The
  overhead is flat across the four shapes (13.6–14.6 ms/call, a 1.15x spread)
  while the ratios are not, because the ratio is `1 + overhead/query_time`.
  The *cheapest* read is punished hardest — `get_task`, a single indexed row,
  at 38.7x — so a read cutover cannot be rescued by making its queries faster,
  which is exactly what the precondition claims and why it is a precondition
  rather than an optimisation note.
* No "S8" threshold exists in this tree to fail (unchanged from the section
  above), so what the tests assert are floors — every read ≥ 3x, every
  connection ≥ 0.25 ms of overhead, overhead spread ≤ 8x — chosen far enough
  below the measurement that a slow or contended box moves the numbers without
  moving the verdict. The measured figures belong in the item and in this
  document; a test that pinned them would be pinning one machine's connect
  cost.

The tests are `serial` (`-p no:xdist`, the serial tail on shard 1): a timing
comparison sharing its box with `-n auto` workers measures the other workers.
They skip with the rest of `tests/db` when `AICC_TEST_PG_ADMIN_DSN` is unset.
To re-measure on your own hardware, point that variable at a superuser DSN and
run the table out:

```
AICC_TEST_PG_ADMIN_DSN='host=… dbname=postgres user=postgres password=…' \
  pytest tests/db/test_pool_read_cost.py -p no:xdist -s
```

Use a server configured for `scram-sha-256`, not `trust`: password
authentication is part of what a connection costs, and a trust-authenticated
loopback server understates the unpooled side.

Still unchanged: no read moved onto the pool, because there is none left to
move, and the `runtime/` half of the item remains option 2 — blocked on the
SRV-01b read cutover and its drills. What has changed is that the cost this
item was raised over is now a fact this repository can re-measure on demand,
rather than a figure it had to take on trust.
