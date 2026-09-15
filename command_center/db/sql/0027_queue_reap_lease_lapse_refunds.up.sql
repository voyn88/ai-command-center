-- 0027: a lapsed visibility lease is a fact about the WORKER PROCESS, so it
-- must not spend the work item's attempt budget either
-- (VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH).
--
-- 0022/0024/0025/0026 drew one boundary and then closed every hole in it: a
-- refusal that names no fault in the WORK refunds the attempt `queue_claim`
-- spent and counts against the queue's own bounded `lease_wait_count` budget
-- instead of `max_attempts`. Every call site that reaches that boundary does
-- so through a `HandlerOutcome`. `queue_reap` is the one path that cannot:
-- no handler ran, nobody reported anything, and the item is recovered by a
-- timer on another host entirely.
--
-- WHY A LAPSE IS NEVER EVIDENCE ABOUT THE ITEM. The heartbeat runs BESIDE the
-- handler, not inside it (`worker.daemon._execute` starts `_heartbeat_loop`
-- on its own thread before dispatching, and renews at a third of the
-- visibility window). A handler that runs long, blocks, or hangs outright
-- therefore keeps renewing its lease and the item stays `claimed` -- which is
-- what `ops.infra_monitor` measures as `queue_stalled`, and it never reaches
-- this function. The lease can only lapse when the PROCESS stops beating:
--
--   * the cgroup OOM-killed the lane (`MemoryMax=6G` in
--     `voyn-aicc-worker@.service`) or systemd's watchdog restarted it
--     (`WatchdogSec=240s`) -- both SIGKILL, both `Restart=always`;
--   * the host rebooted, or the daemon exited on an auth failure it is
--     designed to exit on rather than retry;
--   * PostgreSQL was unreachable for a whole visibility window, so the beat
--     thread gave up and raised `lease_lost`;
--   * `daemon._execute` deliberately chose this exit. It does so in three
--     places -- an outcome it could not write, a lease already lost
--     mid-execution, a report refused as a stale owner -- and each one says
--     in its own comment that "a later delivery retries".
--
-- Every one of those is the fleet, and none of them is the payload. Yet each
-- cost the item one `attempt_count`, and the planner sets `max_attempts` to
-- the executor cascade length -- two links since copilot left the isolated
-- fleet. So TWO worker restarts dead-lettered a task that had never spent a
-- model attempt, under `visibility_timeout_exhausted`, and control-01:queue
-- measured the arrivals as `dead_letter_growth`. The three `_execute`
-- branches above promised a retry the accounting could not pay for.
--
-- So the reaper takes the exit 0022 built and 0026 finished: BOTH branches
-- refund the attempt this delivery spent, the lapse is counted against
-- `lease_wait_count`, and the dead letter comes from THAT budget -- named
-- `lease_wait_exhausted: visibility_timeout`, so the DLQ shows one class for
-- "the fleet put it here" and still says which fleet condition did it. The
-- old `visibility_timeout_exhausted` name would now be a lie: it named the
-- attempt budget, and the attempt budget is no longer what runs out here.
--
-- This is bounded, and the bound is the poison-pill guard: an item that
-- somehow kills its worker every time it is delivered still terminates in the
-- DLQ, after 20 lapses instead of 2. It is also reversible by the operator --
-- `queue_redrive` resets `lease_wait_count` (0024) and audits the number it
-- cleared.
--
-- The refund is only safe because 0025 separated the delivery number from the
-- budget: `attempt_no` is `max(attempt_no) + 1` over the item's own attempt
-- history, so a refunded `attempt_count` can no longer collide with a
-- `work_attempt` row that already exists. Before 0025 this change would have
-- raised a duplicate-key error inside `queue_claim` on the very next claim.
-- The delivery number therefore keeps climbing across lapses, which is also
-- what makes `handlers._cascade_step` walk to the next executor link rather
-- than re-running the one whose host just died.
CREATE OR REPLACE FUNCTION queue_reap() RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; i work_item%ROWTYPE; n integer := 0;
        v_waits integer; v_refunded integer;
        -- `queue_reap()` takes no arguments and must not start taking any:
        -- it is granted and called by that exact signature (`roles.py`,
        -- `db.cli queue-reap`, `aicc-queue-reaper.service`), and adding a
        -- defaulted parameter would OVERLOAD it rather than replace it,
        -- leaving the existing zero-argument call ambiguous. The cap is therefore a constant here, equal to
        -- `queue_fail_lease_wait`'s `p_max_lease_waits` default -- which is
        -- the value the worker actually passes
        -- (`work_queue_store.fail_lease_wait`). One fleet budget, one number.
        v_cap constant integer := 20;
BEGIN
    FOR r IN
        SELECT a.attempt_id, a.work_item_id FROM work_attempt a
         WHERE a.state = 'active' AND a.visible_until <= now()
         ORDER BY a.visible_until
    LOOP
        SELECT * INTO i FROM work_item WHERE work_item_id = r.work_item_id FOR UPDATE;

        -- Re-test under the lock: a completion may have landed since the scan.
        CONTINUE WHEN NOT EXISTS (
            SELECT 1 FROM work_attempt WHERE attempt_id = r.attempt_id AND state = 'active');

        UPDATE work_attempt SET state = 'expired', outcome_reason = 'visibility_timeout',
               updated_at = now()
         WHERE attempt_id = r.attempt_id;

        v_waits := i.lease_wait_count + 1;
        -- Refund the attempt `queue_claim` spent to make THIS delivery, on
        -- both branches (0026 draws the same conclusion for the refusal that
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

        IF v_waits > v_cap THEN
            -- The WAIT budget exhausted, not the attempt budget: lease lapses
            -- kept recurring past what is worth waiting out, so whatever is
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
                                                    'attempt_count', v_refunded));
        ELSE
            -- Backed off by the WAIT count, not by `attempt_count`: the
            -- refund keeps the latter flat, so reading it here would hand a
            -- host that is failing every delivery the same short retry
            -- forever. The queue's own cap (300s) still bounds it.
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
                                                    'attempt_count', v_refunded));
        END IF;
        n := n + 1;
    END LOOP;
    RETURN n;
END
$$;
