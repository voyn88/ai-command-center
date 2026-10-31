-- 0029 down: drop the capacity family and restore 0027's redrive, which
-- clears the two wait budgets that exist without this migration. The column
-- goes last: the redrive body must stop naming it first.
CREATE OR REPLACE FUNCTION queue_redrive(p_work_item_id text, p_extra_attempts integer DEFAULT 1)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE i work_item%ROWTYPE;
BEGIN
    SELECT * INTO i FROM work_item WHERE work_item_id = p_work_item_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _queue_audit(NULL, NULL, 'redrive', 'rejected', 'unknown_work_item',
                             jsonb_build_object('requested_work_item_id', p_work_item_id));
        RETURN false;
    END IF;
    IF i.state <> 'dead' THEN
        PERFORM _queue_audit(p_work_item_id, NULL, 'redrive', 'rejected', 'not_dead_lettered',
                             jsonb_build_object('state', i.state));
        RETURN false;
    END IF;
    UPDATE work_item
       SET state = 'ready', dead_reason = NULL, dead_at = NULL,
           max_attempts = max_attempts + greatest(p_extra_attempts, 1),
           lease_wait_count = 0,
           infra_wait_count = 0,
           available_at = now(), updated_at = now()
     WHERE work_item_id = p_work_item_id;
    PERFORM _queue_audit(p_work_item_id, NULL, 'redrive', 'granted', NULL,
                         jsonb_build_object('extra_attempts', greatest(p_extra_attempts, 1),
                                            'previous_dead_reason', i.dead_reason,
                                            'cleared_lease_wait_count', i.lease_wait_count,
                                            'cleared_infra_wait_count', i.infra_wait_count));
    RETURN true;
END
$$;

DROP FUNCTION IF EXISTS queue_fail_capacity_wait(text, text, text, integer);
DROP FUNCTION IF EXISTS _queue_capacity_backoff(integer);

ALTER TABLE work_item DROP CONSTRAINT IF EXISTS work_item_capacity_wait_bounded;
ALTER TABLE work_item DROP COLUMN IF EXISTS capacity_wait_count;
