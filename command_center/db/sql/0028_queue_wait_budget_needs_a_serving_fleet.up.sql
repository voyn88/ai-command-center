-- 0028: the no-fault wait budget must be spent against a SERVING fleet, and
-- a delivery that reaches the work must give it back
-- (VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH).
--
-- 0022/0024/0025/0026/0027 moved every refusal that names no fault in the
-- WORK off `max_attempts` and onto `lease_wait_count`: a bounded budget of 20
-- waits with the queue's capped backoff. That stopped two fleet refusals from
-- killing a task that had never reached a model. It did not stop twenty-one
-- of them, and twenty-one is not a long time.
--
-- WHAT THE BOUND ACTUALLY BUYS. `_queue_backoff(2, n)` -- 2 is the backoff
-- every dispatch enqueues with (`backlog_dispatch` -> `queue_enqueue`'s
-- default, `_review_enqueue` likewise) -- runs 2, 4, 8, 16, 32, 64, 128, 256
-- and then sits at its 300s cap. Summed over the 20 waits the budget allows:
--
--     SELECT sum(extract(epoch FROM _queue_backoff(2, n)))/60
--       FROM generate_series(1,20) n;   -->   68.5 minutes
--
-- So an item survives about an hour and nine minutes of a capacity outage,
-- and the twenty-first refusal dead-letters it under `lease_wait_exhausted`
-- with `attempt_count = 0` -- provably never delivered to a model.
--
-- WHY THAT NUMBER IS WRONG HERE, MEASURED RATHER THAN ARGUED. The fleet's
-- dominant outage is longer than its own budget by a factor of four.
-- `orchestrator.routing` records the measurement: the Claude credential is a
-- Max *subscription* with a FIVE-HOUR rolling cap, and on 2026-08-23, 142 of
-- 167 parked tasks were literally "You've hit your session limit" rather than
-- task defects. `worker.handlers` routes exactly that response -- the CLI's
-- own `is_error` + `api_error_status`/`terminal_reason` -- to `no_fault`. So
-- every item in flight when the window closes is refused every ~5 minutes for
-- five hours, spends its whole wait budget in the first 69 of those minutes,
-- and dies. Nothing redrives it: `queue_redrive` is operator-only (`python -m
-- command_center.db queue-redrive`), so the work is not delayed, it is lost.
-- `ops.infra_monitor` counts the arrivals as `dead_letter_growth`, and its
-- `executor_quota_exhausted` class -- which buckets a DLQ row whose
-- `dead_reason` matches quota/session limit/rate limit -- exists because
-- these are the rows it kept seeing.
--
-- WHAT THE BUDGET IS FOR, WHICH IS NOT THIS. Both its own migrations say:
-- 0022 bounds "permanent, PATHOLOGICAL contention" so a refusal that never
-- clears cannot retry forever, and 0027 bounds the item "that somehow kills
-- its worker every time it is delivered". Both are statements about THIS ITEM
-- BEING SINGLED OUT. A subscription window that refuses every lane equally is
-- not a poison pill, and a count of polls is not a duration: the same outage
-- kills an item or not depending on how often it happened to be offered.
--
-- THE TEST THAT SEPARATES THEM IS ALREADY IN THE DATABASE. "Is the fleet
-- serving other work?" -- the same question `infra_monitor` asks as
-- `recent_succeeded` / `throughput_stalled`. `_queue_fleet_is_serving` reads
-- it from the audit trail: a `complete`/`granted` event, or a `fail`/`granted`
-- one whose reason is `requeued` or `dead_lettered` (`queue_fail`'s two
-- vocabularies -- a delivery that got past the fleet and returned a verdict
-- about the WORK). Refusals are deliberately not evidence: the lease-wait
-- exits audit `lease_wait_requeued`/`lease_wait_dead_lettered` and the reaper
-- audits under `expire`, so a queue in which nothing but refusals is
-- happening reads as a fleet that is not serving -- which is what it is.
--
-- So the wait budget spends only while the fleet is demonstrably serving
-- OTHER items, and 20 means again what 0022 wrote it to mean: twenty refusals
-- this item collected while its siblings were being served. During a total
-- outage the count still climbs (the backoff is computed from it, and an
-- outage must not turn into a 2-second poll loop) and the audit still records
-- every refusal -- what changes is only that exhausting it does not
-- dead-letter an item the fleet was refusing along with everything else. The
-- backlog then stays `ready` at the 300s cap, which costs one claim per item
-- per five minutes, and the outage surfaces through `infra_monitor`'s
-- `queue_stalled` and `throughput_stalled:0_succeeded_in_1h` -- the classes
-- that name a fleet that has stopped serving. A stall keeps the work and
-- names the fleet; a dead letter destroys the work and blames the item.
--
-- THE TRADE, STATED. A queue holding ONE item that poisons every worker it
-- touches has no sibling to compare against, so it now cycles at the 300s cap
-- instead of dead-lettering after 20 deliveries. That is deliberate: with no
-- other work in the queue, nothing distinguishes "this item is poison" from
-- "the fleet is down", the monitor reports the stall either way, and
-- dead-lettering would not have repaired the host. Fail closed means keep the
-- work.
--
-- SECOND HALF: A DELIVERY THAT REACHES THE WORK REFUNDS THE WAIT BUDGET.
-- `lease_wait_count` was cumulative over the item's entire life and reset by
-- nothing but `queue_redrive` (0024). An item that waited out a long outage
-- carries that number forever, so one ordinary lease race weeks later -- the
-- 21st wait of its lifetime -- dead-letters it. `queue_fail`'s requeue is the
-- one non-terminal path that means a delivery got past the fleet and reported
-- on the work itself, so it clears the wait budget: 20 consecutive refusals,
-- not 20 spread across the months an item may live. This cannot become an
-- unbounded retry, because reaching that reset costs a real `attempt_count`
-- and nothing refunds those -- `max_attempts` (the cascade length) is the
-- ceiling on how many times the wait budget can be cleared.

-- The events that mean a delivery REACHED THE WORK, indexed as their own
-- small set. `EXISTS` short-circuits on the first match, so the cost that
-- matters is the NO-match case -- which is exactly the outage, and exactly
-- when the queue is producing refusals fastest (one claim and one refusal per
-- item per five minutes, all of them written to `work_event`). Against
-- `idx_work_event_created_at` alone that case reads every event in the window
-- to conclude nothing qualifies; against this partial index it reads none,
-- because during an outage the index is empty.
CREATE INDEX IF NOT EXISTS idx_work_event_reached_the_work
    ON work_event (created_at)
 WHERE outcome = 'granted'
   AND (event = 'complete'
        OR (event = 'fail' AND reason IN ('requeued', 'dead_lettered')));

-- Is the fleet serving work other than this item? Evidence, not inference:
-- one delivery, to any OTHER item of the same queue, that got past the fleet
-- and returned a verdict about the work within the window. The predicate is
-- written to match the partial index above exactly; changing one without the
-- other silently gives the scan back.
CREATE OR REPLACE FUNCTION _queue_fleet_is_serving(
    p_queue text, p_except_work_item text, p_window interval DEFAULT interval '1 hour'
) RETURNS boolean
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT EXISTS (
        SELECT 1
          FROM work_event e
          JOIN work_item i ON i.work_item_id = e.work_item_id
         WHERE e.created_at > now() - p_window
           AND e.outcome = 'granted'
           AND (e.event = 'complete'
                OR (e.event = 'fail' AND e.reason IN ('requeued', 'dead_lettered')))
           AND i.queue = p_queue
           AND e.work_item_id IS DISTINCT FROM p_except_work_item
    )
$$;

CREATE OR REPLACE FUNCTION queue_fail_lease_wait(
    p_attempt_id text, p_claim_token text, p_reason text,
    p_max_lease_waits integer DEFAULT 20
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o record; v queue_verdict; i work_item%ROWTYPE;
        v_waits integer; v_cap integer; v_refunded integer; v_serving boolean;
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

    v_cap := greatest(p_max_lease_waits, 1);
    v_waits := i.lease_wait_count + 1;
    -- Refund the attempt this claim spent, on BOTH branches: a refusal that
    -- names no fault in the work is not evidence the WORK is bad, and that is
    -- as true of the refusal that exhausts the wait budget as of the ones
    -- before it. The delivery it was handed under keeps its `attempt_no` and
    -- its `work_attempt` row either way -- the history is not what is being
    -- refunded (see 0025: the delivery number and the budget are separate
    -- numbers taken from separate places).
    v_refunded := greatest(i.attempt_count - 1, 0);
    -- 0028: exhausting the budget is a verdict only if the fleet was serving
    -- other items while this one was refused. Otherwise the refusal is the
    -- outage, not the item, and the wait goes on being a wait.
    v_serving := _queue_fleet_is_serving(i.queue, i.work_item_id);

    IF v_waits > v_cap AND v_serving THEN
        -- The lease-wait budget exhausted while siblings were being served:
        -- contention or a host-local absence kept singling THIS item out past
        -- what is worth waiting out. Dead-letter with a distinct cause so an
        -- operator (or an automated redrive) can tell this apart from a work
        -- item whose own attempts kept failing.
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               attempt_count = v_refunded,
               lease_wait_count = v_waits,
               dead_reason = 'lease_wait_exhausted: ' || coalesce(p_reason, 'unspecified'),
               dead_at = now(), updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'lease_wait_dead_lettered',
                             jsonb_build_object('lease_wait_count', v_waits,
                                                'max_lease_waits', v_cap,
                                                'attempt_count', v_refunded,
                                                'fleet_is_serving', v_serving));
        v.reason := 'lease_wait_dead_lettered';
    ELSE
        UPDATE work_item
           SET state = 'ready', current_attempt_id = NULL,
               attempt_count = v_refunded,
               lease_wait_count = v_waits,
               available_at = now() + _queue_backoff(i.retry_backoff_seconds, v_waits),
               updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'lease_wait_requeued',
                             jsonb_build_object('lease_wait_count', v_waits,
                                                'max_lease_waits', v_cap,
                                                'attempt_count', v_refunded,
                                                'fleet_is_serving', v_serving));
        v.reason := 'lease_wait_requeued';
    END IF;

    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;

-- The reaper reaches the same boundary without a handler to report it (0027),
-- so it takes the same gate. A lapsed lease during a total outage is the most
-- literal case of all: the host that was holding the item died or was
-- restarted while nothing in the queue was being served.
CREATE OR REPLACE FUNCTION queue_reap() RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; i work_item%ROWTYPE; n integer := 0;
        v_waits integer; v_refunded integer; v_serving boolean;
        -- `queue_reap()` takes no arguments and must not start taking any:
        -- it is granted and called by that exact signature (`roles.py`,
        -- `db.cli queue-reap`, `aicc-queue-reaper.service`), and adding a
        -- defaulted parameter would OVERLOAD it rather than replace it,
        -- leaving the existing zero-argument call ambiguous. The cap is
        -- therefore a constant here, equal to `queue_fail_lease_wait`'s
        -- `p_max_lease_waits` default -- which is the value the worker
        -- actually passes (`work_queue_store.fail_lease_wait`). One fleet
        -- budget, one number.
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

-- `queue_fail`'s requeue is the one non-terminal exit that means a delivery
-- got past the fleet and came back with a verdict about the WORK. It
-- therefore clears the wait budget -- see this migration's header: 20 waits
-- is a statement about one outage, not an item's lifetime ledger. Everything
-- else about this function is 0002 unchanged, including the deliberate
-- refusal to reset `attempt_count`.
CREATE OR REPLACE FUNCTION queue_fail(
    p_attempt_id text, p_claim_token text, p_reason text,
    p_retryable boolean DEFAULT true
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o record; v queue_verdict; i work_item%ROWTYPE;
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

    IF NOT p_retryable OR i.attempt_count >= i.max_attempts THEN
        -- DEAD LETTER. The cause is preserved on the item and every attempt
        -- that led there is preserved beside it; `work_dlq` exposes both.
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               dead_reason = CASE WHEN NOT p_retryable
                    THEN 'non_retryable: ' || coalesce(p_reason, 'unspecified')
                    ELSE 'max_attempts_exhausted: ' || coalesce(p_reason, 'unspecified') END,
               dead_at = now(), updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'dead_lettered',
                             jsonb_build_object('attempt_count', i.attempt_count,
                                                'max_attempts', i.max_attempts,
                                                'retryable', p_retryable));
        v.reason := 'dead_lettered';
    ELSE
        UPDATE work_item
           SET state = 'ready', current_attempt_id = NULL,
               lease_wait_count = 0,
               available_at = now() + _queue_backoff(i.retry_backoff_seconds, i.attempt_count),
               updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted', 'requeued',
                             jsonb_build_object('attempt_count', i.attempt_count,
                                                'max_attempts', i.max_attempts,
                                                'cleared_lease_wait_count',
                                                i.lease_wait_count));
        v.reason := 'requeued';
    END IF;

    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;
