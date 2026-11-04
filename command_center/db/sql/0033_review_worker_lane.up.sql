-- A dedicated review worker must select atomically: claiming first and
-- rejecting in Python would briefly consume the item and its attempt budget.
-- Keep the original queue_claim signature for compatibility and expose the
-- filter as a separate, explicitly granted worker function.
CREATE FUNCTION queue_claim_by_task_types(
    p_queue text, p_claim_token_hash text, p_visibility_seconds integer,
    p_task_types jsonb, p_include boolean
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    it work_item%ROWTYPE;
    v queue_verdict;
    v_attempt_id text;
    v_no integer;
    v_hist integer;
    v_used integer;
    v_vis integer;
BEGIN
    v.ok := false;
    IF p_claim_token_hash IS NULL OR length(p_claim_token_hash) <> 64 THEN
        PERFORM _queue_audit(NULL, NULL, 'claim', 'rejected', 'bad_claim_token_hash');
        v.reason := 'bad_claim_token_hash';
        RETURN v;
    END IF;
    IF p_task_types IS NULL OR jsonb_typeof(p_task_types) <> 'array'
       OR jsonb_array_length(p_task_types) = 0 OR p_include IS NULL THEN
        PERFORM _queue_audit(NULL, NULL, 'claim', 'rejected', 'bad_task_type_filter');
        v.reason := 'bad_task_type_filter';
        RETURN v;
    END IF;
    v_vis := least(greatest(coalesce(p_visibility_seconds, 60), 1), 3600);

    SELECT * INTO it FROM work_item
     WHERE queue = p_queue AND state = 'ready' AND available_at <= now()
       AND CASE WHEN p_include
                THEN payload->>'task_type' IN
                     (SELECT jsonb_array_elements_text(p_task_types))
                ELSE coalesce(payload->>'task_type', '') NOT IN
                     (SELECT jsonb_array_elements_text(p_task_types))
           END
     ORDER BY priority DESC, available_at, created_at
     FOR UPDATE SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN
        v.reason := 'no_work';
        RETURN v;
    END IF;

    v_used := it.attempt_count;
    IF v_used + 1 > it.max_attempts THEN
        UPDATE work_item SET state = 'dead', current_attempt_id = NULL,
               dead_reason = 'attempt_budget_exhausted', dead_at = now(), updated_at = now()
         WHERE work_item_id = it.work_item_id;
        PERFORM _queue_audit(it.work_item_id, NULL, 'claim', 'rejected',
                             'attempt_budget_exhausted',
                             jsonb_build_object('attempt_count', v_used,
                                                'max_attempts', it.max_attempts));
        v.reason := 'attempt_budget_exhausted';
        RETURN v;
    END IF;

    SELECT COALESCE(MAX(attempt_no), 0) INTO v_hist
      FROM work_attempt WHERE work_item_id = it.work_item_id;
    v_no := GREATEST(v_used, v_hist) + 1;
    v_attempt_id := _queue_new_id('wat');
    INSERT INTO work_attempt (
        attempt_id, work_item_id, attempt_no, claimed_by_role, claim_token_hash,
        visibility_seconds, visible_until, state, created_at, updated_at)
    VALUES (v_attempt_id, it.work_item_id, v_no, session_user, p_claim_token_hash,
            v_vis, now() + make_interval(secs => v_vis), 'active', now(), now());
    UPDATE work_item SET state = 'claimed', attempt_count = v_used + 1,
           current_attempt_id = v_attempt_id, updated_at = now()
     WHERE work_item_id = it.work_item_id AND state = 'ready';
    IF NOT FOUND THEN
        PERFORM _queue_audit(it.work_item_id, v_attempt_id, 'claim', 'rejected', 'lost_claim_race');
        v.reason := 'lost_claim_race';
        RETURN v;
    END IF;
    PERFORM _queue_audit(it.work_item_id, v_attempt_id, 'claim', 'granted', NULL,
                         jsonb_build_object('attempt_no', v_no,
                                            'attempt_count', v_used + 1,
                                            'visibility_seconds', v_vis,
                                            'task_type_filter', p_task_types,
                                            'include_task_types', p_include));
    v.ok := true;
    v.work_item_id := it.work_item_id;
    v.attempt_id := v_attempt_id;
    v.attempt_no := v_no;
    v.visible_until := now() + make_interval(secs => v_vis);
    v.payload := it.payload;
    RETURN v;
END
$$;
