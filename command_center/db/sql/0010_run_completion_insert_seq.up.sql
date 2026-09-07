-- 0010_run_completion_insert_seq (VOYN-W0-AICC-INSERT-SEQ)
--
-- The PostgreSQL side of the SQLite authority's `_migration_25_add_insert_seq`
-- (command_center/runtime/db/schema.py): a monotonic, per-`task_id` insertion
-- counter on `run` and `completion`, replacing `rowid` as the tiebreak
-- `get_latest_run_for_task`/`get_completion_by_task` order by.
--
-- Neither table had a usable insertion-order column before this. `run.sequence`
-- is scoped to `session_id`, and a task with N restarts gets N sessions — see
-- the SQLite migration's docstring for the full argument, which applies here
-- unchanged since it is about what the two candidate columns mean, not about
-- which engine stores them. `completion` carries no sequence column at all.
--
-- `integer`, matching `run.sequence`'s own type rather than the `bigint` this
-- schema reserves for `GENERATED ALWAYS AS IDENTITY` surrogate keys (slice 6):
-- `insert_seq` is neither a primary key nor identity-generated, it is a plain
-- application-computed counter mirrored across from the SQLite authority like
-- `sequence` already is.
--
-- Nullable, matching the SQLite column: existing rows on a mirror that
-- predates this migration have nothing to backfill from (the SQLite migration
-- seeds them from `rowid`, an artifact of the SQLite file only), and a
-- `NOT NULL DEFAULT` would manufacture an ordering the row's own history does
-- not carry.

ALTER TABLE run ADD COLUMN insert_seq integer;
ALTER TABLE completion ADD COLUMN insert_seq integer;

CREATE INDEX idx_run_task_insert_seq ON run(task_id, insert_seq);
CREATE INDEX idx_completion_task_insert_seq ON completion(task_id, insert_seq);
