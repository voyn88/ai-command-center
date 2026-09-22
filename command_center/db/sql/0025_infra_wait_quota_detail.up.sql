-- The infra-wait audit row carries the caller's own evidence.
--
-- 0024 made a provider/launcher/sandbox outage refund its attempt instead of
-- spending the task's budget, and audited that refund with the wait counters
-- it decided from. Those counters say an attempt was refunded; they cannot
-- say WHY, and a refusal writes no `work_result` row to say it elsewhere --
-- `queue_complete` is the only path that writes one, and by construction an
-- infra wait never reaches it.
--
-- Quota-aware routing (VOYN-W0-AICC-EXECUTOR-QUOTA-AWARE-ROUTING) is the
-- first caller that has a fact worth keeping: which executor refused for
-- quota, the phrase it refused with, and the instant the worker will offer
-- it again (`executor_quota.exhausted[].exhausted_until`), plus the cascade
-- links a still-open circuit made this delivery SKIP without spending an
-- attempt (`executor_quota.skipped[]`). Without it, "the fleet stalled for
-- half an hour" and "one account was out of quota for half an hour" are the
-- same row.
--
-- Shape: one optional jsonb argument merged INTO the existing detail object,
-- not replacing it -- the counters stay exactly where readers already look,
-- and `p_detail` cannot overwrite them because the server-decided keys are
-- applied last. The parameter defaults to NULL, so a worker still running
-- the pre-0025 four-argument call resolves to this same function and writes
-- the same row it wrote before: the rolling deploy needs no ordering beyond
-- the usual migrate-then-ship.
DROP FUNCTION IF EXISTS queue_fail_infra_wait(text, text, text, integer);

CREATE FUNCTION queue_fail_infra_wait(
    p_attempt_id text, p_claim_token text, p_reason text,
    p_max_infra_waits integer DEFAULT 20, p_detail jsonb DEFAULT NULL
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o record; v queue_verdict; i work_item%ROWTYPE;
        v_waits integer; v_cap integer; v_detail jsonb;
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

    v_cap := greatest(p_max_infra_waits, 1);
    v_waits := i.infra_wait_count + 1;
    -- A caller detail that is not an object (a bare number, a string, an
    -- array) has no keys to merge and is dropped rather than allowed to
    -- replace the counters with a scalar.
    v_detail := CASE WHEN jsonb_typeof(p_detail) = 'object'
                     THEN p_detail ELSE '{}'::jsonb END;

    IF v_waits > v_cap THEN
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               dead_reason = 'infra_wait_exhausted: ' || coalesce(p_reason, 'unspecified'),
               dead_at = now(), updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'infra_wait_dead_lettered',
                             v_detail || jsonb_build_object('infra_wait_count', v_waits,
                                                            'max_infra_waits', v_cap));
        v.reason := 'infra_wait_dead_lettered';
    ELSE
        UPDATE work_item
           SET state = 'ready', current_attempt_id = NULL,
               attempt_count = greatest(i.attempt_count - 1, 0),
               infra_wait_count = v_waits,
               available_at = now() + _queue_backoff(i.retry_backoff_seconds, v_waits),
               updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'infra_wait_requeued',
                             v_detail || jsonb_build_object('infra_wait_count', v_waits,
                                                            'max_infra_waits', v_cap,
                                                            'attempt_count',
                                                            greatest(i.attempt_count - 1, 0)));
        v.reason := 'infra_wait_requeued';
    END IF;

    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;

-- DROP took the old function's grant with it. `roles.apply_table_grants`
-- re-renders this from `roles._WORKER_FUNCTIONS`, but a deploy that migrates
-- without re-applying grants would leave every worker unable to report an
-- infrastructure failure at all -- so the migration restores it itself, the
-- same way 0013 and 0021 grant the functions they create.
GRANT EXECUTE ON FUNCTION queue_fail_infra_wait(text, text, text, integer, jsonb)
    TO aicc_worker;
