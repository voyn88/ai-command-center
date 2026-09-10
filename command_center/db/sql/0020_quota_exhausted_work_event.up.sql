-- 0020: surface a worker-local executor-quota-exhaustion circuit opening in
-- work_event, the same durable audit trail every other queue decision
-- already lands in (VOYN-W0-AICC-EXECUTOR-QUOTA-AWARE-ROUTING-REM).
--
-- worker.handlers._run_agent now recognizes an executor's own quota-
-- exhaustion refusal (agent_runner.RunResult.executor_quota_signature,
-- read from the CLI's OWN diagnostic channel -- Claude's structured
-- `api_error_status`, Copilot/Codex's bounded stderr tail -- never from
-- free-form stdout an in-flight task attempt can legitimately echo) and
-- opens a worker-wide, time-bound circuit via
-- agent_runner.record_executor_exhausted, so the executor cascade
-- (BO-S2a) skips it on every later dispatch without spending an attempt.
-- When the cascade fails over inside the SAME already-claimed attempt, the
-- handler already reports each switch as a `route_failovers` entry inside
-- the result it hands to `queue_complete` -- this migration only teaches
-- `queue_complete` to also copy that same, already-sent data into the
-- `complete` event's audit detail, so an operator reading `work_event`
-- (not only `work_result`'s opaque payload) sees the executor, the reason,
-- and -- when the reason is `quota_exhausted` -- the `exhausted_until`
-- deadline the circuit opened until.
--
-- Extraction only: no new argument, no new trust boundary, no change to
-- who can call this function or what it is allowed to do to work_item/
-- work_attempt/work_result. The dead-letter path (queue_fail) is
-- unchanged -- an exhausted cascade with no available link left already
-- carries the same "quota exhausted until ..." fact in the human-readable
-- reason text that lands in work_attempt.outcome_reason and (once
-- dead-lettered) work_item.dead_reason.
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

    -- `route_failovers` (executor, reason, and -- for a quota-exhaustion
    -- reason -- `exhausted_until`) already travels inside `p_result`
    -- because the worker built it before calling this function; copied
    -- through here, not recomputed, so this can never disagree with what
    -- the handler actually did.
    PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'complete', 'granted', NULL,
                         CASE WHEN p_result ? 'route_failovers'
                              THEN jsonb_build_object('result_id', v_result_id,
                                                       'route_failovers', p_result -> 'route_failovers')
                              ELSE jsonb_build_object('result_id', v_result_id)
                         END);
    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;
