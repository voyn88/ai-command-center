-- Revert the insert_seq tiebreaker: restore backlog_eligible to the 0019
-- ordering (created_at alone, with task_class), then drop the column (the
-- view must stop referencing it first).

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
              t.created_at ASC;

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

ALTER TABLE backlog_task DROP COLUMN insert_seq;
