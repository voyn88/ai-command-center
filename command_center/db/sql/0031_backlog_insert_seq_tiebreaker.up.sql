-- 0031_backlog_insert_seq_tiebreaker
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
--
-- Recreates the 0019 view shape (including `task_class`) and restores the
-- migrator owner + app read grant in the same migration (0020's policy:
-- recreating a view past the baseline must be followed by an owner restore
-- at that version or later).
--
-- Numbered 0031 because 0029/0030 already landed on main; earlier remediations
-- that reused 0029 failed discover() (duplicate version) and took down the
-- Prepare CI / manifest / final merge gates.

ALTER TABLE backlog_task ADD COLUMN insert_seq bigint GENERATED ALWAYS AS IDENTITY;

CREATE OR REPLACE VIEW backlog_eligible AS
    SELECT t.task_id, t.wave, t.priority, t.status, t.title, t.body, t.repo,
           t.revision,
           (t.wave ~ '^[0-9]+(\.[0-9]+)?$') AS numeric_wave,
           (t.repo IS NOT NULL) AS dispatchable,
           t.task_class
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
              (t.task_class = 'pipeline') DESC,
              coalesce(t.priority, 'P9') ASC,
              t.created_at ASC,
              t.insert_seq ASC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aicc_migrator') THEN
        EXECUTE 'ALTER VIEW backlog_eligible OWNER TO aicc_migrator';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aicc_app') THEN
        EXECUTE 'GRANT SELECT ON backlog_eligible TO aicc_app';
    END IF;
END
$$;
