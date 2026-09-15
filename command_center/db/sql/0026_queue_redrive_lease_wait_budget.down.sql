-- Restore 0002's `queue_redrive`, which widens the attempt budget and leaves
-- `lease_wait_count` standing. Going back reinstates the no-op described in
-- the up-migration: an item dead-lettered `lease_wait_exhausted` is redriven
-- successfully and dies again on its first writer-lease refusal, with its
-- finished work stranded in the DLQ. The down path exists because every
-- migration here has one, not because this one is safe to run on a fleet
-- using the lease-wait exit.
--
-- `lease_wait_count` itself is 0022's column and is not dropped here; only the
-- redrive's treatment of it goes back.
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
           available_at = now(), updated_at = now()
     WHERE work_item_id = p_work_item_id;
    PERFORM _queue_audit(p_work_item_id, NULL, 'redrive', 'granted', NULL,
                         jsonb_build_object('extra_attempts', greatest(p_extra_attempts, 1),
                                            'previous_dead_reason', i.dead_reason));
    RETURN true;
END
$$;
