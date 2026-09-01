# Three "schema versions" — what each number actually is

Snapshot: 2026-08-27, updated 2026-09-01, against
`backlog/VOYN-W0-AICC-SCHEMA-VERSION-DRIFT` (base `67a996b`). Written for
VOYN-W0-AICC-SCHEMA-VERSION-DRIFT after the 2026-08-20 backlog triage
(`VOYN-W0-BACKLOG-RECONCILE-ALL`) flagged three numbers — 16, 23, 14 — as if
they were three readings of one drifting value, and asked separately what the
"parity gate ... at schema 16" means. Neither reading holds up as stated. Two
of the three numbers belong to two different databases that are *supposed* to
version independently right now; the third is not a live number at all; and
the parity gate has never been "removed" or "lifted" at any schema version —
it is live today, and "16" also turns up as a second, unrelated, genuinely
live number once main (not this branch) is checked. Both threads are
unpacked below.

## The three numbers, resolved

| Quoted as | What it actually is | Where it lives | Current live value |
| --- | --- | --- | --- |
| "live `data/runtime.db` is on migration 16" | **Not a migration state.** The "16 domain tables" count from the *first* SRV-01b SQLite→PostgreSQL correspondence survey, taken before waves W1–W3 shipped. Both the doc and the test suite that replaced it call this number out by name as stale. | `docs/srv01b-schema-map.md` ("Число «16 доменных таблиц» из ранней съёмки **устарело**"); `tests/db/test_schema_correspondence.py` ("An early survey recorded '16 domain tables'... it was `33`, and before that a stale `16` that reached a plan") | N/A — retired |
| "code slice declares SCHEMA_VERSION=23" | Real, but a **stale snapshot** of the SQLite `runtime.db` schema version, not the live value. `docs/srv01b-schema-map.md` was machine-generated on 2026-08-13 and pinned `SCHEMA_VERSION` at 23 at that instant. | `command_center/runtime/db/schema.py::SCHEMA_VERSION` | **24** (see below) |
| "local checkout — 14" | Real and current, but **belongs to a different database.** It is the PostgreSQL `aicc` schema's migration count, not the SQLite one. | `command_center/db/health.py::EXPECTED_SCHEMA_VERSION = len(migrations.discover())`, counting `command_center/db/sql/0001..0014_*.sql` | **14** |

So there are not three versions of one schema in play. There are:

- one live, correct SQLite `runtime.db` schema version (**24**, not 23 — the
  snapshot is one migration stale; see below),
- one live, correct, *unrelated* PostgreSQL `aicc` schema version (**14**),
  and
- one retired literal (**16**) that never described a migration state on
  either side, only a table count from a survey superseded two waves ago.

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
`runtime.db` migration state is at 16; no code path, test, or doc asserts
that. Treat any future appearance of "16" in this context as the same fossil,
not as new evidence.

## Two independent version tracks, by design

`docs/postgres-foundation.md` states the intended state directly: "The runtime
store still writes to SQLite until `VOYN-W0-AICC-SRV-01b` moves it onto this
seam; both schemas exist in parallel until then." Until that cutover, having
two independently-numbered schemas is correct, not drift:

| | SQLite `runtime.db` | PostgreSQL `aicc` |
| --- | --- | --- |
| Version source | `command_center/runtime/db/schema.py::SCHEMA_VERSION`, hand-maintained, bumped once per appended migration in `MIGRATIONS` | `command_center/db/health.py::EXPECTED_SCHEMA_VERSION = len(migrations.discover())`, derived from the file count in `command_center/db/sql/` — cannot go stale by omission |
| Applied-version ledger | `schema_version` table inside the `.db` file itself (`MAX(version)`) | `schema_migration` table in the database, checksum-verified per row |
| **Deploy/migration order** | **Migrate-on-open, in-process.** `command_center.runtime.db.migrate(db_path)` runs lazily, inside the application, the first time any of ~15 call sites (`runtime/supervisor.py`, `digest/service.py`, `council/service.py`, `marketplace/service.py`, `conflicts/service.py`, `api/wave1_service.py`, `api/audit_service.py`, `api/model_registry_service.py`, `networking/service.py`, `runtime/autonomy_service.py`, ...) touches that file. Every migration is additive and idempotent, so this is safe to run repeatedly and safe under concurrent first-application (`schema_version.version` is a `PRIMARY KEY`; a losing `INSERT` is treated as "already applied", not an error). There is **no separate pre-deploy migration step and no readiness gate** — whichever binary next opens the file brings it to *that binary's* compiled-in `SCHEMA_VERSION`, with no check that this matches what any other process expects. |
| **Deploy/migration order** | **Migrate-then-deploy.** An operator/CI runs `AICC_PG_USER=aicc_migrator python -m command_center.db upgrade` once, explicitly, before app instances take traffic (`docs/postgres-foundation.md`). The migration and its ledger row commit atomically; a session-level advisory lock serializes concurrent runners (the normal case in a rolling deploy); an already-applied migration's checksum is re-verified on every run. `GET /readyz` then refuses traffic (`503`) unless `schema_migration` equals `EXPECTED_SCHEMA_VERSION`, so a replica running against the wrong schema is kept out of rotation instead of serving wrong answers. |

The practical asymmetry worth flagging: SQLite's migrate-on-open has no
equivalent of `/readyz`. If a long-lived process were pinned to older code
while the file on disk (or a sibling process) advanced further, nothing today
would surface that mismatch — it would simply mean that process is still
declaring an older `SCHEMA_VERSION`, and the next call to `migrate()` from any
process (old or new) converges the file to whichever `SCHEMA_VERSION` that
specific process was built with. That is a real gap relative to the Postgres
side's guarantees, but closing it is a separate, larger change (an in-process
health check keyed off `current_schema_version()`) and out of scope here.

## The "16 → 23 → 24" migration path, if it's ever asked for again

For reference, the actual commits that moved `SCHEMA_VERSION` through the
range this ticket's status note touches, confirmed against each commit's
tree:

| `SCHEMA_VERSION` set to | Commit | What it added |
| --- | --- | --- |
| 16 | `235cf2e` (#267) | W1 morning-digest engine + «Мой день» auto-fill — `digest_item.day`/`position` |
| 17 | `84a93d6` (#271) | BANK/LEGAL redaction — `owner_item.project_ref`, `digest_item.project_ref` |
| 23 | `28088d1` (#281) | W3 networking feedback→task loop (`contact`/`message`/`networking_invitation`) |
| 24 | `2e74d60` (#318, VOYN-W0-AICC-SRV-09-FINALIZED-AT) | `run.finalized_at` — current HEAD |

`docs/srv01b-schema-map.md`'s "Итог сверки" table was snapshotted right after
28088d1 landed (2026-08-13) and has not been refreshed since 2e74d60 — it is
itself one migration stale, which is the same lesson this document exists to
state: a hand-copied schema-version number in prose is correct for as long as
nobody merges another migration, and no longer. Prefer
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
SRV-07 parity gate" directly in two places, `command_center/db/roles.py:401`
and `tests/db/test_queue_claim.py:1796` — asserts that the live SQLite
`runtime.db` schema and the live PostgreSQL `aicc` schema stay in 1:1
correspondence (tables, columns, primary keys, index column-sets, `UNIQUE`/
`CHECK` constraints) for every table the eventual SRV-01b cutover has to
carry across. `queue_entry` is the one declared exception — it is a
PostgreSQL-native queue mirror that migration `0002` never touches, so it
"sits outside the parity gate" by design (`test_queue_claim.py:1791-1798`).
The gate does not read either `SCHEMA_VERSION` out of source: it derives both
schemas live, by running the real `command_center.runtime.db.migrate()` into
a throwaway SQLite file and applying the real PostgreSQL migrations to a
throwaway database, on every CI run. It has no version pin of its own — it
checks whatever the two schemas happen to be when it runs.

**Where 16 turns up as a real, live number.** Not in this branch's checkout —
here, `command_center/db/sql/` holds 14 migrations and `EXPECTED_SCHEMA_VERSION
== 14`, as stated above. But on `main`'s current tip, ahead of this branch's
base (`67a996b`), it is real: `VOYN-W0-AICC-SRV-09-FINALIZED-AT-REM-CANCEL-DURABILITY`
(`ad5425f`, PR #473, 2026-08-30) added `command_center/db/sql/0016_run_finalization_claim.up.sql`,
bringing the PostgreSQL side to 16 migration files — the first point in this
codebase's history where 16 is a genuine, machine-derived migration count
rather than a hand-transcribed table survey. The same commit extended the
parity gate's scope to match, adding that file to `CORRESPONDING_MIGRATIONS`
in `tests/db/test_schema_correspondence.py` so `run_finalization_claim`'s
PostgreSQL target is checked for correspondence too (it has none on the
SQLite side by design — `0016_run_finalization_claim.up.sql`'s header notes
"the current SQLite authority does not dual-write this row" — so the table
count assertions were widened to source from `INITIAL_MIGRATION` plus
`CORRESPONDING_MIGRATIONS` together, not `INITIAL_MIGRATION` alone). If that
is what a reading of "the gate" turned up 16 at, it is accurate, but:

- it is `EXPECTED_SCHEMA_VERSION` (PostgreSQL), not `SCHEMA_VERSION` (SQLite) —
  the same category error as the earlier "16" fossil, just against a live
  number this time instead of a retired one;
- it belongs to `main`, not to this branch's checkout, which is still pinned
  to the pre-#473 state (14/24);
- on that same `main` commit, SQLite's `SCHEMA_VERSION` is 25 (`fix(runtime):
  fence process identity and v25 cutover`, folded into #473) — so the live
  pair there is (25, 16), two counters that moved independently and stayed
  uncorrelated, exactly the by-design shape this document already describes.
  Nothing about the gate was lifted to produce that pair; it passed, checked,
  on the wider table set.

## Takeaways for whoever reads this next

1. `runtime.db`'s `SCHEMA_VERSION` and the PostgreSQL `aicc` schema's
   `EXPECTED_SCHEMA_VERSION` are two independent counters by design (per
   `docs/postgres-foundation.md`) until `VOYN-W0-AICC-SRV-01b` cuts the
   runtime store over. Never diff them against each other.
2. "16" is not, and has never been, a `runtime.db` migration state on this
   branch. It is the retired early-survey table count from the SRV-01b
   correspondence map. On `main`, ahead of this branch, "16" separately became
   the real, live PostgreSQL `EXPECTED_SCHEMA_VERSION` after PR #473 — a
   coincidence of timing between an unrelated retired literal and a genuine
   migration count, not the same fact twice. Resolve any future appearance of
   "16" by checking which of the two it is, against whichever tree it was
   read from, rather than assuming either.
3. The SRV-07 parity gate (`tests/db/test_schema_correspondence.py`) has never
   been removed, disabled, or version-pinned at 16 or any other number. It
   re-derives both schemas live on every run and currently passes against
   whatever `SCHEMA_VERSION`/`EXPECTED_SCHEMA_VERSION` pair the checkout it
   runs in actually has.
4. Any prose citation of either `SCHEMA_VERSION` or `EXPECTED_SCHEMA_VERSION`
   is a snapshot the moment it is written and should be treated as stale by
   default — query the live source (`schema.py`, `health.py`, or
   `python -m command_center.db status`) before trusting a number in a
   document, including this one.
