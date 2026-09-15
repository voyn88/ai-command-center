-- 0025: a refunded attempt budget must not rewind the ATTEMPT NUMBER
-- (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED, monitor_finding #2840).
--
-- 0002 built `queue_claim` when one number meant two things. `attempt_no` is
-- the delivery's index in the item's attempt history -- "monotonic per item",
-- says the column's own comment, and UNIQUE(work_item_id, attempt_no) is the
-- double-claim backstop that enforces it. `attempt_count` is the BUDGET spent
-- against `max_attempts`. Every path incremented both together, so deriving
-- one from the other (`v_no := it.attempt_count + 1`) was free.
--
-- 0022 SEPARATED THEM AND `queue_claim` WAS NOT TOLD. `queue_fail_lease_wait`
-- exists precisely so a writer-lease refusal does not spend the work's budget:
-- it requeues with `attempt_count = greatest(attempt_count - 1, 0)`, undoing
-- the increment this function made. The history is deliberately NOT rewound --
-- the `work_attempt` row stays, because it is the audit trail 0002 promises
-- and `work_event` references it. So after one refund the item carries
-- `attempt_count = N-1` beside an attempt row that already holds
-- `attempt_no = N`, and the next claim recomputes `v_no = N` and inserts a
-- duplicate.
--
--     duplicate key value violates unique constraint "idx_work_attempt_item_no"
--     DETAIL:  Key (work_item_id, attempt_no)=(wki_..., 1) already exists.
--
-- WHAT THAT COSTS, which is the whole queue and not the one item. The
-- exception aborts `queue_claim`, so the item is not merely un-retried, it is
-- UNCLAIMABLE -- and `queue_claim` takes the OLDEST DUE ROW
-- (`ORDER BY priority DESC, available_at, created_at`), so the poisoned item
-- is head-of-line. Every lane's next claim selects it and raises; healthy work
-- queued behind it is never reached. `worker.daemon.run_forever` does not
-- catch it either -- it handles `QueueRefusal`, which this is not -- so the
-- exception leaves the claim loop and the lane crash-loops under systemd.
-- ONE writer-lease refusal stops the entire fleet.
--
-- And it is a refusal the fleet is BUILT to produce: `backlog_dispatch` bounds
-- concurrency by per-repository writer leases across three repositories with
-- two lanes, and 0022's own header records the live contention
-- (wki_55f316db, 2026-09-06) that motivated the refund.
--
-- THIS IS THE `queue_stalled` THE MONITOR HAS BEEN REPORTING. `control-01:
-- queue` measures due ready work that no lane is holding while the fleet clock
-- stands still -- which is exactly and truthfully this state. The probe was
-- right; the queue was stalled. That is also why the finding kept returning
-- after each measurement fix and could never be cleared by fleet action: no
-- amount of correct measuring, and no restart of any lane, can claim an item
-- whose next `attempt_no` is already taken.
--
-- THE FIX: number the delivery from the history that constrains it.
-- `attempt_no` becomes `max(attempt_no) + 1` over the item's own attempts, and
-- `attempt_count` stays the budget. The two were only ever equal by
-- coincidence of every path moving them together; 0022 ended that, and this is
-- the arithmetic catching up. It is computed under the row lock the SELECT
-- above already holds, so two claimers cannot read the same max for one item,
-- and the unique index stays exactly what its comment says it is -- a backstop
-- rather than the mechanism.
--
-- IT IS ALSO THE RECOVERY. An item already poisoned in production carries
-- `attempt_count = N-1` and a stuck `attempt_no = N`; under this function its
-- next claim takes `attempt_no = N+1` and succeeds. No data fix-up, no
-- redrive, no operator: the head-of-line item drains on the first claim after
-- deploy and the queue behind it moves again.
--
-- WHAT DOES NOT CHANGE. The cascade step is `((attempt_no - 1) % cascade_len)
-- + 1` (`worker.handlers._cascade_step`), which already tolerates an
-- `attempt_no` that climbs independently of the budget -- `queue_redrive`
-- widens `max_attempts` without resetting `attempt_count` for the same reason,
-- and `routing.py` documents the wrap as what keeps a redrive walking the
-- cascade. So a lease-wait retry advances to the next cascade link, which is
-- what a lease wait did BEFORE 0022 existed (it was a plain
-- `queue_fail(retryable => true)`, spending an attempt and climbing). 0022
-- meant to change the budget, not the routing; this restores the routing it
-- changed by accident.
--
-- CREATE OR REPLACE, so the EXECUTE grants of 0002 survive untouched: the
-- signature, the SECURITY DEFINER and the `search_path` pin are all
-- re-declared identically, and only the numbering inside changes.
CREATE OR REPLACE FUNCTION queue_claim(
    p_queue text, p_claim_token_hash text, p_visibility_seconds integer DEFAULT 60
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    it work_item%ROWTYPE;
    v  queue_verdict;
    v_attempt_id text;
    v_no  integer;
    v_spend integer;
    v_vis integer;
BEGIN
    v.ok := false;

    IF p_claim_token_hash IS NULL OR length(p_claim_token_hash) <> 64 THEN
        PERFORM _queue_audit(NULL, NULL, 'claim', 'rejected', 'bad_claim_token_hash');
        v.reason := 'bad_claim_token_hash';
        RETURN v;
    END IF;

    v_vis := least(greatest(coalesce(p_visibility_seconds, 60), 1), 3600);

    -- Unchanged from 0002: the row lock plus the `state = 'ready'` predicate
    -- is what produces mutual exclusion, and SKIP LOCKED buys throughput
    -- rather than correctness. See that file's comment for the measurements.
    SELECT * INTO it FROM work_item
     WHERE queue = p_queue AND state = 'ready' AND available_at <= now()
     ORDER BY priority DESC, available_at, created_at
     FOR UPDATE SKIP LOCKED LIMIT 1;

    IF NOT FOUND THEN
        v.reason := 'no_work';
        RETURN v;
    END IF;

    -- THE BUDGET. What this claim spends against `max_attempts`, and the only
    -- one of the two numbers `queue_fail_lease_wait` refunds.
    v_spend := it.attempt_count + 1;

    -- Fail closed: an item that already spent its budget must never be handed
    -- out again. The failure and expiry paths dead-letter such items, so
    -- reaching here means an upstream invariant broke.
    IF v_spend > it.max_attempts THEN
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

    -- THE DELIVERY NUMBER, taken from the history that constrains it rather
    -- than from the budget, which no longer tracks it. Read under the item's
    -- row lock (held since the SELECT above), so two claimers cannot see the
    -- same max for one item and UNIQUE(work_item_id, attempt_no) stays the
    -- backstop it is documented to be instead of the thing that decides.
    SELECT coalesce(max(attempt_no), 0) + 1 INTO v_no
      FROM work_attempt WHERE work_item_id = it.work_item_id;

    v_attempt_id := _queue_new_id('wat');

    INSERT INTO work_attempt (
        attempt_id, work_item_id, attempt_no, claimed_by_role, claim_token_hash,
        visibility_seconds, visible_until, state, created_at, updated_at)
    VALUES (v_attempt_id, it.work_item_id, v_no, session_user, p_claim_token_hash,
            v_vis, now() + make_interval(secs => v_vis), 'active', now(), now());

    UPDATE work_item
       SET state = 'claimed', attempt_count = v_spend,
           current_attempt_id = v_attempt_id, updated_at = now()
     WHERE work_item_id = it.work_item_id
       -- CAS, defence in depth. Unreachable while the row lock above holds;
       -- kept so that a future change dropping the lock fails closed
       -- (`lost_claim_race`) instead of handing one item to two workers.
       AND state = 'ready';

    IF NOT FOUND THEN
        PERFORM _queue_audit(it.work_item_id, v_attempt_id, 'claim', 'rejected',
                             'lost_claim_race');
        v.reason := 'lost_claim_race';
        RETURN v;
    END IF;

    -- Both numbers are audited now that they can differ: `attempt_no` is where
    -- this delivery sits in the history (and which cascade link it routes to),
    -- `attempt_count` is what is left of the budget.
    PERFORM _queue_audit(it.work_item_id, v_attempt_id, 'claim', 'granted', NULL,
                         jsonb_build_object('attempt_no', v_no,
                                            'attempt_count', v_spend,
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
