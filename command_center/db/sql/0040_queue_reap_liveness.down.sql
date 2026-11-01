-- Restore 0037's `queue_reap()` (the body this branch's reap fix was rebased onto): one unbounded transaction that WAITS on a
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
        v_waits integer; v_refunded integer; v_serving boolean;
        -- `queue_reap()` takes no arguments and must not start taking any:
        -- it is granted and called by that exact signature (`roles.py`,
        -- `db.cli queue-reap`, `aicc-queue-reaper.service`), and adding a
        -- defaulted parameter would OVERLOAD it rather than replace it,
        -- leaving the existing zero-argument call ambiguous. The cap is
        -- therefore a constant here, equal to `queue_fail_lease_wait`'s
        -- `p_max_lease_waits` default -- which is the value the worker
        -- actually passes (`work_queue_store.fail_lease_wait`). One fleet
        -- budget, one number.
        v_cap constant integer := 20;
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
