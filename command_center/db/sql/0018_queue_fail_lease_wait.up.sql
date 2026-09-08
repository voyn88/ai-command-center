-- 0018: writer-lease contention refuses a publish honestly, but the retry it
-- earns must not spend from the same budget a real work failure does
-- (VOYN-W0-AICC-PUBLISH-LEASE-CONTENTION-BURNS-ATTEMPT).
--
-- Live 2026-09-06 (wki_55f316db): a guarded publish lost the writer-lease
-- race to a sibling lane -- `voyn-lease acquire` refused with
-- `lease_unavailable` while neighbouring publishes for other tasks
-- succeeded 04:57-04:58Z. `publish.py`'s own comment at the refusal site
-- already states the intended semantics ("the attempt returns to the pool,
-- and a later tick retries"), but the only route back to the pool was
-- `queue_fail(p_retryable => true)`, which cannot tell WHY a retryable
-- failure happened -- it counts every one of them against the same
-- `attempt_count`/`max_attempts` budget `queue_claim` already advanced when
-- it handed the attempt out. Finished work -- a real commit, unpublished
-- only because another writer held the row -- was dead-lettered by
-- contention it had no part in causing.
--
-- `queue_fail_lease_wait` is a second, narrower exit, not a new parameter on
-- `queue_fail`: refunding `attempt_count` for a genuine handler failure
-- would be wrong, so the refund must be reachable only through a path that
-- can never be handed a real failure by mistake. It undoes exactly the one
-- increment `queue_claim` made for this attempt (never more, never below
-- zero) and tracks its own separate `lease_wait_count` budget -- bounded so
-- that permanent, pathological contention still terminates in the DLQ
-- instead of retrying forever against a budget nothing ever spends down.
ALTER TABLE work_item
    ADD COLUMN lease_wait_count integer NOT NULL DEFAULT 0;

ALTER TABLE work_item
    ADD CONSTRAINT work_item_lease_wait_bounded CHECK (lease_wait_count >= 0);

CREATE FUNCTION queue_fail_lease_wait(
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
