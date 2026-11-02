-- 0041: the reap's re-test under the lock dropped half the predicate it was
-- re-testing (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED).
--
-- `queue_reap` selects lapsed leases and then, under each item's row lock,
-- re-tests the attempt before expiring it. 0002 wrote that re-test as
--
--     CONTINUE WHEN NOT EXISTS (
--         SELECT 1 FROM work_attempt
--          WHERE attempt_id = r.attempt_id AND state = 'active');
--
-- with the comment "a completion may have landed since the scan". The scan's
-- own predicate is TWO conditions -- `state = 'active' AND visible_until <=
-- now()` -- and the re-test carries only the first. So it catches the attempt
-- that FINISHED since the scan and misses the attempt that was RENEWED since
-- the scan, which is the other thing that can happen to a live claim in that
-- window and the only one `queue_heartbeat` produces: a beat moves
-- `visible_until` and never touches `state`.
--
-- WHY THERE IS A WINDOW AT ALL. The scan is one statement, so `work_attempt`
-- is read once, at the transaction's snapshot; the re-test is a later
-- statement in the same READ COMMITTED transaction, so it sees whatever has
-- COMMITTED since. Between them sits the rest of the loop -- up to
-- `WorkQueueAdmin.REAP_BATCH` items, each taking a row lock, two UPDATEs and
-- an audit INSERT. A heartbeat that committed anywhere in there is invisible
-- to the scan and visible to the re-test, and the re-test waves it through.
--
-- 0040's `SKIP LOCKED` narrowed this and could not close it. While the beat's
-- transaction is open it holds the item's row `FOR UPDATE` (`_queue_owns`), so
-- a reap arriving THEN steps past the item and defers it -- correct. The
-- window that remains is the beat that has already COMMITTED and released:
-- the reap finds the row free, finds the attempt `active`, and expires a lease
-- with time left on it.
--
-- MEASURED AGAINST A REAL POSTGRESQL 16 SERVER, with the beat's transaction
-- pinned open across the expiry instant (`transaction_timestamp()` freezes
-- there, which is what makes it a beat that raced its own deadline) and the
-- reap blocked mid-loop on another attempt's row:
--
--     queue_heartbeat -> (ok = true)          lease renewed, one hour of it left
--     queue_reap      -> 2
--     beating attempt: state = expired, visible_until > now()
--     beating item:    state = ready,   current_attempt_id = NULL
--
-- An attempt marked `expired` while its lease is still in the future, and an
-- item handed back to the queue out from under the lane that was running it.
--
-- WHAT IT COSTS, and none of it is the reap's to give away:
--
--   * THE RUN IS LOST. The lane is still executing. Its next beat, and then
--     its `queue_complete`, resolve through `_queue_owns` to
--     `attempt_superseded` (`current_attempt_id` no longer names it), so
--     `worker.daemon._execute` discards the outcome -- up to one whole
--     `TimeoutStopSec=3660s` attempt, thrown away at the finish line.
--   * THE SIDE EFFECTS RE-RUN. The item is `ready` again and any lane may
--     take it, including while the first one is still working. The protocol's
--     one-owner promise is intact on paper -- the fence refuses the loser's
--     WRITE -- but the work itself was already done twice.
--   * THE BUDGET IS SPENT. `queue_claim` charged `attempt_count` for the
--     delivery that just got taken away; `queue_fail_lease_wait` is the only
--     refund and nothing on this path calls it. Repeat it enough and a
--     perfectly healthy item reaches `max_attempts` and dead-letters
--     (`dead_letter_growth`), reachable after that only by an operator's
--     `queue_redrive`.
--
-- AND IT IS THE RECOVERY PATH DOING IT, which is why it is filed here. 0040
-- gave the reaper liveness because `lapsed_claim_age_seconds` is the one
-- starvation class `infra_monitor.evaluate` neither excuses by capacity nor
-- bounds by the fleet clock -- "only the reaper" clears it. A reaper that
-- also expires LIVE claims is not merely failing to recover; it is minting
-- the class it exists to drain, one attended claim at a time, and every
-- occurrence pushes an item back onto the due-ready pile the same probe
-- weighs.
--
-- WHEN IT FIRES. The beat has to straddle its own deadline: begun while the
-- lease was live, committed after it lapsed. Beats normally run at a third of
-- the window and renew 200s clear of expiry, so this is the fleet coming BACK
-- from a stall -- a database blip or a `voyn-aicc-pgtunnel.service` restart
-- that cost two beats, with the third landing right on the deadline. That is
-- precisely the moment the lane is recoverable and the reap is most likely to
-- be running, and it is the moment this turns a recovery into a lost attempt.
--
-- THE FIX: re-test the predicate the scan used, both halves of it. One clock
-- for the whole tick -- `now()` is the transaction timestamp, the same value
-- the scan compared against -- so "renewed past this tick" is the test, not
-- "renewed past this instant". An attempt that lapses DURING a reap is
-- therefore left to the next tick rather than raced, which is the same answer
-- 0040 gives a contended row and for the same reason: recovery that is one
-- minute late costs a minute, recovery that is wrong costs an attempt.
--
-- CREATE OR REPLACE on the bounded arity only. `queue_reap()` is one line over
-- this body (0040) and inherits the fix without being re-declared; both keep
-- 0002's EXECUTE grants, their SECURITY DEFINER and their `search_path` pin.
-- REBASED (merge of main into #979): this branch's 0029 (now 0041) were written
-- over 0002's reap body. Main has since replaced that body twice -- 0036
-- (a lapse refunds its attempt and spends the lease-WAIT budget instead) and
-- 0037 (that budget is spent only while the fleet is serving). Re-applying
-- the old body would silently revert both, so the bounded arity below is
-- 0037's body with exactly this migration's change on top.
--
CREATE OR REPLACE FUNCTION queue_reap(p_max_items integer) RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; i work_item%ROWTYPE; n integer := 0;
        v_waits integer; v_refunded integer; v_serving boolean;
        -- The lease-WAIT cap (0037): equal to `queue_fail_lease_wait`'s
        -- `p_max_lease_waits` default, which is what the worker passes. Not
        -- to be confused with `v_bound`, the per-call ITEM bound below.
        v_cap constant integer := 20;
        v_bound integer;
BEGIN
    -- NULL means "no bound", which is what the no-argument form passes. A
    -- non-positive bound clamps to one item, so a tick always makes progress
    -- (0040).
    v_bound := CASE WHEN p_max_items IS NULL THEN NULL
                    ELSE greatest(p_max_items, 1) END;

    FOR r IN
        SELECT a.attempt_id, a.work_item_id FROM work_attempt a
         WHERE a.state = 'active' AND a.visible_until <= now()
         ORDER BY a.visible_until
    LOOP
        EXIT WHEN v_bound IS NOT NULL AND n >= v_bound;

        -- SKIP LOCKED, not a plain FOR UPDATE (0040). A row somebody else is
        -- holding is deferred to the next tick rather than allowed to stop the
        -- recovery of every item behind it.
        SELECT * INTO i FROM work_item
         WHERE work_item_id = r.work_item_id FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            -- Not silent. `work_item_id` is NULL and the id travels in the
            -- detail because this branch does NOT hold the row lock that
            -- `_queue_audit` requires of a caller naming an item (0039).
            PERFORM _queue_audit(NULL, r.attempt_id, 'expire', 'rejected',
                                 'item_locked',
                                 jsonb_build_object('deferred_work_item_id',
                                                    r.work_item_id));
            CONTINUE;
        END IF;

        -- RE-TEST THE WHOLE PREDICATE THE SCAN USED (0041): the attempt may
        -- have FINISHED (state) or been RENEWED (visible_until) since the
        -- scan. `now()` is the scan's own clock, so an attempt that lapses
        -- while this reap runs is next tick's work. See the header.
        CONTINUE WHEN NOT EXISTS (
            SELECT 1 FROM work_attempt
             WHERE attempt_id = r.attempt_id
               AND state = 'active'
               AND visible_until <= now());

        UPDATE work_attempt SET state = 'expired', outcome_reason = 'visibility_timeout',
               updated_at = now()
         WHERE attempt_id = r.attempt_id;

        v_waits := i.lease_wait_count + 1;
        -- Refund the attempt `queue_claim` spent to make THIS delivery, on
        -- both branches (0028 draws the same conclusion for the refusal that
        -- exhausts the budget as for the ones before it). Never below zero.
        --
        -- Conditional on the attempt still being the item's current one.
        -- Today it always is -- every terminal path writes the attempt's
        -- state, so an 'active' attempt implies a 'claimed' item pointing at
        -- it -- and NO TEST PINS THE FALSE BRANCH, because none can without
        -- forging a row. It is written this way so that if an orphaned
        -- 'active' attempt ever did appear (`queue_claim`'s own
        -- `lost_claim_race` leaves one behind on a CAS it documents as
        -- unreachable), the refund cannot decrement a budget that belongs to
        -- a different, live delivery. Deciding whose attempt to refund by
        -- guessing is exactly the accounting error this migration exists to
        -- stop making.
        v_refunded := CASE
            WHEN i.current_attempt_id IS NOT DISTINCT FROM r.attempt_id
                THEN greatest(i.attempt_count - 1, 0)
            ELSE i.attempt_count
        END;
        v_serving := _queue_fleet_is_serving(i.queue, r.work_item_id);

        IF v_waits > v_cap AND v_serving THEN
            -- The WAIT budget exhausted while the fleet was serving its
            -- siblings, not the attempt budget: lease lapses kept recurring
            -- on THIS item past what is worth waiting out, so whatever is
            -- wrong is not curing itself. The item never spent a model
            -- attempt on any of them, and `work_dlq` reports `attempt_count`
            -- accordingly to the operator deciding whether to redrive.
            UPDATE work_item
               SET state = 'dead', current_attempt_id = NULL,
                   attempt_count = v_refunded,
                   lease_wait_count = v_waits,
                   dead_reason = 'lease_wait_exhausted: visibility_timeout',
                   dead_at = now(), updated_at = now()
             WHERE work_item_id = r.work_item_id;
            PERFORM _queue_audit(r.work_item_id, r.attempt_id, 'expire', 'granted',
                                 'dead_lettered',
                                 jsonb_build_object('lease_wait_count', v_waits,
                                                    'max_lease_waits', v_cap,
                                                    'attempt_count', v_refunded,
                                                    'fleet_is_serving', v_serving));
        ELSE
            -- Backed off by the WAIT count, not by `attempt_count`: the
            -- refund keeps the latter flat, so reading it here would hand a
            -- host that is failing every delivery the same short retry
            -- forever. The queue's own cap (300s) still bounds it, and it is
            -- what keeps a fleet-wide outage from becoming a poll loop now
            -- that exhausting the count no longer ends the item.
            UPDATE work_item
               SET state = 'ready', current_attempt_id = NULL,
                   attempt_count = v_refunded,
                   lease_wait_count = v_waits,
                   available_at = now() + _queue_backoff(i.retry_backoff_seconds, v_waits),
                   updated_at = now()
             WHERE work_item_id = r.work_item_id;
            PERFORM _queue_audit(r.work_item_id, r.attempt_id, 'expire', 'granted',
                                 'requeued',
                                 jsonb_build_object('lease_wait_count', v_waits,
                                                    'max_lease_waits', v_cap,
                                                    'attempt_count', v_refunded,
                                                    'fleet_is_serving', v_serving));
        END IF;
        n := n + 1;
    END LOOP;
    RETURN n;
END
$$;
