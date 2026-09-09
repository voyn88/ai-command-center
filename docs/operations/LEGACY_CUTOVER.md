# Legacy cutover: SQLite/JSON/JSONL → PostgreSQL

The one-time backfill of data written *before* the SRV-01b dual-write hooks
existed, plus the reconciliation that proves it landed and the cutover that
stops the legacy files being writable authorities. Implemented in
`command_center/db/legacy_migration.py`, driven by
`python -m command_center.db legacy-*` (VOYN-W0-AICC-SRV-07).

Read `docs/srv01b-schema-map.md` first for *what* maps to what. This document
is only the procedure.

## The three legacy sources

| Source | Authority for | How it reaches PostgreSQL |
| --- | --- | --- |
| `data/runtime.db` | the 32 mirrored tables | snapshot → each table's own mirror |
| `data/execution_queue.json` | `queue_entry` | its own frozen JSON snapshot |
| `data/runs.jsonl` | v1.2 runs not yet in the v2 store | drained into `runtime.db` **first**, then as above |

`runtime.db`'s own `queue_entry` table is *not* the queue's authority —
`queue_store.py` documents it as a best-effort mirror of the JSON file that is
allowed to fall behind — so importing it from the SQLite snapshot would migrate
a mirror's mirror. The queue is always taken from its own JSON snapshot.

`data/runs.jsonl` is the case that is easy to get wrong. It reaches PostgreSQL
only *indirectly*: `runtime/legacy_import.py` drains it into the v2 SQLite
store, linking each record to a `session` via `session.legacy_run_id`. A v1.2
run that was never drained is therefore in no mirrored table, in no SQLite
snapshot, and **invisible to a snapshot-to-PostgreSQL comparison** — the
reconciliation would report clean while the cutover made the only file holding
those runs read-only. `reconcile` closes this by reporting undrained records as
the `legacy_runs_jsonl` row, so the single `clean` flag that gates the cutover
covers the JSONL leg too.

`data/activity.jsonl` is deliberately **out of scope**: it is an append-only UI
activity log with no PostgreSQL table at all, so it is not a legacy *copy* of
migrated state and there is nothing to reconcile it against. It is not locked;
locking it would only break `activity_log.record()`. Giving it a home in
PostgreSQL is a schema change, and so SRV-01's work.

## Procedure

Run against the migrated schema (`upgrade` first). Every step is idempotent —
a re-run after fixing a finding is the intended way to use this.

```
# 1. Backfill, then reconcile. Writes a checksummed snapshot of every source
#    plus both reports under data/legacy-migration/ (gitignored).
python -m command_center.db legacy-migrate --drain-legacy-runs

# 2. Read the report. Exit code 0 means every table and both non-SQLite
#    authorities reconciled clean.
cat data/legacy-migration/reports/reconciliation-report.json

# 3. Cut over: revoke write access to all three legacy sources.
python -m command_center.db legacy-lock \
    --report data/legacy-migration/reports/reconciliation-report.json
```

`--drain-legacy-runs` runs the existing, idempotent `import_legacy_runs`
*before* the snapshot is taken — necessarily before, since a snapshot taken
first cannot contain the sessions the drain creates. It is opt-in because
draining writes v2 rows into the legacy SQLite store, and a migration should
not mutate the authority it is about to freeze without being asked. Without it,
undrained records are reported as `legacy_runs_jsonl` differences and the
cutover is refused rather than silently stranding them.

`legacy-reconcile` is the same thing without the import: use it to check for
drift after a migrate, or immediately before a lock decision.

## Rollback

```
python -m command_center.db legacy-unlock
```

Nothing in this migration ever deletes or rewrites a legacy source, so rollback
is exactly restoring write access — the SQLite file and both JSON/JSONL files
are byte for byte what they were. Reverting is restoring writability, not
restoring data.

`legacy-lock` and `legacy-unlock` deliberately run **before** any database
configuration or connection pool, for the same reason `self-deploy` does: both
are pure filesystem operations, and rollback must not require a reachable
PostgreSQL — a broken database is exactly when an operator reaches for it.

## What the gates actually refuse

- **Not clean.** `lock_legacy_sources` refuses any reconciliation with a single
  difference in any table, including `legacy_runs_jsonl`. Locking a source that
  reconciliation found still ahead of PostgreSQL would remove the only writable
  copy of rows PostgreSQL does not have.
- **Not covered.** It refuses a path the report does not mention. A clean report
  from another install, or from a run that never looked at this file, is not
  evidence about the file in front of it.
- **Changed since.** Each snapshot records the live source's size and mtime as
  the copy finished; the lock re-checks them. This closes the window between
  "reconcile says clean" and "operator runs the cutover" — on a large database
  the import and reconcile can take long enough for real writes to land, and
  those rows exist only in the file about to go read-only. The SQLite `-wal` and
  `-shm` siblings are fingerprinted with the main file, because a WAL-mode
  commit need not touch `runtime.db`'s mtime at all.
- **Modified snapshot.** Every read of a snapshot re-verifies its SHA-256 first,
  so a report can never describe a snapshot that has since changed on disk.

The enforcement is a `chmod`, not an application flag: it is honoured by the
kernel for every process on the machine, including ones this migration knows
nothing about, where a flag would only stop code paths that remembered to check
it.

## Evidence

`tests/db/test_legacy_migration.py` proves the acceptance criteria directly
against a real SQLite runtime store and a real PostgreSQL (`AICC_TEST_PG_ADMIN_DSN`;
the PostgreSQL half skips without it): counts and primary-key sets match, the
same snapshot imports twice without duplicating anything, `queue_entry` order is
compared as data, an undrained `runs.jsonl` record makes the reconciliation
dirty and the cutover refuse, and lock/unlock round-trips with contents intact.
