# SQLite schema upgrade window (16 → head)

Ticket: `VOYN-W0-AICC-SQLITE-SCHEMA-16-TO-23` (deployed-schema-behind-code).

The live `data/runtime.db` sat at schema **16** while code on `origin/main`
already carried **23** (and has since moved further). The seven steps
17…23 had never been timed against that ~346 MB file. This window is separate
from any provenance / PostgreSQL mirror **backfill**, and it takes its own
backup **before** those backfills run.

`SCHEMA-VERSION-DRIFT` (the “14 vs main” report) is cancelled: that “14” was a
checkout 66 commits behind, not a branch divergence.

## What the seven steps actually do

| Version | Cost shape on a populated DB |
| --- | --- |
| 17 | `ALTER TABLE … ADD COLUMN` + `CREATE INDEX` on `owner_item` / `digest_item` — scales with those tables |
| 18–23 | `CREATE TABLE IF NOT EXISTS` for new empty families (conflict, audit, models, market, council, networking) — O(1) |

Later heads on current `main` also add:

| Version | Cost shape |
| --- | --- |
| 24 | `finalized_at` + partial index on `run` — scales with `run` row count |
| 25 | finalization-claim fencing — **refused** by ordinary `migrate()`; needs the offline cutover CLI |

## Operator sequence

1. **Schedule a dedicated window.** Stop intake / writers that must not race a
   schema change. Retention and mirror backfills wait until this window ends.
2. **Rehearse on a copy first** (original must stay byte-identical):

   ```bash
   python scripts/rehearse_runtime_schema_upgrade.py \
     --db data/runtime.db \
     --archive-dir data/schema-upgrade \
     --stop-after 23
   ```

   The report lists per-version `elapsed_seconds`. Re-run with
   `--stop-after 24` if you will also apply the `finalized_at` index in the
   same window. Do **not** pass `--backfill` during rehearsal unless you are
   deliberately timing that separate step.
3. **Backup the live file** (the rehearsal already wrote
   `runtime-schema-backup-*.db` under `--archive-dir`; keep it).
4. **Apply schema only on the live DB**, still without the large backfill:

   ```python
   from pathlib import Path
   from command_center.runtime import schema_upgrade
   schema_upgrade.apply_pending_migrations(
       Path("data/runtime.db"),
       stop_after_version=24,   # or 23 if stopping at the ticket boundary
       run_backfill=False,
   )
   ```
5. **v25 offline cutover** (only when promoting to a binary whose
   `SCHEMA_VERSION` is 25+):

   ```bash
   python scripts/execution_center_debug.py offline-finalization-cutover --confirm-offline
   ```
6. **Only then** run provenance / mirror backfills (`db.migrate()`’s bounded
   provenance backfill, `backfill_run_provenance`, or PostgreSQL mirror
   backfills). Rollback is `command_center.runtime.maintenance.restore_backup`.

## Proof in CI

- `tests/test_schema_upgrade.py` times 17…23 over populated `owner_item` /
  `digest_item` stand-ins and proves rehearsal leaves the original untouched
  with backfill off.
- `tests/test_runtime_db.py::test_migration_24_partial_index_build_over_populated_run_table_is_bounded`
  times the one later step whose cost scales with `run`.
