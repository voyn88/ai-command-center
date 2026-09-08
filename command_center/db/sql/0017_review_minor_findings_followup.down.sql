DROP FUNCTION IF EXISTS backlog_record_followup(text, text, text, text, text);
DROP INDEX IF EXISTS idx_backlog_task_followup_parent_kind;
DROP TABLE IF EXISTS backlog_task_followup;
