-- Restore the 0022 lease-wait failure: the requeue branch refunds the attempt
-- the claim spent, the dead-letter branch keeps it. Down migrations restore the
-- previous behaviour, including the stray increment a redrive then spends.
CREATE OR REPLACE FUNCTION queue_fail_lease_wait(
    p_attempt_id text, p_claim_token text, p_reason text,
    p_max_lease_waits integer DEFAULT 20
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o record; v queue_verdict; i work_item%ROWTYPE;
        v_waits integer; v_cap integer;
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

    IF v_waits > v_cap THEN
        -- The lease-wait budget exhausted, not the attempt budget: writer
        -- -lease contention kept recurring past what is worth waiting out.
        -- Dead-letter with a distinct cause so an operator (or an automated
        -- redrive) can tell this apart from a work item whose own attempts
        -- kept failing.
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               dead_reason = 'lease_wait_exhausted: ' || coalesce(p_reason, 'unspecified'),
               dead_at = now(), updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'lease_wait_dead_lettered',
                             jsonb_build_object('lease_wait_count', v_waits,
                                                'max_lease_waits', v_cap));
        v.reason := 'lease_wait_dead_lettered';
    ELSE
        -- Refund the attempt this claim spent: a writer-lease refusal is not
        -- evidence the WORK is bad, so it must not count down the same
        -- budget a real failure does.
        UPDATE work_item
           SET state = 'ready', current_attempt_id = NULL,
               attempt_count = greatest(i.attempt_count - 1, 0),
               lease_wait_count = v_waits,
               available_at = now() + _queue_backoff(i.retry_backoff_seconds, v_waits),
               updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'lease_wait_requeued',
                             jsonb_build_object('lease_wait_count', v_waits,
                                                'max_lease_waits', v_cap,
                                                'attempt_count',
                                                greatest(i.attempt_count - 1, 0)));
        v.reason := 'lease_wait_requeued';
    END IF;

    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;
