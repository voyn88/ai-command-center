DROP FUNCTION IF EXISTS queue_fail_infra_wait(text, text, text, integer);

ALTER TABLE work_item DROP CONSTRAINT IF EXISTS work_item_infra_wait_bounded;
ALTER TABLE work_item DROP COLUMN IF EXISTS infra_wait_count;
