-- VOYN-W0-AICC-INSERT-SEQ: a monotonic insertion-order tiebreaker for
-- backlog_eligible.
--
-- backlog_eligible's (0006) final ORDER BY key was `t.created_at ASC` alone.
-- `created_at` is DEFAULT now() (0005) -- server-clock resolution, which
-- under a virtualized or otherwise throttled clock is coarse enough that
-- two tasks upserted back to back (a bulk backlog-import, two dispatches in
-- the same test tick) land on the SAME created_at. A tie there is not
-- broken by insertion order: PostgreSQL is free to return equal-timestamp
-- rows in either physical order, so "earliest inserted, same wave and
-- priority" -- the property the planner's dispatch order depends on --
-- becomes nondeterministic instead of monotonic.
--
-- The fix is a column ties cannot collide on: `insert_seq`, an IDENTITY
-- assigned exactly once per row at INSERT time and strictly increasing
-- regardless of how many rows land in the same transaction or the same
-- clock tick.

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
              t.insert_seq ASC;
