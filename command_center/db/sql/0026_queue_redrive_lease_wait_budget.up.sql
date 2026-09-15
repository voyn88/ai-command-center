-- 0026: the DLQ's exit did not work for the dead-letter class 0022 created
-- (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED).
--
-- 0002 built `queue_redrive` as "the DLQ's exit" when an item had exactly ONE
-- budget. Its contract is the comment it still carries: the attempt history is
-- not reset, `attempt_count` keeps counting, and the budget is WIDENED
-- explicitly so that each widening is a recorded act rather than a silent
-- grant of infinite retries.
--
-- 0022 GAVE work_item A SECOND BUDGET AND A SECOND WAY TO DIE, AND `queue_
-- redrive` WAS NOT TOLD -- the same omission 0025 fixed in `queue_claim`, in
-- the same family, from the same migration. `lease_wait_count` bounds writer
-- -lease contention against `p_max_lease_waits` (20 in the deployed worker),
-- and exhausting it dead-letters with `lease_wait_exhausted`. `queue_redrive`
-- widens `max_attempts` and never touches `lease_wait_count`.
--
-- So for an item dead-lettered by contention, redrive is a NO-OP THAT REPORTS
-- SUCCESS. Measured against a real PostgreSQL 16 server, cap 2 for brevity:
--
--     lease waits 1,2,3            -> dead, lease_wait_exhausted, count = 2
--     queue_redrive(item, 3)       -> true, state = ready, max_attempts 3 -> 6
--     ONE further lease refusal    -> dead, lease_wait_exhausted, count = 2
--
-- It returns true, it audits 'granted', it moves the row to `ready`, and it
-- widens the one budget the item is not dying of. The count is already at the
-- cap, so the very next refusal -- the exact condition that dead-lettered it,
-- and the one most likely to still be true moments later -- kills it again.
-- The operator sees a successful redrive and an item back in the DLQ.
--
-- WHOSE WORK THIS STRANDS. 0022 exists because `lease_unavailable` names no
-- fault in the work itself: another lane won the race for a repository's
-- single writer-lease row, and the item's own commit is finished and merely
-- unpublished. `backlog_dispatch` bounds concurrency by per-repository writer
-- leases across three repositories with two lanes, so sustained contention is
-- a state this fleet produces by design. An item that rides it to the cap is
-- FINISHED WORK, and until now the queue had no way to let it out.
--
-- THE FIX: a redrive restores BOTH budgets, because it is already the audited
-- operator act the one-budget design reserved for exactly this. Unconditional
-- rather than only for `lease_wait_exhausted` items: an item dead-lettered on
-- `max_attempts` can carry a nearly spent `lease_wait_count` from earlier
-- contention, and redriving it into a handful of refusals before the widened
-- attempts are ever tried is the same defect wearing a different dead_reason.
--
-- It does not weaken the rule it is written under. `lease_wait_count` counts
-- contention against ONE delivery of the work, not a property of the work, and
-- resetting it grants no retry that is not also a recorded act: every redrive
-- is audited, and the previous count travels in the audit beside the previous
-- dead_reason, so a redrive loop is as visible here as the `max_attempts`
-- widening it sits next to. The attempt HISTORY is still not reset --
-- `work_attempt` is untouched, and 0025 numbers the next delivery from it.
--
-- CREATE OR REPLACE, so 0002's EXECUTE grants survive: the signature, the
-- SECURITY DEFINER and the `search_path` pin are re-declared identically.
CREATE OR REPLACE FUNCTION queue_redrive(p_work_item_id text, p_extra_attempts integer DEFAULT 1)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE i work_item%ROWTYPE;
BEGIN
    SELECT * INTO i FROM work_item WHERE work_item_id = p_work_item_id FOR UPDATE;
    IF NOT FOUND THEN
        -- The audit's `work_item_id` is a foreign key, so naming an item that
        -- does not exist would make the AUDIT raise — turning a refusal into an
        -- aborted transaction and losing the very record of it. The unknown id
        -- goes into `detail` instead, where nothing references it.
        PERFORM _queue_audit(NULL, NULL, 'redrive', 'rejected', 'unknown_work_item',
                             jsonb_build_object('requested_work_item_id', p_work_item_id));
        RETURN false;
    END IF;
    IF i.state <> 'dead' THEN
        PERFORM _queue_audit(p_work_item_id, NULL, 'redrive', 'rejected', 'not_dead_lettered',
                             jsonb_build_object('state', i.state));
        RETURN false;
    END IF;
    -- The attempt history is NOT reset. `attempt_count` keeps counting and the
    -- budget is widened explicitly, so a redrive loop cannot silently grant
    -- infinite retries — each widening is a recorded act.
    --
    -- `lease_wait_count` is the OTHER budget (0022), and it is restored rather
    -- than widened: it has no stored ceiling to widen, because the cap is the
    -- caller's `p_max_lease_waits`. Leaving it at that cap is what made this
    -- function a no-op for every item dead-lettered as `lease_wait_exhausted`.
    UPDATE work_item
       SET state = 'ready', dead_reason = NULL, dead_at = NULL,
           max_attempts = max_attempts + greatest(p_extra_attempts, 1),
           lease_wait_count = 0,
           available_at = now(), updated_at = now()
     WHERE work_item_id = p_work_item_id;
    PERFORM _queue_audit(p_work_item_id, NULL, 'redrive', 'granted', NULL,
                         jsonb_build_object('extra_attempts', greatest(p_extra_attempts, 1),
                                            'previous_dead_reason', i.dead_reason,
                                            'previous_lease_wait_count',
                                            i.lease_wait_count));
    RETURN true;
END
$$;
