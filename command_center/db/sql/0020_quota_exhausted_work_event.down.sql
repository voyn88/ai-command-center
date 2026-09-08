-- Restore the pre-0020 queue_complete: audit detail carries only result_id.
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
