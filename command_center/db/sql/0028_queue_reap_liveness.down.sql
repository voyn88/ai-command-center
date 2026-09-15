-- Restore 0002's `queue_reap()`: one unbounded transaction that WAITS on a
-- contended item's row lock and therefore recovers nothing at all when it is
-- interrupted -- the behaviour measured in the up-migration's header (0 of 3
-- items recovered when one of the three was held). Going back reinstates that
-- exactly: the recovery path regains its safety property and loses its
-- liveness one.
--
-- The down path exists because every migration here has one, not because this
-- one is safe to run on a fleet whose `queue_stalled` exit depends on the
-- reaper finishing.
--
-- The bounded arity is dropped rather than left behind. `WorkQueueAdmin.reap`
-- calls it when it is present and falls back to the no-argument form when it
-- is not, so a control host that has gone back keeps reaping -- but a
-- lingering `queue_reap(integer)` over 0002's blocking body would be a second
-- promise of progress the function could no longer keep.
DROP FUNCTION IF EXISTS queue_reap(integer);

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
