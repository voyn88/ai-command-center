-- 0028: the queue's recovery path had no liveness guarantee
-- (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED).
--
-- `queue_reap()` is the ONLY thing that clears a lapsed claim, and a lapsed
-- claim is the one starvation class `command_center/ops/infra_monitor.py`
-- neither gates on capacity nor bounds by the fleet clock:
--
--     starved_ages = [queue.lapsed_claim_age_seconds]
--     if spare_capacity: starved_ages.append(ready_due_starved)
--
-- and its own comment says why -- "A lapsed claim is starved at any capacity:
-- no lane is holding it, so no lane being free is irrelevant to it ... only
-- the reaper does, and the lapse age is what measures whether
-- `aicc-queue-reaper.timer` (every minute) is still recovering." So a reaper
-- that stops recovering is `queue_stalled` with NO EXIT REACHABLE BY FLEET
-- ACTION: restarting a lane does not reap, and `queue_redrive` only reaches
-- items that are already `dead`. The probe is measuring the reaper, and until
-- now nothing guaranteed the reaper could answer.
--
-- 0002 SHIPPED THE SAFETY PROPERTY AND CALLED IT THE WHOLE PROPERTY. Its
-- header states the row lock as the reason a reap "cannot race a concurrent
-- complete()", and `WorkQueueAdmin.reap`'s docstring repeats it as the reason
-- "a missed tick delays recovery and never corrupts it". Both are true about
-- CORRECTNESS. Neither is a statement about PROGRESS, and `queue_claim`'s own
-- comment two hundred lines above draws exactly that distinction for the claim
-- path:
--
--     `SKIP LOCKED` BUYS THROUGHPUT, NOT CORRECTNESS ... What changes is that
--     claimers serialise behind each other instead of stepping past a held
--     row, so one slow claim transaction stalls every other claimer.
--     `test_a_claimer_is_never_blocked_by_another_transactions_row_lock` pins
--     that property, and pins it as liveness rather than dressing it up as
--     exclusivity.
--
-- The CLAIM path has that liveness property and a test pinning it. The
-- RECOVERY path -- the only exit from the stall class nothing else can clear
-- -- had neither, and it is the path where being stuck is unbounded rather
-- than merely slow.
--
-- MEASURED AGAINST A REAL POSTGRESQL 16 SERVER. Three items claimed, three
-- leases lapsed, one of the three items' rows held by another transaction
-- (any `_queue_owns` caller, a duplicate `queue_enqueue` since 0027, a
-- `queue_redrive`, a migration's ALTER, an operator's psql):
--
--     reap interrupted: canceling statement due to statement timeout
--     CONTEXT: while locking tuple (0,4)
--     recovered by the interrupted reap: 0 of 3
--
-- BOTH halves of that are the defect, and the second is the worse one:
--
--   * It WAITED on the held row instead of stepping past it. One contended
--     item is enough to stop the recovery of every other item, including the
--     two nothing was holding.
--   * It is ALL OR NOTHING. The first item had already been expired and
--     requeued before the block; the interrupted transaction took that back
--     with it. An interrupted tick recovers NOTHING, not "everything up to
--     where it stopped" -- so the work it did is not merely delayed, it is
--     discarded, and the next tick starts from the same place.
--
-- AND THE TICK IS INTERRUPTIBLE BY DESIGN. `aicc-queue-reaper.service` is a
-- `Type=oneshot` with `TimeoutStartSec=60s`, so systemd kills a tick that
-- runs long; the connection crosses `voyn-aicc-pgtunnel.service`, which the
-- credential rotation restarts; and the scan is over every expired attempt in
-- the table with no bound at all. Each of those turns a reap into a rollback,
-- and every rollback returns the fleet to the state that produced it.
--
-- THE FIX, in the two pieces that make recovery monotonic:
--
--   1. `FOR UPDATE SKIP LOCKED` on the item, exactly as `queue_claim` takes
--      it. A contended item is DEFERRED to the next tick (60 seconds) instead
--      of stopping this one. Correctness is untouched: the re-test under the
--      lock already handles "somebody else finished this attempt", and
--      whoever holds the row is by definition one of the parties that
--      resolves it -- a completion, a claim, a redrive. Skipping is how the
--      reaper waits for them without making every other item wait too.
--
--      The deferral is AUDITED rather than silent, so an item that is
--      contended tick after tick is visible as a record instead of as a
--      number that quietly fails to grow. The audit passes a NULL
--      `work_item_id` and carries the id in `detail`, because `_queue_audit`
--      requires the caller to hold the item's row lock (0027) and not holding
--      it is the whole reason this branch exists -- the same reason and the
--      same shape as `queue_redrive`'s unknown-item branch.
--
--   2. A BOUNDED BATCH. `queue_reap(p_max_items)` stops after that many
--      expirations so one tick's transaction is bounded by a number the
--      caller chose rather than by the size of the table, and
--      `WorkQueueAdmin.reap` loops batches on its autocommit connection --
--      so each batch is DURABLE before the next one starts. An interruption
--      now costs at most one batch instead of the whole backlog.
--
-- WHAT THIS DOES NOT CLAIM. It is not the cause of any one `queue_stalled`
-- reading: a reaper that is never interrupted and never contended behaves
-- exactly as before, and this branch's earlier commits removed the causes
-- that were producing the finding. It is why the lapsed-claim half of that
-- finding had no reliable exit -- the recovery path could lose a tick's work
-- to a fault that had nothing to do with the items it was recovering, and
-- nothing on the host would ever have said so.
--
-- WHY AN OVERLOAD AND NOT A NEW PARAMETER, and this is 0024's lesson applied:
-- `CREATE OR REPLACE` cannot add a parameter, so `p_max_items integer
-- DEFAULT NULL` would define a SECOND function and make the no-argument call
-- AMBIGUOUS -- breaking every existing caller. `queue_reap()` therefore keeps
-- its exact signature (and with it 0002's EXECUTE grant), and delegates to
-- the bounded form with no bound. Control hosts and the worker host deploy
-- independently, so a caller still running pre-0028 code keeps working AND
-- gets the liveness fix, because the fix lives in the body both arities share.
CREATE FUNCTION queue_reap(p_max_items integer) RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; i work_item%ROWTYPE; n integer := 0; v_cap integer;
BEGIN
    -- NULL means "no bound", which is what the no-argument form passes. A
    -- non-positive bound is a caller error that must not mean "reap nothing
    -- for ever"; it clamps to one item, so a tick always makes progress --
    -- the same `greatest(..., 1)` convention `queue_claim` applies to its own
    -- visibility clamp and `evaluate` to `--claim-capacity`.
    v_cap := CASE WHEN p_max_items IS NULL THEN NULL
                  ELSE greatest(p_max_items, 1) END;

    FOR r IN
        SELECT a.attempt_id, a.work_item_id FROM work_attempt a
         WHERE a.state = 'active' AND a.visible_until <= now()
         ORDER BY a.visible_until
    LOOP
        EXIT WHEN v_cap IS NOT NULL AND n >= v_cap;

        -- SKIP LOCKED, not a plain FOR UPDATE: see the header. A row somebody
        -- else is holding is deferred to the next tick rather than allowed to
        -- stop the recovery of every item behind it.
        SELECT * INTO i FROM work_item
         WHERE work_item_id = r.work_item_id FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            -- Not silent. `work_item_id` is NULL and the id travels in the
            -- detail because this branch does NOT hold the row lock that
            -- `_queue_audit` requires of a caller naming an item (0027) --
            -- the same shape, for the same reason, as `queue_redrive`'s
            -- unknown-item branch.
            PERFORM _queue_audit(NULL, r.attempt_id, 'expire', 'rejected',
                                 'item_locked',
                                 jsonb_build_object('deferred_work_item_id',
                                                    r.work_item_id));
            CONTINUE;
        END IF;

        -- Re-test under the lock: a completion may have landed since the scan.
        CONTINUE WHEN NOT EXISTS (
            SELECT 1 FROM work_attempt WHERE attempt_id = r.attempt_id AND state = 'active');

        UPDATE work_attempt SET state = 'expired', outcome_reason = 'visibility_timeout',
               updated_at = now()
         WHERE attempt_id = r.attempt_id;

        IF i.attempt_count >= i.max_attempts THEN
            UPDATE work_item
               SET state = 'dead', current_attempt_id = NULL,
                   dead_reason = 'visibility_timeout_exhausted',
                   dead_at = now(), updated_at = now()
             WHERE work_item_id = r.work_item_id;
            PERFORM _queue_audit(r.work_item_id, r.attempt_id, 'expire', 'granted',
                                 'dead_lettered');
        ELSE
            UPDATE work_item
               SET state = 'ready', current_attempt_id = NULL,
                   available_at = now() + _queue_backoff(i.retry_backoff_seconds,
                                                         i.attempt_count),
                   updated_at = now()
             WHERE work_item_id = r.work_item_id;
            PERFORM _queue_audit(r.work_item_id, r.attempt_id, 'expire', 'granted',
                                 'requeued');
        END IF;
        n := n + 1;
    END LOOP;
    RETURN n;
END
$$;

-- Same grantee as the arity it joins (0002): recovery is an `aicc_app`
-- privilege and deliberately not a worker one, so a compromised worker host
-- cannot expire the fleet's leases.
GRANT EXECUTE ON FUNCTION queue_reap(integer) TO aicc_app;

-- The no-argument form keeps its exact signature -- and with it 0002's
-- EXECUTE grant, its SECURITY DEFINER and its `search_path` pin -- and
-- becomes one line over the shared body. Every existing caller therefore gets
-- the liveness fix without being redeployed, which is the point of keeping
-- the arity at all.
CREATE OR REPLACE FUNCTION queue_reap() RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    RETURN queue_reap(NULL::integer);
END
$$;
