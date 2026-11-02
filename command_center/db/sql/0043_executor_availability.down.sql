-- Downgrade of 0018_executor_availability (VOYN-W0-AICC-EXECUTOR-QUOTA-VISIBILITY).
--
-- 0018 introduced both tables wholesale and altered nothing pre-existing, so
-- the downgrade is a plain drop -- no prior shape to restore.

DROP FUNCTION IF EXISTS executor_mark_available(text);
DROP FUNCTION IF EXISTS executor_mark_unavailable(text, text, integer);

DROP TABLE IF EXISTS executor_availability_event;
DROP TABLE IF EXISTS executor_availability;
