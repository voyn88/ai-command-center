-- Restore the 0002 claim: one counter serves as both the delivery number and
-- the spent budget (`attempt_count + 1`, written back as `attempt_count`).
-- Correct only while nothing refunds an attempt -- with `queue_fail_lease_wait`
-- (0022) in place, the claim after a refund re-uses an `attempt_no` the item's
-- history already holds and raises a unique-violation out of the function. Down
-- migrations restore the previous behaviour, including its defects.
CREATE OR REPLACE FUNCTION queue_claim(
    p_queue text, p_claim_token_hash text, p_visibility_seconds integer DEFAULT 60
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    it work_item%ROWTYPE;
    v  queue_verdict;
    v_attempt_id text;
    v_no  integer;
    v_vis integer;
BEGIN
    v.ok := false;

    IF p_claim_token_hash IS NULL OR length(p_claim_token_hash) <> 64 THEN
        PERFORM _queue_audit(NULL, NULL, 'claim', 'rejected', 'bad_claim_token_hash');
        v.reason := 'bad_claim_token_hash';
        RETURN v;
    END IF;

    v_vis := least(greatest(coalesce(p_visibility_seconds, 60), 1), 3600);

    -- THE MUTUAL EXCLUSION, stated as measurement rather than as intent.
    --
    -- What produces it is the ROW LOCK plus the `state = 'ready'` PREDICATE, and
    -- it is PostgreSQL's rather than this design's. Under READ COMMITTED a
    -- claimer that waits on a locked row re-evaluates the predicate once the
    -- lock is released; the winner has since set `state = 'claimed'`, so the row
    -- no longer matches and the waiter gets no row at all.
    --
    -- The predicate is the guard the suite pins: widening it to
    -- `state IN ('ready','claimed')` fails three tests, including both
    -- concurrency ones.
    --
    -- `SKIP LOCKED` BUYS THROUGHPUT, NOT CORRECTNESS — measured, and the
    -- correction of an earlier claim in this file that said otherwise. Replacing
    -- it with a plain `FOR UPDATE` leaves the whole suite green, because the
    -- re-evaluation above is what refuses the loser either way. What changes is
    -- that claimers serialise behind each other instead of stepping past a held
    -- row, so one slow claim transaction stalls every other claimer.
    -- `test_a_claimer_is_never_blocked_by_another_transactions_row_lock` pins
    -- that property, and pins it as liveness rather than dressing it up as
    -- exclusivity.
    --
    -- The two remaining guards are defence in depth, and neither is pinned by a
    -- concurrency test because neither can be made to fire while the row lock
    -- holds — see the `state = 'ready'` CAS on the UPDATE below, and
    -- UNIQUE(work_item_id, attempt_no) on `work_attempt`. Saying so is the
    -- point: a comment claiming a guard is test-covered when it is not makes a
    -- refactor that deletes the guard land green.
    SELECT * INTO it FROM work_item
     WHERE queue = p_queue AND state = 'ready' AND available_at <= now()
     ORDER BY priority DESC, available_at, created_at
     FOR UPDATE SKIP LOCKED LIMIT 1;

    IF NOT FOUND THEN
        v.reason := 'no_work';
        RETURN v;
    END IF;

    v_no := it.attempt_count + 1;

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
       -- CAS, defence in depth. Unreachable today and deliberately kept: the
       -- row is locked by the SELECT above, so nothing can change its state
       -- between the two statements and this can never be false. NO TEST PINS
       -- IT, and none can without removing the lock. It is here so that a future
       -- change dropping the lock fails closed (`lost_claim_race` below)
       -- instead of handing one item to two workers.
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
