-- 0029: an exhausted ACCOUNT is not a broken HOST, and it keeps a different
-- clock (VOYN-MON-CONTROL-01-QUEUE-EXECUTOR-QUOTA-EXHAUSTED).
--
-- 0022 gave writer-lease contention its own bounded budget and 0024 gave
-- host/launcher/provider infrastructure failures theirs, each with its own cap
-- "because lease contention and infrastructure recovery have different caps
-- and different operator signals" (0024). Executor CAPACITY is the third such
-- class and it was being charged to the second one, whose clock is far too
-- short for it:
--
--   * `infra_wait_count` allows 20 refusals, and every requeue waits
--     `_queue_backoff(retry_backoff_seconds, waits)` -- doubling from 2 s,
--     capped at 300 s. Twenty of those is 4110 s: one hour and eight minutes
--     of wall time. That is the right size for a launcher socket, a missing
--     CLI or a refused sandbox, all of which a host recovery cures in
--     minutes.
--   * The fleet's model capacity is two subscription pools that close on the
--     PROVIDER's schedule: the Claude Max credential's 5-hour rolling cap and
--     the codex account's weekly one (live: out of quota 2026-09-14 .. 09-19,
--     four days, with the review cascade's two paid links measured exhausted
--     on the same day). No amount of asking every five minutes shortens that
--     window.
--
-- So every item in flight during a capacity window spent all 20 waits in the
-- first hour of it and was dead-lettered as
-- `infra_wait_exhausted: ... (provider/auth/quota)` -- work with nothing
-- wrong with it, destroyed while an account refilled, and the hourly
-- `executor_quota_exhausted` finding on `control-01:queue` is the count of
-- those deaths. (A dead item is not merely delayed: nothing claims it, the
-- backlog task it carries is returned or parked, and only an operator's
-- `queue_redrive` brings it back.)
--
-- This family keeps 0022/0024's shape -- own column, own cap, own audit
-- reasons, attempt refunded atomically on BOTH branches per 0028 -- and
-- changes only the clock: the delay starts at 15 minutes and grows to an
-- hour, because a pool that is empty now is not refilled in 300 seconds and
-- polling it is pure waste. 24 waits is then ~22.75 hours of patience, so an
-- item survives every rolling window the fleet actually meets, and a
-- capacity outage that outlasts a FULL DAY still ends in a dead letter the
-- monitor reports -- a day with no capacity at all is an operator's problem,
-- not a work item's.
--
-- The infra-wait budget is deliberately NOT touched by a capacity wait: the
-- two measure different substrates, and spending one on the other is exactly
-- the conflation this migration exists to end.
ALTER TABLE work_item
    ADD COLUMN capacity_wait_count integer NOT NULL DEFAULT 0;

ALTER TABLE work_item
    ADD CONSTRAINT work_item_capacity_wait_bounded CHECK (capacity_wait_count >= 0);

-- The capacity clock: 15 minutes, doubling to a 1-hour ceiling. Separate from
-- `_queue_backoff` rather than a parameter of it, because that function's
-- 300-second cap is load-bearing for every OTHER retry in the protocol (a
-- genuine task failure must come back promptly) and widening it there would
-- slow all of them down.
CREATE FUNCTION _queue_capacity_backoff(p_waits integer) RETURNS interval
    LANGUAGE sql IMMUTABLE AS $$
    SELECT make_interval(secs => least(
        900::numeric * power(2, greatest(p_waits - 1, 0)), 3600
    )::double precision)
$$;

CREATE FUNCTION queue_fail_capacity_wait(
    p_attempt_id text, p_claim_token text, p_reason text,
    p_max_capacity_waits integer DEFAULT 24
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o record; v queue_verdict; i work_item%ROWTYPE;
        v_waits integer; v_cap integer; v_refunded integer;
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

    v_cap := greatest(p_max_capacity_waits, 1);
    v_waits := i.capacity_wait_count + 1;

    IF v_waits > v_cap THEN
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               attempt_count = greatest(work_item.attempt_count - 1, 0),
               capacity_wait_count = v_waits,
               dead_reason = 'capacity_wait_exhausted: ' || coalesce(p_reason, 'unspecified'),
               dead_at = now(), updated_at = now()
         WHERE work_item_id = o.work_item_id
         RETURNING attempt_count INTO v_refunded;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'capacity_wait_dead_lettered',
                             jsonb_build_object('capacity_wait_count', v_waits,
                                                'max_capacity_waits', v_cap,
                                                'attempt_count', v_refunded));
        v.reason := 'capacity_wait_dead_lettered';
    ELSE
        UPDATE work_item
           SET state = 'ready', current_attempt_id = NULL,
               attempt_count = greatest(work_item.attempt_count - 1, 0),
               capacity_wait_count = v_waits,
               available_at = now() + _queue_capacity_backoff(v_waits),
               updated_at = now()
         WHERE work_item_id = o.work_item_id
         RETURNING attempt_count INTO v_refunded;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'capacity_wait_requeued',
                             jsonb_build_object('capacity_wait_count', v_waits,
                                                'max_capacity_waits', v_cap,
                                                'attempt_count', v_refunded));
        v.reason := 'capacity_wait_requeued';
    END IF;

    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;

-- 0027's rule, extended to the third family: a redrive is the operator's
-- audited assertion that what the fleet was doing to the item has stopped, so
-- every wait budget is cleared and the cleared counts travel in the audit.
-- The attempt history still is not reset (0002).
CREATE OR REPLACE FUNCTION queue_redrive(p_work_item_id text, p_extra_attempts integer DEFAULT 1)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE i work_item%ROWTYPE;
BEGIN
    SELECT * INTO i FROM work_item WHERE work_item_id = p_work_item_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _queue_audit(NULL, NULL, 'redrive', 'rejected', 'unknown_work_item',
                             jsonb_build_object('requested_work_item_id', p_work_item_id));
        RETURN false;
    END IF;
    IF i.state <> 'dead' THEN
        PERFORM _queue_audit(p_work_item_id, NULL, 'redrive', 'rejected', 'not_dead_lettered',
                             jsonb_build_object('state', i.state));
        RETURN false;
    END IF;
    UPDATE work_item
       SET state = 'ready', dead_reason = NULL, dead_at = NULL,
           max_attempts = max_attempts + greatest(p_extra_attempts, 1),
           lease_wait_count = 0,
           infra_wait_count = 0,
           capacity_wait_count = 0,
           available_at = now(), updated_at = now()
     WHERE work_item_id = p_work_item_id;
    PERFORM _queue_audit(p_work_item_id, NULL, 'redrive', 'granted', NULL,
                         jsonb_build_object('extra_attempts', greatest(p_extra_attempts, 1),
                                            'previous_dead_reason', i.dead_reason,
                                            'cleared_lease_wait_count', i.lease_wait_count,
                                            'cleared_infra_wait_count', i.infra_wait_count,
                                            'cleared_capacity_wait_count', i.capacity_wait_count));
    RETURN true;
END
$$;
