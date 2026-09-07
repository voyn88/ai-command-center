-- Downgrade of 0010_run_completion_insert_seq (VOYN-W0-AICC-INSERT-SEQ).
--
-- Indexes named first, columns dropped after, so the downgrade reads as the
-- exact inverse of the upgrade rather than relying on the column drop to take
-- an index with it silently.

DROP INDEX IF EXISTS idx_completion_task_insert_seq;
DROP INDEX IF EXISTS idx_run_task_insert_seq;

ALTER TABLE completion DROP COLUMN IF EXISTS insert_seq;
ALTER TABLE run DROP COLUMN IF EXISTS insert_seq;
