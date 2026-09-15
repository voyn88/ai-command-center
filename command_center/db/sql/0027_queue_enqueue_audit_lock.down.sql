-- Restore 0002's `queue_enqueue`, whose duplicate path resolves the existing
-- item with a bare SELECT. Going back reinstates the race the up-migration
-- describes: a re-enqueue of an in-flight key, concurrent with that item's own
-- heartbeat or with a second dispatcher, raises
-- `idx_work_event_item_seq` and aborts the caller's transaction. The down path
-- exists because every migration here has one, not because this one is safe to
-- run on a fleet whose dispatchers retry enqueues.
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
        SELECT work_item_id INTO v_id FROM work_item
         WHERE queue = p_queue AND idempotency_key = p_idempotency_key;
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
