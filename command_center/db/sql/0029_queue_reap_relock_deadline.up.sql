-- 0029: the reap's re-test under the lock dropped half the predicate it was
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
-- 0028's `SKIP LOCKED` narrowed this and could not close it. While the beat's
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
-- AND IT IS THE RECOVERY PATH DOING IT, which is why it is filed here. 0028
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
-- 0028 gives a contended row and for the same reason: recovery that is one
-- minute late costs a minute, recovery that is wrong costs an attempt.
--
-- CREATE OR REPLACE on the bounded arity only. `queue_reap()` is one line over
-- this body (0028) and inherits the fix without being re-declared; both keep
-- 0002's EXECUTE grants, their SECURITY DEFINER and their `search_path` pin.
CREATE OR REPLACE FUNCTION queue_reap(p_max_items integer) RETURNS integer
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

        -- SKIP LOCKED, not a plain FOR UPDATE (0028). A row somebody else is
        -- holding is deferred to the next tick rather than allowed to stop the
        -- recovery of every item behind it.
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

        -- RE-TEST THE WHOLE PREDICATE THE SCAN USED, not just its first half
        -- (0029). The scan read `work_attempt` at this transaction's snapshot;
        -- this statement reads it again, so it sees every commit the rest of
        -- the loop has given other sessions time to make. TWO of them matter
        -- and 0002 checked for only one:
        --
        --   * the attempt FINISHED -- `queue_complete`/`queue_fail` moved
        --     `state` off 'active', which the `state` test catches;
        --   * the attempt was RENEWED -- `queue_heartbeat` moved
        --     `visible_until` forward and left `state` exactly where it was,
        --     which the `state` test does not catch at all.
        --
        -- Without the deadline half, a beat that straddled its own expiry
        -- (begun live, committed lapsed) had its lease expired anyway: the
        -- lane's run is discarded as `attempt_superseded`, its side effects
        -- re-run under the next delivery, and the attempt `queue_claim`
        -- charged for it is gone. See this migration's header for the
        -- measurement.
        --
        -- `now()` and not `clock_timestamp()`: one clock for one tick, the
        -- same value the scan compared against, so an attempt that lapses
        -- WHILE this reap runs is next tick's work rather than a row to race.
        -- Consistency rather than a load-bearing difference -- a lease is
        -- renewed by a whole `visibility_seconds` and a tick runs for
        -- milliseconds, so no renewal lands between the two clocks, and the
        -- regression test cannot tell them apart. It is written this way so a
        -- reader does not have to work out whether it could.
        CONTINUE WHEN NOT EXISTS (
            SELECT 1 FROM work_attempt
             WHERE attempt_id = r.attempt_id
               AND state = 'active'
               AND visible_until <= now());

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
