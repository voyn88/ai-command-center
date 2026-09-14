-- 0025: a claim numbers the DELIVERY, and spends the BUDGET. They stopped
-- being the same number the moment a refusal could be refunded
-- (VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH).
--
-- 0002 derived the new attempt's number from the budget column:
-- `v_no := attempt_count + 1`, inserted as `work_attempt.attempt_no` under
-- `UNIQUE (work_item_id, attempt_no)`, and written straight back as
-- `attempt_count = v_no`. One counter served two purposes, which was exact
-- while the only way to move an item was forward.
--
-- 0022 introduced the first backward move: `queue_fail_lease_wait` refunds
-- the attempt a no-fault refusal was handed. `attempt_count` therefore
-- returns to the number the JUST-FAILED attempt already holds, and the next
-- `queue_claim` computes that same `attempt_no` again --
--
--     duplicate key value violates unique constraint "idx_work_attempt_item_no"
--     DETAIL: Key (work_item_id, attempt_no)=(wki_..., 1) already exists.
--
-- -- inside the SECURITY DEFINER function, so the exception leaves
-- `queue_claim` and the claiming worker instead of a verdict. The daemon
-- calls `claim()` from its main loop (`worker.daemon.run_forever`), so the
-- lane dies and systemd restarts it; the item is still `ready` and still
-- the highest-priority row, so the next claim -- by that lane or any other
-- -- picks the same row and raises again. One refunded item stops the whole
-- fleet from claiming anything, and no dead letter, stall or growth number
-- says why.
--
-- Reachable since 0022 (a publish that lost the writer-lease race) and
-- ordinary since the worker change that routed EVERY no-fault refusal
-- through the refund: an executor no host can offer, a provider past its
-- cap, a workspace the fleet could not provision. The refund is the fix
-- control-01:queue's dead-letter growth needed; this is the collision that
-- fix walks into on the very next claim.
--
-- So the two numbers separate, each taken from the thing it actually
-- measures:
--
--   * the DELIVERY number is `max(attempt_no) + 1` over the item's own
--     attempt history -- the sequence the unique index constrains, read
--     from the table that holds it, under the item's row lock. It cannot
--     collide with a row that exists, whatever any counter was refunded to,
--     and it stays the value the executor cascade selects its link from
--     (`worker.handlers._cascade_step` wraps, so a climbing number walks the
--     cascade again -- a refunded quota refusal now escalates to the next
--     link's separate account instead of re-running the exhausted one);
--
--   * the BUDGET is `attempt_count`, incremented by one per delivery and
--     decremented by a refund, tested against `max_attempts` exactly as
--     before. It is what `queue_fail`, `queue_reap` and the fail-closed
--     refusal below all mean by "this item spent its budget".
--
-- Without a refund in play the two are equal and every existing behaviour is
-- unchanged, which is why this is a repair of one expression rather than a
-- new protocol.
CREATE OR REPLACE FUNCTION queue_claim(
    p_queue text, p_claim_token_hash text, p_visibility_seconds integer DEFAULT 60
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    it work_item%ROWTYPE;
    v  queue_verdict;
    v_attempt_id text;
    v_no  integer;
    v_spent integer;
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

    -- The budget this delivery spends, and the number it is delivered under.
    -- Equal until something refunds; see this migration's header for what
    -- separates them and why the delivery number comes from the attempt
    -- history rather than from the budget. The read is covered by the item's
    -- row lock taken above, so no concurrent claim can insert between it and
    -- the INSERT below.
    v_spent := it.attempt_count + 1;
    SELECT coalesce(max(attempt_no), 0) + 1 INTO v_no
      FROM work_attempt WHERE work_item_id = it.work_item_id;

    -- Fail closed: an item that already spent its budget must never be handed
    -- out again. The failure and expiry paths dead-letter such items, so
    -- reaching here means an upstream invariant broke.
    IF v_spent > it.max_attempts THEN
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
       SET state = 'claimed', attempt_count = v_spent,
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

    -- Both numbers travel into the audit: a refunded item's delivery number
    -- runs ahead of its spent budget, and an operator reading the trail for
    -- "why did this item dead-letter" needs to see which of the two moved.
    PERFORM _queue_audit(it.work_item_id, v_attempt_id, 'claim', 'granted', NULL,
                         jsonb_build_object('attempt_no', v_no,
                                            'attempt_count', v_spent,
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
