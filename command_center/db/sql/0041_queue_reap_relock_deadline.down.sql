-- Restore 0040's body: the re-test under the item's row lock goes back to
-- checking only that the attempt is still 'active', so a lease renewed between
-- the scan and the lock is expired anyway -- the lane's run discarded as
-- `attempt_superseded`, its side effects re-run under the next delivery, and
-- the attempt `queue_claim` charged for it spent. The measurement is in the
-- up-migration's header.
--
-- Everything else 0040 established is unchanged and must stay: the
-- `SKIP LOCKED` that defers a contended item instead of stopping the tick, the
-- audited deferral, and the bound that lets `WorkQueueAdmin.reap` commit
-- batches. Going back gives up the deadline re-test and nothing else.
--
-- `queue_reap()` is one line over this body and is not re-declared here for
-- the same reason it was not in 0041: the arity that changes is the one the
-- other delegates to.
CREATE OR REPLACE FUNCTION queue_reap(p_max_items integer) RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; i work_item%ROWTYPE; n integer := 0;
        v_waits integer; v_refunded integer; v_serving boolean;
        -- The lease-WAIT cap (0037): equal to `queue_fail_lease_wait`'s
        -- `p_max_lease_waits` default, which is what the worker passes. Not
        -- to be confused with `v_bound`, the per-call ITEM bound below.
        v_cap constant integer := 20;
        v_bound integer;
BEGIN
    -- NULL means "no bound", which is what the no-argument form passes. A
    -- non-positive bound clamps to one item, so a tick always makes progress
    -- (0040).
    v_bound := CASE WHEN p_max_items IS NULL THEN NULL
                    ELSE greatest(p_max_items, 1) END;

    FOR r IN
        SELECT a.attempt_id, a.work_item_id FROM work_attempt a
         WHERE a.state = 'active' AND a.visible_until <= now()
         ORDER BY a.visible_until
    LOOP
        EXIT WHEN v_bound IS NOT NULL AND n >= v_bound;

        -- SKIP LOCKED, not a plain FOR UPDATE (0040). A row somebody else is
        -- holding is deferred to the next tick rather than allowed to stop the
        -- recovery of every item behind it.
        SELECT * INTO i FROM work_item
         WHERE work_item_id = r.work_item_id FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            -- Not silent. `work_item_id` is NULL and the id travels in the
            -- detail because this branch does NOT hold the row lock that
            -- `_queue_audit` requires of a caller naming an item (0039).
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

        v_waits := i.lease_wait_count + 1;
        -- Refund the attempt `queue_claim` spent to make THIS delivery, on
        -- both branches (0028 draws the same conclusion for the refusal that
        -- exhausts the budget as for the ones before it). Never below zero.
        --
        -- Conditional on the attempt still being the item's current one.
        -- Today it always is -- every terminal path writes the attempt's
        -- state, so an 'active' attempt implies a 'claimed' item pointing at
        -- it -- and NO TEST PINS THE FALSE BRANCH, because none can without
        -- forging a row. It is written this way so that if an orphaned
        -- 'active' attempt ever did appear (`queue_claim`'s own
        -- `lost_claim_race` leaves one behind on a CAS it documents as
        -- unreachable), the refund cannot decrement a budget that belongs to
        -- a different, live delivery. Deciding whose attempt to refund by
        -- guessing is exactly the accounting error this migration exists to
        -- stop making.
        v_refunded := CASE
            WHEN i.current_attempt_id IS NOT DISTINCT FROM r.attempt_id
                THEN greatest(i.attempt_count - 1, 0)
            ELSE i.attempt_count
        END;
        v_serving := _queue_fleet_is_serving(i.queue, r.work_item_id);

        IF v_waits > v_cap AND v_serving THEN
            -- The WAIT budget exhausted while the fleet was serving its
            -- siblings, not the attempt budget: lease lapses kept recurring
            -- on THIS item past what is worth waiting out, so whatever is
            -- wrong is not curing itself. The item never spent a model
            -- attempt on any of them, and `work_dlq` reports `attempt_count`
            -- accordingly to the operator deciding whether to redrive.
            UPDATE work_item
               SET state = 'dead', current_attempt_id = NULL,
                   attempt_count = v_refunded,
                   lease_wait_count = v_waits,
                   dead_reason = 'lease_wait_exhausted: visibility_timeout',
                   dead_at = now(), updated_at = now()
             WHERE work_item_id = r.work_item_id;
            PERFORM _queue_audit(r.work_item_id, r.attempt_id, 'expire', 'granted',
                                 'dead_lettered',
                                 jsonb_build_object('lease_wait_count', v_waits,
                                                    'max_lease_waits', v_cap,
                                                    'attempt_count', v_refunded,
                                                    'fleet_is_serving', v_serving));
        ELSE
            -- Backed off by the WAIT count, not by `attempt_count`: the
            -- refund keeps the latter flat, so reading it here would hand a
            -- host that is failing every delivery the same short retry
            -- forever. The queue's own cap (300s) still bounds it, and it is
            -- what keeps a fleet-wide outage from becoming a poll loop now
            -- that exhausting the count no longer ends the item.
            UPDATE work_item
               SET state = 'ready', current_attempt_id = NULL,
                   attempt_count = v_refunded,
                   lease_wait_count = v_waits,
                   available_at = now() + _queue_backoff(i.retry_backoff_seconds, v_waits),
                   updated_at = now()
             WHERE work_item_id = r.work_item_id;
            PERFORM _queue_audit(r.work_item_id, r.attempt_id, 'expire', 'granted',
                                 'requeued',
                                 jsonb_build_object('lease_wait_count', v_waits,
                                                    'max_lease_waits', v_cap,
                                                    'attempt_count', v_refunded,
                                                    'fleet_is_serving', v_serving));
        END IF;
        n := n + 1;
    END LOOP;
    RETURN n;
END
$$;
