-- backlog_mark_duplicate: OPEN -> DECIDED for a finding confirmed duplicate
-- after triage (VOYN-W0-AICC-BGE-M3-DEDUP-SCAN).
--
-- backlog_triage (0008) only reaches DECIDED from UNTRIAGED — by design, a
-- raw finding's "this is a duplicate" verdict is part of triage. But the
-- bge-m3 dedup scan runs against the LIVE backlog (OPEN / IN_PROGRESS /
-- DEFER_TO_USER), so a real duplicate it finds is already OPEN, past
-- triage, and backlog_triage's own gate ("not_untriaged") refuses it. There
-- was no legal SQL path from OPEN to DECIDED for a duplicate discovered
-- this way — the same class of gap backlog_triage closed for UNTRIAGED.
--
-- backlog_mark_duplicate closes it, narrowly: OPEN only (not the linear
-- machine's IN_PROGRESS/READY_TO_REVIEW — a duplicate found mid-flight is a
-- different, larger decision this function does not make), and only with a
-- canonical task named and verified to exist, recorded in `duplicate_of` so
-- the decision is queryable, not just prose in an audit blob.
--
-- Ordering fixed after review (PR #785, rejected): existence of p_task_id
-- MUST be confirmed before anything is audited. backlog_event.task_id is
-- FK-constrained against backlog_task, so an audit row under an unknown
-- task_id raises a foreign-key violation instead of returning a verdict —
-- exactly the crash the rejected PR's canonical_required/self_duplicate
-- checks could trigger by auditing p_task_id before confirming it exists.
-- Every early-return here either (a) has already confirmed p_task_id
-- exists, or (b) audits with NULL task_id, matching the unknown_task
-- idiom backlog_transition (0005) and backlog_triage (0008) already use.
ALTER TABLE backlog_task ADD COLUMN duplicate_of text REFERENCES backlog_task(task_id);

CREATE FUNCTION backlog_mark_duplicate(
    p_task_id            text,
    p_canonical_task_id  text,
    p_detail             text DEFAULT NULL
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    t backlog_task%ROWTYPE;
    v backlog_verdict;
    v_first  text;
    v_second text;
BEGIN
    v.ok := false;

    -- Lock both endpoints, in a deterministic (alphabetical) order, before
    -- reading either row: two concurrent calls that name each other
    -- (A duplicate-of B, B duplicate-of A) must not be able to deadlock by
    -- acquiring FOR UPDATE in opposite orders.
    IF p_canonical_task_id IS NOT NULL AND p_canonical_task_id <> p_task_id THEN
        v_first  := LEAST(p_task_id, p_canonical_task_id);
        v_second := GREATEST(p_task_id, p_canonical_task_id);
        PERFORM 1 FROM backlog_task WHERE task_id = v_first FOR UPDATE;
        PERFORM 1 FROM backlog_task WHERE task_id = v_second FOR UPDATE;
    ELSE
        PERFORM 1 FROM backlog_task WHERE task_id = p_task_id FOR UPDATE;
    END IF;

    -- Existence first, before any audit: see the header note above.
    SELECT * INTO t FROM backlog_task WHERE task_id = p_task_id;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'mark_duplicate', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;

    IF p_canonical_task_id IS NULL THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected', 'canonical_required');
        v.reason := 'canonical_required'; v.revision := t.revision;
        RETURN v;
    END IF;

    IF p_canonical_task_id = p_task_id THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected', 'self_duplicate');
        v.reason := 'self_duplicate'; v.revision := t.revision;
        RETURN v;
    END IF;

    IF NOT EXISTS (SELECT 1 FROM backlog_task WHERE task_id = p_canonical_task_id) THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected', 'unknown_canonical',
                               jsonb_build_object('requested_canonical', p_canonical_task_id));
        v.reason := 'unknown_canonical'; v.revision := t.revision;
        RETURN v;
    END IF;

    IF t.status <> 'OPEN' THEN
        -- Only OPEN: a duplicate found while IN_PROGRESS/READY_TO_REVIEW is
        -- a bigger decision (abandon in-flight work) than this function
        -- makes; a duplicate found already DECIDED/DONE is a no-op refused
        -- as data, not silently re-applied.
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected', 'not_open',
                               jsonb_build_object('status', t.status));
        v.reason := 'not_open: ' || t.status; v.revision := t.revision;
        RETURN v;
    END IF;

    UPDATE backlog_task
       SET status = 'DECIDED', duplicate_of = p_canonical_task_id,
           revision = revision + 1, updated_at = clock_timestamp()
     WHERE task_id = p_task_id
    RETURNING revision INTO v.revision;

    PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'granted', 'duplicate',
                           jsonb_build_object('canonical', p_canonical_task_id, 'detail', p_detail));
    v.ok := true;
    RETURN v;
END
$$;

REVOKE ALL ON FUNCTION backlog_mark_duplicate(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION backlog_mark_duplicate(text, text, text) TO aicc_app;
