-- 0027: a redrive must actually redrive an item the FLEET dead-lettered
-- (VOYN-MON-CONTROL-01-QUEUE-DEAD-LETTER-GROWTH-C2E2267ECC; carried over from
-- fleet PR #945 and extended to the infra-wait budget of 0024).
--
-- 0022 gave no-fault refusals their own bounded budget (`lease_wait_count`)
-- and 0024 gave executor infrastructure failures theirs (`infra_wait_count`),
-- so neither class can spend the item's `max_attempts`. Neither taught
-- `queue_redrive` about those budgets: an item dead-lettered as
-- `lease_wait_exhausted` or `infra_wait_exhausted` came back `ready` with its
-- wait budget still spent, and the very next refusal of the same class
-- re-dead-lettered it at once -- a no-op redrive counted as fresh
-- dead-letter growth.
--
-- The asymmetry is deliberate: the attempt history is NOT reset (0002: that
-- is what stops a redrive loop from silently granting infinite retries),
-- while both wait budgets ARE -- they measure what the fleet was doing to
-- the item, and the redrive is the operator's audited assertion that it
-- stopped. The cleared counts travel in the audit detail.
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
           available_at = now(), updated_at = now()
     WHERE work_item_id = p_work_item_id;
    PERFORM _queue_audit(p_work_item_id, NULL, 'redrive', 'granted', NULL,
                         jsonb_build_object('extra_attempts', greatest(p_extra_attempts, 1),
                                            'previous_dead_reason', i.dead_reason,
                                            'cleared_lease_wait_count', i.lease_wait_count,
                                            'cleared_infra_wait_count', i.infra_wait_count));
    RETURN true;
END
$$;
