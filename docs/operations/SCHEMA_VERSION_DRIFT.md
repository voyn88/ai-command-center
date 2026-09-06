# Three "schema versions" — what each number actually is

Snapshot: 2026-09-02, against `main` tip (`c9d2e23`). Written for
VOYN-W0-AICC-SCHEMA-VERSION-DRIFT-REM after the 2026-08-20 backlog triage
(`VOYN-W0-BACKLOG-RECONCILE-ALL`) flagged three numbers — 16, 23, 14 — as if
they were three readings of one drifting value, and asked separately what the
"parity gate ... at schema 16" means. Neither reading holds up as stated. Two
of the three numbers belong to two different databases that are *supposed* to
version independently right now; the third is not a live number at all; and
the parity gate has never been "removed" or "lifted" at any schema version —
it is live today, and "16" also turns up as a second, unrelated, genuinely
live number on this exact checkout. Both threads are unpacked below.

## The three numbers, resolved

| Quoted as | What it actually is | Where it lives | Current live value |
| --- | --- | --- | --- |
| "live `data/runtime.db` is on migration 16" | **Not a migration state.** The "16 domain tables" count from the *first* SRV-01b SQLite→PostgreSQL correspondence survey, taken before waves W1–W3 shipped. Both the doc and the test suite that replaced it call this number out by name as stale. | `docs/srv01b-schema-map.md` ("Число «16 доменных таблиц» из ранней съёмки **устарело**"); `tests/db/test_schema_correspondence.py` ("An early survey recorded '16 domain tables'... it was `33`, and before that a stale `16` that reached a plan") | N/A — retired |
| "code slice declares SCHEMA_VERSION=23" | Real, but a **stale snapshot** of the SQLite `runtime.db` schema version, not the live value. `docs/srv01b-schema-map.md` was machine-generated on 2026-08-13 and pinned `SCHEMA_VERSION` at 23 at that instant; it has not been regenerated since. | `command_center/runtime/db/schema.py::SCHEMA_VERSION` | **25** (see below) |
| "local checkout — 14" | Real at the time it was read, but **belongs to a different database.** It is the PostgreSQL `aicc` schema's migration count, not the SQLite one, and it too has since moved. | `command_center/db/health.py::EXPECTED_SCHEMA_VERSION = len(migrations.discover())`, counting `command_center/db/sql/0001..NNNN_*.sql` | **16** (see below) |

So there are not three versions of one schema in play. There are:

- one live, correct SQLite `runtime.db` schema version (**25**, not 23 — the
  snapshot is several migrations stale; see the table below),
- one live, correct, *unrelated* PostgreSQL `aicc` schema version (**16**,
  not 14 — also moved since the triage read it), and
- one retired literal (**16**) that never described a migration state on
  either side, only a table count from a survey superseded two waves ago —
  and which, by coincidence of timing only, now collides in value with the
  live PostgreSQL count above. They are not the same fact; see below.

## Why 16 keeps coming back

`tests/db/test_schema_correspondence.py` was written specifically because the
stale `16` "reached a plan" once already — a hand-transcribed table count from
an early draft of the SRV-01b correspondence map ended up quoted in a planning
document after the real count had already grown past it (waves W1–W3 added
digest/owner/conflict/audit/model-registry/marketplace/council/networking
tables, taking the domain-table count from 16 to 33). The fix at the time was
to stop hand-transcribing the count into prose and derive it from the running
migration instead (`_tables_declared_by_the_initial_migration()` in that test
module, and the "Итог сверки" table in `docs/srv01b-schema-map.md`, which is
explicitly documented as machine-derived, not read from source).

This backlog item is the second time the same literal has reached a plan —
this time misread as a *migration number* on the live `runtime.db` rather
than as the retired table count it actually is. Nothing about the live
`runtime.db` migration state has ever been 16; no code path, test, or doc
asserts that. Separately, and for an unrelated reason, "16" has since become
a genuine, live PostgreSQL migration count (below) — treat any future
appearance of "16" by checking which of the two it is against the tree it was
read from, not by assuming either.

## Two independent version tracks, by design

`docs/postgres-foundation.md` states the intended state directly: "The runtime
store still writes to SQLite until `VOYN-W0-AICC-SRV-01b` moves it onto this
seam; both schemas exist in parallel until then." Until that cutover, having
two independently-numbered schemas is correct, not drift:

| | SQLite `runtime.db` | PostgreSQL `aicc` |
| --- | --- | --- |
| Version source | `command_center/runtime/db/schema.py::SCHEMA_VERSION`, hand-maintained, bumped once per appended migration in `MIGRATIONS` | `command_center/db/health.py::EXPECTED_SCHEMA_VERSION = len(migrations.discover())`, derived from the file count in `command_center/db/sql/` — cannot go stale by omission |
| Applied-version ledger | `schema_version` table inside the `.db` file itself (`MAX(version)`) | `schema_migration` table in the database, checksum-verified per row |
| Deploy/migration order | **Migrate-on-open, in-process.** `command_center.runtime.db.migrate(db_path)` runs lazily, inside the application, the first time any of ~15 call sites (`runtime/supervisor.py`, `digest/service.py`, `council/service.py`, `marketplace/service.py`, `conflicts/service.py`, `api/wave1_service.py`, `api/audit_service.py`, `api/model_registry_service.py`, `networking/service.py`, `runtime/autonomy_service.py`, ...) touches that file. Every migration is additive and idempotent, so this is safe to run repeatedly and safe under concurrent first-application (`schema_version.version` is a `PRIMARY KEY`; a losing `INSERT` is treated as "already applied", not an error). There is no separate pre-deploy migration step and no readiness gate — whichever binary next opens the file brings it forward to *that binary's* compiled-in `SCHEMA_VERSION`, with no check that this matches what any other process expects. | **Migrate-then-deploy.** An operator/CI runs `AICC_PG_USER=aicc_migrator python -m command_center.db upgrade` once, explicitly, before app instances take traffic (`docs/postgres-foundation.md`). The migration and its ledger row commit atomically; a session-level advisory lock serializes concurrent runners (the normal case in a rolling deploy); an already-applied migration's checksum is re-verified on every run. `GET /readyz` then refuses traffic (`503`) unless `schema_migration` equals `EXPECTED_SCHEMA_VERSION`, so a replica running against the wrong schema is kept out of rotation instead of serving wrong answers. |

The practical asymmetry worth flagging: SQLite's migrate-on-open has no
equivalent of `/readyz`, and what happens on a version mismatch is
direction-dependent — it does not simply "converge the file to whichever
`SCHEMA_VERSION` that process was built with." `_migrate_unlocked()`
(`command_center/runtime/db/core.py`) reads the file's current
`MAX(schema_version.version)` and then applies, in order, only the entries of
its own `MIGRATIONS` list whose version is *greater* than that current value;
it never reverts or reapplies a lower one.

- A **newer** binary opening an older file forward-migrates it in place, up to
  that binary's own `SCHEMA_VERSION`. This is the ordinary case and the only
  direction in which "converges to this binary's version" is accurate.
- An **older** binary opening a file already migrated past its own
  `SCHEMA_VERSION` finds none of its `MIGRATIONS` entries greater than the
  file's current version, so it applies nothing and `current` stays exactly
  where the file already was. The very next check in `migrate()`,
  `if current > db.SCHEMA_VERSION`, is then true, and it raises
  `RuntimeError(f"runtime schema v{current} is newer than supported
  v{db.SCHEMA_VERSION}")` before the caller can proceed.

So an older process pinned to stale code does not silently pull the file
backward, and it does not silently keep running against a schema it doesn't
understand either — it fails closed, loudly, the moment it tries to open the
file. That is still weaker than the PostgreSQL side's guarantee (`/readyz`
keeps a mismatched replica out of rotation *before* it serves a single
request; SQLite's check only fires when something opens the file, and only
that one process is protected), but it is a fail-closed gap, not a
silent-drift one. Closing it further — e.g. an in-process health check keyed
off `current_schema_version()` that other call sites consult before trusting
the file — is a separate, larger change and out of scope here.

## The "16 → 23 → 25" migration path, if it's ever asked for again

For reference, the actual commits that moved `SCHEMA_VERSION` through the
range this ticket's status note touches, confirmed against each commit's
tree:

| `SCHEMA_VERSION` set to | Commit | What it added |
| --- | --- | --- |
| 16 | `235cf2e` (#267) | W1 morning-digest engine + «Мой день» auto-fill — `digest_item.day`/`position` |
| 17 | `84a93d6` (#271) | BANK/LEGAL redaction — `owner_item.project_ref`, `digest_item.project_ref` |
| 23 | `28088d1` (#281) | W3 networking feedback→task loop (`contact`/`message`/`networking_invitation`) |
| 24 | `2e74d60` (#318, VOYN-W0-AICC-SRV-09-FINALIZED-AT) | `run.finalized_at` |
| 25 | `ad5425f` (#473, VOYN-W0-AICC-SRV-09-FINALIZED-AT-REM-CANCEL-DURABILITY) | Process-identity fencing + the v25 finalization-claim cutover — current HEAD |

`docs/srv01b-schema-map.md`'s "Итог сверки" table was snapshotted right after
`28088d1` landed (2026-08-13) and has not been refreshed since; it is now
two migrations stale, which is the same lesson this document exists to state:
a hand-copied schema-version number in prose is correct for as long as nobody
merges another migration, and no longer. Prefer
`command_center.runtime.db.SCHEMA_VERSION` / `current_schema_version(db_path)`
and `command_center.db.health.EXPECTED_SCHEMA_VERSION` /
`python -m command_center.db status` as the live sources; do not re-quote a
number out of this document, `docs/srv01b-schema-map.md`, or any other prose
without re-deriving it.

## The "parity gate," and the second 16

The status note also asks what "the parity gate, снятый at schema 16" means.
That phrase names something real, distinct from the retired table count
above — but reading `снятый` as "removed" sends the search in the wrong
direction. This document's sibling, `docs/srv01b-schema-map.md`, uses the same
verb for itself in its opening line — "Снята машинно 2026-08-13" ("captured/
taken mechanically on 2026-08-13") — meaning *measured*, not *lifted*. Read
that way, the note is asking what a reading of the parity gate showed at the
moment the count was 16. Nothing in this codebase's history shows the gate
being removed, disabled, or bypassed at any schema version; it is live and
enforced right now.

**What the gate is.** `tests/db/test_schema_correspondence.py` — named "the
SRV-07 parity gate" directly in two places, `command_center/db/roles.py` and
`tests/db/test_queue_claim.py` — asserts that the live SQLite `runtime.db`
schema and the live PostgreSQL `aicc` schema stay in 1:1 correspondence
(tables, columns, primary keys, index column-sets, `UNIQUE`/`CHECK`
constraints) for every table the eventual SRV-01b cutover has to carry
across. `queue_entry` is the one declared exception — it is a
PostgreSQL-native queue mirror that migration `0002` never touches, so it
"sits outside the parity gate" by design. The gate does not read either
`SCHEMA_VERSION` out of source: it derives both schemas live, by running the
real `command_center.runtime.db.migrate()` into a throwaway SQLite file and
applying the real PostgreSQL migrations (`0001_initial.up.sql` plus the
`CORRESPONDING_MIGRATIONS` list — currently `0004_run_finalized_at.up.sql`
and `0016_run_finalization_claim.up.sql`) to a throwaway database, on every
CI run. It has no version pin of its own — it checks whatever the two
schemas happen to be when it runs.

**Where 16 turns up as a real, live number.** On this checkout,
`command_center/db/sql/` holds 16 migration files (`0001` through
`0016_run_finalization_claim`), so `EXPECTED_SCHEMA_VERSION == 16`. That file
was added in `ad5425f` (#473, VOYN-W0-AICC-SRV-09-FINALIZED-AT-REM-CANCEL-DURABILITY,
2026-08-30) — the same commit that took SQLite's `SCHEMA_VERSION` from 24 to
25 — which is why the live pair today is (25, 16): two counters that moved
together in one commit and then stayed uncorrelated, exactly the by-design
shape this document already describes. The same commit extended the parity
gate's scope to match, adding that file to `CORRESPONDING_MIGRATIONS` so
`run_finalization_claim`'s PostgreSQL target is checked for correspondence
too (it has none on the SQLite side by design — `0016`'s header notes "the
current SQLite authority does not dual-write this row" — so the table-count
assertions were widened to source from `INITIAL_MIGRATION` plus
`CORRESPONDING_MIGRATIONS` together, not `INITIAL_MIGRATION` alone). If that
is what a reading of "the gate" turned up 16 at, it is accurate, but it is
`EXPECTED_SCHEMA_VERSION` (PostgreSQL), not `SCHEMA_VERSION` (SQLite) — the
same category error as the earlier "16" fossil, just against a live number
this time instead of a retired one, and landing on the same digit for an
unrelated reason (16 migration files, versus 16 domain tables in a survey
taken years of migrations earlier). Nothing about the gate was lifted to
produce that pair; it passed, checked, on the wider table set, and continues
to pass on every CI run since.

## Takeaways for whoever reads this next

1. `runtime.db`'s `SCHEMA_VERSION` and the PostgreSQL `aicc` schema's
   `EXPECTED_SCHEMA_VERSION` are two independent counters by design (per
   `docs/postgres-foundation.md`) until `VOYN-W0-AICC-SRV-01b` cuts the
   runtime store over. Never diff them against each other.
2. "16" is not, and has never been, a `runtime.db` migration state. It is the
   retired early-survey table count from the SRV-01b correspondence map. It
   also separately became the real, live PostgreSQL `EXPECTED_SCHEMA_VERSION`
   after PR #473 — a coincidence of timing between an unrelated retired
   literal and a genuine migration count, not the same fact twice. Resolve
   any future appearance of "16" by checking which of the two it is, against
   whichever tree it was read from, rather than assuming either.
3. The SRV-07 parity gate (`tests/db/test_schema_correspondence.py`) has never
   been removed, disabled, or version-pinned at 16 or any other number. It
   re-derives both schemas live on every run and currently passes against
   whatever `SCHEMA_VERSION`/`EXPECTED_SCHEMA_VERSION` pair the checkout it
   runs in actually has.
4. SQLite's `migrate()` only ever applies migrations above the file's
   recorded version — never backward, and never past what the calling
   binary's `MIGRATIONS` list knows. A binary older than the file it opens
   fails closed with a `RuntimeError` instead of running against (or
   reverting) a schema it doesn't understand.
5. Any prose citation of either `SCHEMA_VERSION` or `EXPECTED_SCHEMA_VERSION`
   is a snapshot the moment it is written and should be treated as stale by
   default — query the live source (`schema.py`, `health.py`, or
   `python -m command_center.db status`) before trusting a number in a
   document, including this one.
