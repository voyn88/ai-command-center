-- 0026: the attempt budget is the counter, the attempt number is the history
-- (VOYN-W0-AICC-CLAIM-BUDGET-IGNORES-INFRA-WAIT-REFUND-AFTER-0025).
--
-- 0024 refunds the claimed attempt on an infrastructure wait
-- (`attempt_count - 1`, its own `infra_wait_count` budget) and 0025 made the
-- next attempt number follow the attempt history
-- (`greatest(attempt_count, max(attempt_no)) + 1`) so a lagged counter can
-- never re-insert an existing `attempt_no`. Together they cancelled 0024:
-- every infra wait leaves its `work_attempt` row behind, so after two waits
-- on a `max_attempts = 2` item the history says 2, the refunded counter says
-- 0, and the claim computed attempt 3 > 2 -> `attempt_budget_exhausted`
-- (live 2026-09-15: 89 items dead that way in 24 h, three attempts each of
-- them a launcher or OAuth failure that never ran the task).
--
-- The two numbers answer different questions and are kept apart here:
--   * `attempt_no` is an identity: monotonic over the history, never reused
--     (0025's guarantee stays; the worker-crash race of 2026-09-11 stays fixed).
--   * `attempt_count` is the budget actually consumed: incremented by one on
--     a granted claim, refunded by the lease/infra-wait paths, compared to
--     `max_attempts` here and in `queue_fail`. It may lag the history by
--     exactly the number of refunded attempts -- that lag IS the refund.
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
    v_used integer;
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

    -- Budget: what this item has actually consumed (refunds included).
    v_used := it.attempt_count;
    IF v_used + 1 > it.max_attempts THEN
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               dead_reason = 'attempt_budget_exhausted', dead_at = now(),
               updated_at = now()
         WHERE work_item_id = it.work_item_id;
        PERFORM _queue_audit(it.work_item_id, NULL, 'claim', 'rejected',
                             'attempt_budget_exhausted',
                             jsonb_build_object('attempt_count', v_used,
                                                'max_attempts', it.max_attempts));
        v.reason := 'attempt_budget_exhausted';
        RETURN v;
    END IF;

    -- Identity: one past whatever the history already holds (0025).
    SELECT COALESCE(MAX(attempt_no), 0) INTO v_hist
      FROM work_attempt
     WHERE work_item_id = it.work_item_id;
    v_no := GREATEST(v_used, v_hist) + 1;

    v_attempt_id := _queue_new_id('wat');
    INSERT INTO work_attempt (
        attempt_id, work_item_id, attempt_no, claimed_by_role, claim_token_hash,
        visibility_seconds, visible_until, state, created_at, updated_at)
    VALUES (v_attempt_id, it.work_item_id, v_no, session_user, p_claim_token_hash,
            v_vis, now() + make_interval(secs => v_vis), 'active', now(), now());

    UPDATE work_item
       SET state = 'claimed', attempt_count = v_used + 1,
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
                                            'attempt_count', v_used + 1,
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
