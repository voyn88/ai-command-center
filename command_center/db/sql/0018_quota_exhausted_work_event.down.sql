-- Restore the 0002 bodies: `work_event.detail` for 'complete' carries only
-- `result_id`, and for 'fail' carries only `attempt_count`/`max_attempts`/
-- (`retryable`).
CREATE OR REPLACE FUNCTION queue_complete(p_attempt_id text, p_claim_token text, p_result jsonb)
    RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o record; v queue_verdict; v_result_id text;
BEGIN
    v.ok := false;
    IF p_result IS NULL THEN
        v.reason := 'result_required';
        RETURN v;
    END IF;

    o := _queue_owns(p_attempt_id, p_claim_token);
    IF o.reason IS NOT NULL THEN
        -- THE STALE-OWNER REFUSAL. A worker whose visibility timeout elapsed and
        -- whose item was re-claimed lands here with 'attempt_superseded', and
        -- its result is not written. Audited, because it returns.
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'complete', 'rejected', o.reason);
        v.reason := o.reason;
        RETURN v;
    END IF;

    v_result_id := _queue_new_id('wrs');

    INSERT INTO work_result (result_id, attempt_id, work_item_id, payload, created_at)
    VALUES (v_result_id, p_attempt_id, o.work_item_id, p_result, now());

    UPDATE work_attempt SET state = 'succeeded', result_id = v_result_id,
           updated_at = now()
     WHERE attempt_id = p_attempt_id;

    UPDATE work_item SET state = 'succeeded', result_id = v_result_id,
           current_attempt_id = NULL, updated_at = now()
     WHERE work_item_id = o.work_item_id;

    PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'complete', 'granted', NULL,
                         jsonb_build_object('result_id', v_result_id));
    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;


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
