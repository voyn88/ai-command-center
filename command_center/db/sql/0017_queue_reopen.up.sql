-- 0017_queue_reopen
--
-- VOYN-W0-AICC-REVIEW-STUCK-ON-TRANSIENT-FAILURE.
--
-- A `succeeded` work_item is not always a completed task. 0002_queue_claim's
-- outcome discipline (see `command_center/worker/handlers.py`) means a run
-- that formally EXECUTED reports ok=true through `queue_complete` even when
-- the agent's own output is unusable -- a rate-limit/auth/overload message
-- the executor classifier does not recognise (and no classifier can
-- enumerate every future failure text; live incident 2026-08-21 16:09 UTC
-- predates the classifier branch that now catches that one specific shape).
-- `publish_review_verdicts` already refuses to read a verdict out of such a
-- result (`_parse_verdict` -> None), which is correct -- but because the
-- review-cycle key is deterministic on (task, pr, head_sha, policy_version),
-- and `queue_enqueue` no-ops on an existing row of ANY state, nothing
-- re-queues the work while the head sha stays put. The item sits
-- `succeeded` forever, outside `work_dlq` (`WHERE state = 'dead'`), so
-- `queue_redrive()` -- gated on exactly that state -- cannot reach it
-- either. Silent skip, every tick, until a new commit changes the key.
--
-- `queue_reopen()` is the missing exit: the operator-invoked counterpart of
-- `queue_redrive()`, for a `succeeded` item instead of a `dead` one. It
-- requires an explicit, non-empty reason -- this is a human override of a
-- real acknowledgement, not an automatic recovery, so unlike `queue_fail`/
-- `queue_reap` nothing calls it on a timer. `result_id` is cleared:
-- `work_item_succeeded_has_result` is a biconditional
-- (`(state = 'succeeded') = (result_id IS NOT NULL)`), so a `ready` row must
-- carry no result, exactly like a fresh or redriven-dead one. The stale
-- result row itself is untouched in `work_result` -- `idx_work_result_item`
-- still finds it by `work_item_id` for anyone auditing what the transient
-- failure actually said -- only the item's CURRENT pointer moves.
-- `max_attempts` is widened the same explicit, audited way `queue_redrive`
-- widens it, so a reopen loop cannot grant infinite retries silently.

CREATE FUNCTION queue_reopen(
    p_work_item_id text, p_reason text, p_extra_attempts integer DEFAULT 1
) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE i work_item%ROWTYPE;
BEGIN
    SELECT * INTO i FROM work_item WHERE work_item_id = p_work_item_id FOR UPDATE;
    IF NOT FOUND THEN
        -- Same rule as queue_redrive: the unknown id goes into `detail`,
        -- never into the audit row's own FK column, or the refusal would
        -- abort the transaction meant to record it.
        PERFORM _queue_audit(NULL, NULL, 'reopen', 'rejected', 'unknown_work_item',
                             jsonb_build_object('requested_work_item_id', p_work_item_id));
        RETURN false;
    END IF;
    IF i.state <> 'succeeded' THEN
        PERFORM _queue_audit(p_work_item_id, NULL, 'reopen', 'rejected', 'not_succeeded',
                             jsonb_build_object('state', i.state));
        RETURN false;
    END IF;
    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        -- A reopen overrides a real acknowledgement; it must say why.
        PERFORM _queue_audit(p_work_item_id, NULL, 'reopen', 'rejected', 'reason_required');
        RETURN false;
    END IF;

    UPDATE work_item
       SET state = 'ready', result_id = NULL,
           max_attempts = max_attempts + greatest(p_extra_attempts, 1),
           available_at = now(), updated_at = now()
     WHERE work_item_id = p_work_item_id;

    PERFORM _queue_audit(p_work_item_id, NULL, 'reopen', 'granted', p_reason,
                         jsonb_build_object('extra_attempts', greatest(p_extra_attempts, 1),
                                            'previous_result_id', i.result_id));
    RETURN true;
END
$$;
