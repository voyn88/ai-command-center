# "Three schema versions in play" — resolved, not drift

Snapshot: 2026-09-01, against `main` (`f799f78`). Written for
VOYN-W0-AICC-SCHEMA-VERSION-DRIFT-RETRY, whose title — "three schema versions
in play" — reads as if one schema were drifting across three readings. It
isn't. This repository has exactly **two** independently-versioned schemas by
design, and a third number that keeps recurring in triage notes is not a
schema version at all. Re-deriving each live number against this checkout
below closes the question; nothing here needed a code change.

## The numbers, live on this checkout

| Number | What it actually is | Where it lives | Live value here |
| --- | --- | --- | --- |
| SQLite `runtime.db` schema version | The desktop/server runtime store's own hand-maintained version, bumped once per appended migration. | `command_center/runtime/db/schema.py:25` (`SCHEMA_VERSION = 25`) | **25** |
| PostgreSQL `aicc` schema version | The server-side Postgres schema's migration count, derived from the migration set — cannot go stale by omission. | `command_center/db/health.py:36` (`EXPECTED_SCHEMA_VERSION = len(migrations.discover())`), counting `command_center/db/sql/0001..0016_*.sql` | **16** |
| "16 domain tables" | **Not a migration state at all.** A retired table count from the earliest SQLite→PostgreSQL correspondence survey (`VOYN-W0-AICC-SRV-01b`), taken before waves W1–W3 shipped and long superseded. Both the doc it came from and the test that replaced it call it out as stale. | `docs/srv01b-schema-map.md` ("Число «16 доменных таблиц» из ранней съёмки **устарело**"); `tests/db/test_schema_correspondence.py:15,277` | N/A — retired, current domain-table count is 33 |

The Postgres side landing on 16 migrations is a coincidence of arithmetic, not
a second sighting of the same fossil: `command_center/db/sql/0016_run_finalization_claim.up.sql`
(added by `VOYN-W0-AICC-SRV-09-FINALIZED-AT-REM-CANCEL-DURABILITY`, `ad5425f`,
PR #473) is the file that took `EXPECTED_SCHEMA_VERSION` from 14 to 16 — the
same commit that took SQLite's `SCHEMA_VERSION` from 24 to 25. Two counters
moved together in one commit and landed on visually similar-looking values;
they still describe two different databases and are not comparable to each
other.

## Two independent version tracks, by design

`docs/postgres-foundation.md` states the intended state directly: "The
runtime store still writes to SQLite until `VOYN-W0-AICC-SRV-01b` moves it
onto this seam; both schemas exist in parallel until then." Until that
cutover, having two independently-numbered schemas is correct, not drift:

| | SQLite `runtime.db` | PostgreSQL `aicc` |
| --- | --- | --- |
| Version source | `command_center/runtime/db/schema.py::SCHEMA_VERSION`, hand-maintained | `command_center/db/health.py::EXPECTED_SCHEMA_VERSION`, derived from the file count in `command_center/db/sql/` |
| Applied-version ledger | `schema_version` table inside the `.db` file itself (`MAX(version)`) | `schema_migration` table, checksum-verified per row |
| Deploy/migration order | Migrate-on-open, in-process: `command_center.runtime.db.migrate(db_path)` runs lazily the first time any of ~15 call sites (`runtime/supervisor.py`, `digest/service.py`, `council/service.py`, `marketplace/service.py`, `conflicts/service.py`, `api/wave1_service.py`, `api/audit_service.py`, `api/model_registry_service.py`, `networking/service.py`, `runtime/autonomy_service.py`, ...) touches the file. No separate pre-deploy step, no readiness gate. | Migrate-then-deploy: an operator/CI runs `AICC_PG_USER=aicc_migrator python -m command_center.db upgrade` once, before app instances take traffic. `GET /readyz` (`command_center/webapi/app.py:134-141` → `command_center/db/health.py:57-88`) then refuses traffic (503) unless the live `schema_migration` ledger equals `EXPECTED_SCHEMA_VERSION`, keeping a mis-versioned replica out of rotation. |

The asymmetry is real and worth naming for whoever next touches this: SQLite's
migrate-on-open has no equivalent of `/readyz` — nothing detects a long-lived
process still running against a compiled-in `SCHEMA_VERSION` older or newer
than the file's current state, the way the Postgres probe does. That is a
genuine gap relative to the Postgres side's guarantees. It is also a separate,
larger change (an in-process staleness check keyed off the already-exported
`command_center.runtime.db.core.current_schema_version(db_path)`) than what
this ticket's title describes, and stays out of scope here: the ticket asked
whether three numbers meant one schema was drifting, not whether the SQLite
side should grow a readiness gate. If that gap needs closing, it belongs in
its own ticket against `runtime/supervisor.py`'s reconcile loop, not folded
into this one under a different name.

## The parity gate

`tests/db/test_schema_correspondence.py` — named "the SRV-07 parity gate" in
`command_center/db/roles.py:410-411` and `tests/db/test_queue_claim.py:1796` —
asserts that the live SQLite `runtime.db` schema and the live PostgreSQL
`aicc` schema stay in 1:1 correspondence (tables, columns, primary keys,
index column-sets, `UNIQUE`/`CHECK` constraints) for every table the eventual
SRV-01b cutover has to carry across. `queue_entry` is the one declared
exception — a PostgreSQL-native queue mirror that migration `0002` never
touches, so it sits outside the gate by design. The gate has never been
removed, disabled, or version-pinned at any number; it re-derives both
schemas live, by running the real migration code into throwaway databases, on
every CI run, and currently passes against the checkout's actual (25, 16)
pair.

## Takeaways for whoever reads this next

1. `runtime.db`'s `SCHEMA_VERSION` and the PostgreSQL `aicc` schema's
   `EXPECTED_SCHEMA_VERSION` are two independent counters by design until
   `VOYN-W0-AICC-SRV-01b` cuts the runtime store over. Never diff them
   against each other, and never read a similarity between their current
   values as either drift or correspondence.
2. "16 domain tables" is not, and has never been, a `runtime.db` migration
   state. It is the retired early-survey table count from the SRV-01b
   correspondence map (superseded — the live domain-table count is 33).
3. Any prose citation of either `SCHEMA_VERSION` or `EXPECTED_SCHEMA_VERSION`
   is a snapshot the moment it is written and goes stale the next time either
   side gains a migration. Query the live source
   (`command_center/runtime/db/schema.py`, `command_center/db/health.py`, or
   `python -m command_center.db status`) before trusting a number in a
   document, including this one — do not re-quote 25 or 16 without
   re-deriving them.
4. The known gap — SQLite has no runtime equivalent of the Postgres
   `/readyz` schema-mismatch check — is real but is future work for a
   separate ticket, not something this one's title asked to fix.
