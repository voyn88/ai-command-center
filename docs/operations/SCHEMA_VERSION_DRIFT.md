# Three "schema versions" — what each number actually is

Snapshot: 2026-08-27, against `backlog/VOYN-W0-AICC-SCHEMA-VERSION-DRIFT`
(base `67a996b`). Written for VOYN-W0-AICC-SCHEMA-VERSION-DRIFT after the
2026-08-20 backlog triage (`VOYN-W0-BACKLOG-RECONCILE-ALL`) flagged three
numbers — 16, 23, 14 — as if they were three readings of one drifting value.
They are not. Two belong to two different databases that are *supposed* to
version independently right now, and the third is not a live number at all.

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

## Takeaways for whoever reads this next

1. `runtime.db`'s `SCHEMA_VERSION` and the PostgreSQL `aicc` schema's
   `EXPECTED_SCHEMA_VERSION` are two independent counters by design (per
   `docs/postgres-foundation.md`) until `VOYN-W0-AICC-SRV-01b` cuts the
   runtime store over. Never diff them against each other.
2. "16" is not, and has never been, a `runtime.db` migration state. It is the
   retired early-survey table count from the SRV-01b correspondence map. If it
   resurfaces in a future backlog item, close it against this document rather
   than re-investigating.
3. Any prose citation of either `SCHEMA_VERSION` or `EXPECTED_SCHEMA_VERSION`
   is a snapshot the moment it is written and should be treated as stale by
   default — query the live source (`schema.py`, `health.py`, or
   `python -m command_center.db status`) before trusting a number in a
   document, including this one.
