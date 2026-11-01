DROP FUNCTION IF EXISTS queue_fail_lease_wait(text, text, text, integer);

ALTER TABLE work_item DROP CONSTRAINT IF EXISTS work_item_lease_wait_bounded;
ALTER TABLE work_item DROP COLUMN IF EXISTS lease_wait_count;
