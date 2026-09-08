DROP FUNCTION IF EXISTS backlog_mark_duplicate(text, text, text);
ALTER TABLE backlog_task DROP COLUMN duplicate_of;
