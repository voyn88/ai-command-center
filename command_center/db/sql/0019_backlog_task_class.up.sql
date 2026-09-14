-- VOYN-W0-AICC-PLANNER-PIPELINE-CLASS-PRIORITY-AND-WINDOW-PAUSE: tasks that
-- repair the delivery pipeline itself (CI, review, merge train, planner,
-- queue) outrank functional work within the same wave, and the review-
-- backlog fence must never starve them. Observed 2026-09-08: dispatch paused
-- for hours at review backlog 164 while four lanes sat idle and the fixes
-- for the very backlog (self-cancel CI, marker carry-over, window gate)
-- waited OPEN behind functional tasks.
--
-- `task_class` is an explicit machine field (never inferred from a task id
-- substring): 'functional' by default, 'pipeline' set by the backlog writer.
ALTER TABLE backlog_task
    ADD COLUMN task_class text NOT NULL DEFAULT 'functional'
        CONSTRAINT backlog_task_class_vocabulary
        CHECK (task_class IN ('functional', 'pipeline'));

DROP VIEW IF EXISTS backlog_eligible;
CREATE VIEW backlog_eligible AS
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
