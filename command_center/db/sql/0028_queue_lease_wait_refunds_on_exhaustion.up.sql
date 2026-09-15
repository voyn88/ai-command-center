-- 0028: the LAST no-fault refusal must refund the attempt it was handed too
-- (VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH-C2E2267ECC; carried over from
-- fleet PR #945, renumbered behind 0025/0026).
--
-- 0022's requeue branch states the rule its own comment gives: "a ... refusal
-- is not evidence the WORK is bad", so the attempt `queue_claim` spent to
-- deliver it comes back. Its dead-letter branch -- the one taken when the
-- wait budget is finally exhausted -- did not, so the item's `attempt_count`
-- kept the very last delivery even though that delivery was refused for
-- exactly the same no-fault reason as every refunded one before it.
--
-- Nothing claims a dead item, so this is not a retry bug; it is an accounting
-- one, and it surfaces at the DLQ's exit. `queue_redrive` deliberately does
-- not reset `attempt_count` (0002: the attempt history is what stops a
-- redrive loop from silently granting infinite retries), so the stray
-- increment follows the item back onto the queue and spends one of the
-- attempts the operator just granted it -- on a delivery that never reached a
-- model. On a two-link cascade that is half the budget.
--
-- With this, `attempt_count` on a no-fault dead letter means exactly what it
-- means everywhere else: deliveries this ITEM spent. An item that never got
-- past contention or a capacity outage reads `0`, and `work_dlq` says so
-- to the operator deciding whether to redrive.
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
