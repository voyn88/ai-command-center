-- 0024: a redrive must actually redrive an item the FLEET dead-lettered
-- (VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH).
--
-- 0022 gave no-fault refusals their own bounded budget (`lease_wait_count`,
-- 20 waits) so contention could not spend the item's `max_attempts`. It did
-- not teach `queue_redrive` about that budget, and while exactly one call
-- site set `lease_wait=True` (a publish that lost the writer-lease race)
-- that omission was very nearly unreachable: 20 lost lease races on one
-- item is pathological.
--
-- The worker change that accompanies this migration routes EVERY no-fault
-- refusal that way -- an executor no host can offer, a provider past its
-- cap, a workspace the fleet could not provision -- so reaching the cap is
-- now an ordinary consequence of a capacity outage that outlasts it. That
-- makes the omission a live dead end: `queue_redrive` widens `max_attempts`
-- and clears `dead_reason`, but leaves `lease_wait_count` above the cap, so
-- the item comes back `ready` and the very first no-fault refusal
-- re-dead-letters it on the same exhausted budget. The DLQ's exit becomes a
-- no-op for precisely the items the fleet, not the work, put there -- and
-- the re-deaths are counted again as the dead-letter growth control-01
-- measures.
--
-- So a redrive resets the wait budget, and only the wait budget. The
-- asymmetry with `attempt_count` (which 0002 deliberately does NOT reset) is
-- the point, and it follows from what each counter measures: `attempt_count`
-- is what this ITEM has consumed, so persisting it is what stops a redrive
-- loop from silently granting infinite retries. `lease_wait_count` is what
-- the FLEET was doing to it -- and a redrive is an explicit, audited
-- assertion that those conditions have changed. Carrying that number across
-- the operator's decision answers a question nobody asked.
--
-- This does not restore an unbounded retry: the reset is reachable only
-- through `queue_redrive`, which is control-plane/operator-only, refuses
-- anything not already `dead`, and audits every grant. The number it clears
-- travels into that audit row so the history survives the reset.
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
    -- `lease_wait_count` IS reset (0024): it counts what the fleet did to this
    -- item, never what the item spent, and leaving it at the cap would hand
    -- back an item the next no-fault refusal immediately re-dead-letters.
    UPDATE work_item
       SET state = 'ready', dead_reason = NULL, dead_at = NULL,
           max_attempts = max_attempts + greatest(p_extra_attempts, 1),
           lease_wait_count = 0,
           available_at = now(), updated_at = now()
     WHERE work_item_id = p_work_item_id;
    PERFORM _queue_audit(p_work_item_id, NULL, 'redrive', 'granted', NULL,
                         jsonb_build_object('extra_attempts', greatest(p_extra_attempts, 1),
                                            'previous_dead_reason', i.dead_reason,
                                            'cleared_lease_wait_count', i.lease_wait_count));
    RETURN true;
END
$$;
