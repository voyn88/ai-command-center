-- 0017_backlog_insert_seq_tiebreaker
--
-- backlog_eligible's dispatch order ends on `created_at ASC`, but
-- `created_at` is only microsecond-resolution and importer batches can land
-- several tasks inside the same tick: two rows with an identical timestamp
-- come back from the view in whatever order PostgreSQL happens to produce
-- them in, not the order they were inserted. `insert_seq` is a plain
-- monotonic identity column — its only job is to break that tie the way the
-- rows actually arrived.
--
-- It is a tiebreaker APPENDED after `created_at`, never a replacement for
-- it: existing rows get their `insert_seq` values in whatever order this
-- ALTER's table scan visits them, which has no relationship to their real
-- `created_at` history. Sorting on `insert_seq` before `created_at` would let
-- that backfill artifact reorder every task that predates this migration.
-- Kept last, it only ever resolves same-timestamp ties.

ALTER TABLE backlog_task ADD COLUMN insert_seq bigint GENERATED ALWAYS AS IDENTITY;

CREATE OR REPLACE VIEW backlog_eligible AS
    SELECT t.task_id, t.wave, t.priority, t.status, t.title, t.body, t.repo,
           t.revision,
           (t.wave ~ '^[0-9]+(\.[0-9]+)?$') AS numeric_wave,
           (t.repo IS NOT NULL) AS dispatchable
      FROM backlog_task t
     WHERE t.kind = 'task'
       AND t.status = 'OPEN'
       AND NOT EXISTS (
           SELECT 1 FROM backlog_dependency d
             JOIN backlog_task dep ON dep.task_id = d.depends_on_task_id
            WHERE d.task_id = t.task_id AND dep.status <> 'DONE')
     ORDER BY (t.wave ~ '^[0-9]+(\.[0-9]+)?$') DESC,
              CASE WHEN t.wave ~ '^[0-9]+(\.[0-9]+)?$'
                   THEN t.wave::numeric ELSE NULL END ASC,
              coalesce(t.priority, 'P9') ASC,
              t.created_at ASC,
              t.insert_seq ASC;
