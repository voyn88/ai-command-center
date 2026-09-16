-- Restore 0028's body: the re-test under the item's row lock goes back to
-- checking only that the attempt is still 'active', so a lease renewed between
-- the scan and the lock is expired anyway -- the lane's run discarded as
-- `attempt_superseded`, its side effects re-run under the next delivery, and
-- the attempt `queue_claim` charged for it spent. The measurement is in the
-- up-migration's header.
--
-- Everything else 0028 established is unchanged and must stay: the
-- `SKIP LOCKED` that defers a contended item instead of stopping the tick, the
-- audited deferral, and the bound that lets `WorkQueueAdmin.reap` commit
-- batches. Going back gives up the deadline re-test and nothing else.
--
-- `queue_reap()` is one line over this body and is not re-declared here for
-- the same reason it was not in 0029: the arity that changes is the one the
-- other delegates to.
CREATE OR REPLACE FUNCTION queue_reap(p_max_items integer) RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; i work_item%ROWTYPE; n integer := 0; v_cap integer;
BEGIN
    v_cap := CASE WHEN p_max_items IS NULL THEN NULL
                  ELSE greatest(p_max_items, 1) END;

    FOR r IN
        SELECT a.attempt_id, a.work_item_id FROM work_attempt a
         WHERE a.state = 'active' AND a.visible_until <= now()
         ORDER BY a.visible_until
    LOOP
        EXIT WHEN v_cap IS NOT NULL AND n >= v_cap;

        SELECT * INTO i FROM work_item
         WHERE work_item_id = r.work_item_id FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            PERFORM _queue_audit(NULL, r.attempt_id, 'expire', 'rejected',
                                 'item_locked',
                                 jsonb_build_object('deferred_work_item_id',
                                                    r.work_item_id));
            CONTINUE;
        END IF;

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
