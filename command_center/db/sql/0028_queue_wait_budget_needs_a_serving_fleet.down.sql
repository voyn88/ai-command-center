-- Restore the pre-0028 behaviour: the no-fault wait budget dead-letters on
-- its 21st wait regardless of whether the fleet was serving anything else,
-- `queue_fail`'s requeue carries `lease_wait_count` across untouched, and the
-- fleet-serving evidence function goes away. Down migrations restore the
-- previous behaviour, including the accounting that converted a capacity
-- outage longer than 69 minutes into a dead letter per item in flight.

-- 0026's lease-wait failure, unchanged.
CREATE OR REPLACE FUNCTION queue_fail_lease_wait(
    p_attempt_id text, p_claim_token text, p_reason text,
    p_max_lease_waits integer DEFAULT 20
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o record; v queue_verdict; i work_item%ROWTYPE;
        v_waits integer; v_cap integer; v_refunded integer;
BEGIN
    v.ok := false;
    o := _queue_owns(p_attempt_id, p_claim_token);
    IF o.reason IS NOT NULL THEN
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'rejected', o.reason);
        v.reason := o.reason;
        RETURN v;
    END IF;

    UPDATE work_attempt SET state = 'failed', outcome_reason = p_reason, updated_at = now()
     WHERE attempt_id = p_attempt_id;

    SELECT * INTO i FROM work_item WHERE work_item_id = o.work_item_id;

    v_cap := greatest(p_max_lease_waits, 1);
    v_waits := i.lease_wait_count + 1;
    -- Refund the attempt this claim spent, on BOTH branches: a refusal that
    -- names no fault in the work is not evidence the WORK is bad, and that is
    -- as true of the refusal that exhausts the wait budget as of the ones
    -- before it. The delivery it was handed under keeps its `attempt_no` and
    -- its `work_attempt` row either way -- the history is not what is being
    -- refunded (see 0025: the delivery number and the budget are separate
    -- numbers taken from separate places).
    v_refunded := greatest(i.attempt_count - 1, 0);

    IF v_waits > v_cap THEN
        -- The lease-wait budget exhausted, not the attempt budget: writer
        -- -lease contention kept recurring past what is worth waiting out.
        -- Dead-letter with a distinct cause so an operator (or an automated
        -- redrive) can tell this apart from a work item whose own attempts
        -- kept failing.
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               attempt_count = v_refunded,
               lease_wait_count = v_waits,
               dead_reason = 'lease_wait_exhausted: ' || coalesce(p_reason, 'unspecified'),
               dead_at = now(), updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'lease_wait_dead_lettered',
                             jsonb_build_object('lease_wait_count', v_waits,
                                                'max_lease_waits', v_cap,
                                                'attempt_count', v_refunded));
        v.reason := 'lease_wait_dead_lettered';
    ELSE
        UPDATE work_item
           SET state = 'ready', current_attempt_id = NULL,
               attempt_count = v_refunded,
               lease_wait_count = v_waits,
               available_at = now() + _queue_backoff(i.retry_backoff_seconds, v_waits),
               updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'lease_wait_requeued',
                             jsonb_build_object('lease_wait_count', v_waits,
                                                'max_lease_waits', v_cap,
                                                'attempt_count', v_refunded));
        v.reason := 'lease_wait_requeued';
    END IF;

    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;

-- 0027's reaper, unchanged.
CREATE OR REPLACE FUNCTION queue_reap() RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; i work_item%ROWTYPE; n integer := 0;
        v_waits integer; v_refunded integer;
        -- `queue_reap()` takes no arguments and must not start taking any:
        -- it is granted and called by that exact signature (`roles.py`,
        -- `db.cli queue-reap`, `aicc-queue-reaper.service`), and adding a
        -- defaulted parameter would OVERLOAD it rather than replace it,
        -- leaving the existing zero-argument call ambiguous. The cap is therefore a constant here, equal to
        -- `queue_fail_lease_wait`'s `p_max_lease_waits` default -- which is
        -- the value the worker actually passes
        -- (`work_queue_store.fail_lease_wait`). One fleet budget, one number.
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
        -- both branches (0026 draws the same conclusion for the refusal that
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

        IF v_waits > v_cap THEN
            -- The WAIT budget exhausted, not the attempt budget: lease lapses
            -- kept recurring past what is worth waiting out, so whatever is
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
                                                    'attempt_count', v_refunded));
        ELSE
            -- Backed off by the WAIT count, not by `attempt_count`: the
            -- refund keeps the latter flat, so reading it here would hand a
            -- host that is failing every delivery the same short retry
            -- forever. The queue's own cap (300s) still bounds it.
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
                                                    'attempt_count', v_refunded));
        END IF;
        n := n + 1;
    END LOOP;
    RETURN n;
END
$$;

-- 0002's `queue_fail`, unchanged: the requeue branch leaves
-- `lease_wait_count` alone.
CREATE OR REPLACE FUNCTION queue_fail(
    p_attempt_id text, p_claim_token text, p_reason text,
    p_retryable boolean DEFAULT true
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o record; v queue_verdict; i work_item%ROWTYPE;
BEGIN
    v.ok := false;
    o := _queue_owns(p_attempt_id, p_claim_token);
    IF o.reason IS NOT NULL THEN
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'rejected', o.reason);
        v.reason := o.reason;
        RETURN v;
    END IF;

    UPDATE work_attempt SET state = 'failed', outcome_reason = p_reason, updated_at = now()
     WHERE attempt_id = p_attempt_id;

    SELECT * INTO i FROM work_item WHERE work_item_id = o.work_item_id;

    IF NOT p_retryable OR i.attempt_count >= i.max_attempts THEN
        -- DEAD LETTER. The cause is preserved on the item and every attempt
        -- that led there is preserved beside it; `work_dlq` exposes both.
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               dead_reason = CASE WHEN NOT p_retryable
                    THEN 'non_retryable: ' || coalesce(p_reason, 'unspecified')
                    ELSE 'max_attempts_exhausted: ' || coalesce(p_reason, 'unspecified') END,
               dead_at = now(), updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'dead_lettered',
                             jsonb_build_object('attempt_count', i.attempt_count,
                                                'max_attempts', i.max_attempts,
                                                'retryable', p_retryable));
        v.reason := 'dead_lettered';
    ELSE
        UPDATE work_item
           SET state = 'ready', current_attempt_id = NULL,
               available_at = now() + _queue_backoff(i.retry_backoff_seconds, i.attempt_count),
               updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted', 'requeued',
                             jsonb_build_object('attempt_count', i.attempt_count,
                                                'max_attempts', i.max_attempts));
        v.reason := 'requeued';
    END IF;

    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;

DROP FUNCTION IF EXISTS _queue_fleet_is_serving(text, text, interval);
DROP INDEX IF EXISTS idx_work_event_reached_the_work;
