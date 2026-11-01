-- Restore the 0002 reaper: a lapsed lease spends the item's `attempt_count`
-- and dead-letters on `max_attempts` as `visibility_timeout_exhausted`. Down
-- migrations restore the previous behaviour, including the mis-accounting
-- that dead-lettered an item after two worker restarts it had no part in.
CREATE OR REPLACE FUNCTION queue_reap() RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; i work_item%ROWTYPE; n integer := 0;
BEGIN
    FOR r IN
        SELECT a.attempt_id, a.work_item_id FROM work_attempt a
         WHERE a.state = 'active' AND a.visible_until <= now()
         ORDER BY a.visible_until
    LOOP
        SELECT * INTO i FROM work_item WHERE work_item_id = r.work_item_id FOR UPDATE;

        -- Re-test under the lock: a completion may have landed since the scan.
        CONTINUE WHEN NOT EXISTS (
            SELECT 1 FROM work_attempt WHERE attempt_id = r.attempt_id AND state = 'active');

        UPDATE work_attempt SET state = 'expired', outcome_reason = 'visibility_timeout',
               updated_at = now()
         WHERE attempt_id = r.attempt_id;

        IF i.attempt_count >= i.max_attempts THEN
            UPDATE work_item
               SET state = 'dead', current_attempt_id = NULL,
                   dead_reason = 'visibility_timeout_exhausted',
                   dead_at = now(), updated_at = now()
             WHERE work_item_id = r.work_item_id;
            PERFORM _queue_audit(r.work_item_id, r.attempt_id, 'expire', 'granted',
                                 'dead_lettered');
        ELSE
            UPDATE work_item
               SET state = 'ready', current_attempt_id = NULL,
                   available_at = now() + _queue_backoff(i.retry_backoff_seconds,
                                                         i.attempt_count),
                   updated_at = now()
             WHERE work_item_id = r.work_item_id;
            PERFORM _queue_audit(r.work_item_id, r.attempt_id, 'expire', 'granted',
                                 'requeued');
        END IF;
        n := n + 1;
    END LOOP;
    RETURN n;
END
$$;
