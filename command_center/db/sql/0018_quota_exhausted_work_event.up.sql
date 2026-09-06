-- 0018: quota-exhaustion telemetry reaches work_event, not just work_result /
-- work_attempt (VOYN-W0-AICC-EXECUTOR-QUOTA-AWARE-ROUTING).
--
-- The worker (`command_center/worker/handlers.py`) already knows exactly when
-- a cascade link was skipped or a delivery ended for QUOTA exhaustion
-- specifically -- `agent_runner.record_executor_exhausted` opened the
-- circuit, and the attempt's own `result`/`reason` already carry the
-- `exhausted_until` value both `queue_complete` and `queue_fail` receive as
-- `p_result`/`p_reason`. Neither function's audit call forwarded either
-- payload into `work_event.detail`: `queue_complete` recorded only the
-- `result_id` pointer (the fact lived in `work_result.payload`, reachable
-- only by joining back through that id), and `queue_fail` recorded only the
-- coarse `attempt_count`/`max_attempts`/`retryable` triple (the fact lived in
-- `work_attempt.outcome_reason`, embedded as free text). `work_event` is this
-- queue's append-only decision audit -- "INCLUDING THE REFUSALS" per its own
-- table comment -- and a quota refusal is exactly the kind of decision that
-- table exists to make queryable without joining out to two other tables and
-- parsing a text column.
--
-- Both changes are body-only, over the SAME argument list `0002_queue_claim`
-- shipped: neither function gains a parameter, so this is `CREATE OR REPLACE`
-- (no `GRANT EXECUTE` in `command_center/db/roles.py` names a signature that
-- moves), matching this codebase's own precedent for revising a function
-- (0009, 0010, 0011, 0017) of never changing a shipped function's arg types.
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

    -- `quota_exhausted` (VOYN-W0-AICC-EXECUTOR-QUOTA-AWARE-ROUTING): every
    -- recognized quota refusal THIS attempt observed before succeeding on a
    -- later cascade link, verbatim from the worker's own result. Folded in
    -- only when non-empty, so an ordinary completion's audit detail stays
    -- exactly the one-key shape it always was.
    PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'complete', 'granted', NULL,
                         CASE
                             WHEN p_result ? 'quota_exhausted'
                                  AND jsonb_typeof(p_result -> 'quota_exhausted') = 'array'
                                  AND jsonb_array_length(p_result -> 'quota_exhausted') > 0
                             THEN jsonb_build_object('result_id', v_result_id,
                                                      'quota_exhausted', p_result -> 'quota_exhausted')
                             ELSE jsonb_build_object('result_id', v_result_id)
                         END);
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
        -- `outcome_reason` (VOYN-W0-AICC-EXECUTOR-QUOTA-AWARE-ROUTING): the
        -- same full reason text `work_attempt.outcome_reason` just recorded
        -- above, including the worker's `[exhausted_until=...]` suffix on a
        -- terminal quota refusal -- so that fact is queryable from
        -- `work_event` directly rather than only via `work_attempt`.
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'dead_lettered',
                             jsonb_build_object('attempt_count', i.attempt_count,
                                                'max_attempts', i.max_attempts,
                                                'retryable', p_retryable,
                                                'outcome_reason', p_reason));
        v.reason := 'dead_lettered';
    ELSE
        UPDATE work_item
           SET state = 'ready', current_attempt_id = NULL,
               available_at = now() + _queue_backoff(i.retry_backoff_seconds, i.attempt_count),
               updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted', 'requeued',
                             jsonb_build_object('attempt_count', i.attempt_count,
                                                'max_attempts', i.max_attempts,
                                                'outcome_reason', p_reason));
        v.reason := 'requeued';
    END IF;

    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;
