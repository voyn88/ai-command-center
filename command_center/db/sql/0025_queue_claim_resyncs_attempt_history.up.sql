-- `queue_claim` used `attempt_count + 1` as the next attempt_no. That counter
-- can lag the `work_attempt` history (redrive, infra-wait refund, or a
-- previous claim that inserted the row then lost the CAS). The UNIQUE index
-- then raises UniqueViolation, which crashed every worker lane (live
-- 2026-09-11: four `voyn-aicc-worker@` units crash-looped on
-- `(wki_91e6ef4f…, 8)` until the host credential TTL expired and the fleet
-- could not recover).
--
-- The attempt rows are the authority. Take the next number from them.
CREATE OR REPLACE FUNCTION queue_claim(
    p_queue text, p_claim_token_hash text, p_visibility_seconds integer DEFAULT 60
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    it work_item%ROWTYPE;
    v  queue_verdict;
    v_attempt_id text;
    v_no  integer;
    v_hist integer;
    v_vis integer;
BEGIN
    v.ok := false;

    IF p_claim_token_hash IS NULL OR length(p_claim_token_hash) <> 64 THEN
        PERFORM _queue_audit(NULL, NULL, 'claim', 'rejected', 'bad_claim_token_hash');
        v.reason := 'bad_claim_token_hash';
        RETURN v;
    END IF;

    v_vis := least(greatest(coalesce(p_visibility_seconds, 60), 1), 3600);

    SELECT * INTO it FROM work_item
     WHERE queue = p_queue AND state = 'ready' AND available_at <= now()
     ORDER BY priority DESC, available_at, created_at
     FOR UPDATE SKIP LOCKED LIMIT 1;

    IF NOT FOUND THEN
        v.reason := 'no_work';
        RETURN v;
    END IF;

    SELECT COALESCE(MAX(attempt_no), 0) INTO v_hist
      FROM work_attempt
     WHERE work_item_id = it.work_item_id;
    v_no := GREATEST(it.attempt_count, v_hist) + 1;

    -- Fail closed: an item that already spent its budget must never be handed
    -- out again. The failure and expiry paths dead-letter such items, so
    -- reaching here means an upstream invariant broke.
    IF v_no > it.max_attempts THEN
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               dead_reason = 'attempt_budget_exhausted', dead_at = now(),
               updated_at = now()
         WHERE work_item_id = it.work_item_id;
        PERFORM _queue_audit(it.work_item_id, NULL, 'claim', 'rejected',
                             'attempt_budget_exhausted');
        v.reason := 'attempt_budget_exhausted';
        RETURN v;
    END IF;

    v_attempt_id := _queue_new_id('wat');

    INSERT INTO work_attempt (
        attempt_id, work_item_id, attempt_no, claimed_by_role, claim_token_hash,
        visibility_seconds, visible_until, state, created_at, updated_at)
    VALUES (v_attempt_id, it.work_item_id, v_no, session_user, p_claim_token_hash,
            v_vis, now() + make_interval(secs => v_vis), 'active', now(), now());

    UPDATE work_item
       SET state = 'claimed', attempt_count = v_no,
           current_attempt_id = v_attempt_id, updated_at = now()
     WHERE work_item_id = it.work_item_id
       AND state = 'ready';

    IF NOT FOUND THEN
        PERFORM _queue_audit(it.work_item_id, v_attempt_id, 'claim', 'rejected',
                             'lost_claim_race');
        v.reason := 'lost_claim_race';
        RETURN v;
    END IF;

    PERFORM _queue_audit(it.work_item_id, v_attempt_id, 'claim', 'granted', NULL,
                         jsonb_build_object('attempt_no', v_no,
                                            'visibility_seconds', v_vis));

    v.ok := true;
    v.work_item_id := it.work_item_id;
    v.attempt_id := v_attempt_id;
    v.attempt_no := v_no;
    v.visible_until := now() + make_interval(secs => v_vis);
    v.payload := it.payload;
    RETURN v;
END
$$;
