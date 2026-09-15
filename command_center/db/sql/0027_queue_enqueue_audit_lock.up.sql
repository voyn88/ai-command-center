-- 0027: the duplicate-enqueue audit must hold the item's row lock, like every
-- other audited path does (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED).
--
-- `_queue_audit` numbers `work_event.seq` as `max(seq) + 1` over the item's own
-- events, and `idx_work_event_item_seq` (UNIQUE on `work_item_id, seq` where
-- the item is not null) enforces the gap-free per-item sequence that promise
-- rests on. The function's own comment states the precondition that makes the
-- arithmetic safe without a retry loop:
--
--     `max(seq)+1` is collision-free without a retry loop because every caller
--     passing a non-null work_item_id already holds that item's row lock,
--     taken by the FOR UPDATE or by the matching UPDATE in the same
--     transaction.
--
-- EVERY CALLER HELD IT EXCEPT ONE. `queue_claim` (both refusals and the
-- grant), `queue_reap`, `queue_redrive` and everything reached through
-- `_queue_owns` -- heartbeat, complete, fail, fail_lease_wait -- take the item
-- `FOR UPDATE` first; `queue_enqueue`'s granted path inserted the item itself
-- in the same transaction. The duplicate path resolved the existing item with
-- a BARE SELECT and audited against it holding nothing.
--
-- The audit's INSERT does take the FK's `FOR KEY SHARE` on `work_item`, and
-- that is exactly why the bug survived: KEY SHARE does not conflict with
-- itself, and it is acquired as part of the INSERT -- AFTER the `max(seq)`
-- subquery in the same statement has already been evaluated. So a duplicate
-- enqueue computes its `seq` from a snapshot, then blocks behind whoever holds
-- the item, then commits the number it worked out before that wait:
--
--     duplicate key value violates unique constraint "idx_work_event_item_seq"
--     DETAIL:  Key (work_item_id, seq)=(wki_..., 2) already exists.
--
-- REPRODUCED, not deduced -- see `test_a_duplicate_enqueue_cannot_lose_the_
-- audit_sequence_race`, which fails against this function without the lock.
-- The unlocked caller is always the one that loses, because it is the only one
-- computing a sequence nothing serialises; every other path blocks on the row
-- lock and re-reads `max(seq)` in a later statement, on a fresh snapshot.
--
-- IT IS A RACE THIS FLEET RUNS ALL DAY. The duplicate path is not the
-- exception -- it is what an idempotency key is FOR ("the dispatcher may retry
-- an enqueue after a timeout without knowing whether the first landed", 0002),
-- and `aicc-backlog-review.timer`, `aicc-backlog-merge.timer` and the planner
-- all re-enqueue keys for work that is still in flight, on their own
-- schedules. The counterparty needs no coincidence either: the worker holding
-- that item heartbeats every ~100s (`visibility_seconds / 3`), and each beat
-- is an audited, row-locked write to the very item the duplicate enqueue is
-- auditing.
--
-- WHAT IT COSTS. The exception aborts the caller's transaction, so a routine
-- re-enqueue of in-flight work fails -- and takes down whatever else that
-- dispatch tick had batched with it, none of which had anything wrong with it.
-- The retry is a refusal being RECORDED; losing the record by raising out of
-- it is the one outcome the audit exists to prevent, and `queue_redrive`'s
-- unknown-item branch already carries the same lesson in its own comment
-- ("turning a refusal into an aborted transaction and losing the very record
-- of it").
--
-- THE FIX: resolve the existing item `FOR UPDATE`, so the one caller that was
-- outside the precondition is inside it. Then two duplicate enqueues of one
-- key serialise, and the second re-reads `max(seq)` after the first commits --
-- READ COMMITTED gives the audit's INSERT its own snapshot, taken after the
-- wait rather than before it. The unique index goes back to being the backstop
-- it is documented as, rather than the thing that decides.
--
-- The lock is taken only on the path that already found a conflicting row, so
-- the granted path is untouched and enqueues of DIFFERENT keys never meet.
-- Against the item's other writers it is the same lock they take, in the same
-- place, for the same reason -- and it is held for the rest of a function that
-- does one audit and returns.
--
-- CREATE OR REPLACE, so 0002's EXECUTE grants survive: the signature, the
-- SECURITY DEFINER and the `search_path` pin are re-declared identically, and
-- only the lookup inside changes.
CREATE OR REPLACE FUNCTION queue_enqueue(
    p_queue text, p_idempotency_key text, p_payload jsonb,
    p_task_id text DEFAULT NULL, p_repository_id text DEFAULT NULL,
    p_max_attempts integer DEFAULT 3, p_priority integer DEFAULT 0,
    p_delay_seconds integer DEFAULT 0, p_backoff_seconds integer DEFAULT 2
) RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_id text;
BEGIN
    INSERT INTO work_item (
        work_item_id, queue, idempotency_key, task_id, repository_id, payload,
        priority, available_at, state, attempt_count, max_attempts,
        retry_backoff_seconds, created_at, updated_at)
    VALUES (_queue_new_id('wki'), p_queue, p_idempotency_key, p_task_id, p_repository_id,
            coalesce(p_payload, '{}'::jsonb), p_priority,
            now() + make_interval(secs => greatest(p_delay_seconds, 0)),
            'ready', 0, greatest(p_max_attempts, 1), greatest(p_backoff_seconds, 0),
            now(), now())
    ON CONFLICT (queue, idempotency_key) DO NOTHING
    RETURNING work_item_id INTO v_id;

    IF v_id IS NULL THEN
        -- FOR UPDATE: `_queue_audit` below numbers this item's next event, and
        -- that is only collision-free under the item's row lock. Every other
        -- audited path already holds it here; this one did not, and the
        -- unique index on (work_item_id, seq) refused whichever duplicate
        -- enqueue worked its number out first and committed it second.
        --
        -- It is also the wait that makes `ON CONFLICT DO NOTHING` honest: a
        -- concurrent FIRST insert of this key is speculative, so the INSERT
        -- above already waited for it to commit before reporting the
        -- conflict, and this SELECT then sees the row it conflicted with.
        SELECT work_item_id INTO v_id FROM work_item
         WHERE queue = p_queue AND idempotency_key = p_idempotency_key
           FOR UPDATE;
        PERFORM _queue_audit(v_id, NULL, 'enqueue', 'rejected',
                             'duplicate_idempotency_key',
                             jsonb_build_object('queue', p_queue));
        RETURN v_id;
    END IF;

    PERFORM _queue_audit(v_id, NULL, 'enqueue', 'granted', NULL,
                         jsonb_build_object('queue', p_queue,
                                            'max_attempts', p_max_attempts));
    RETURN v_id;
END
$$;
