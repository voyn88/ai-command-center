-- One-shot exact-head reviews invalidated by a merge-tick branch update.
CREATE TABLE backlog_review_priority (
    task_id text NOT NULL REFERENCES backlog_task(task_id),
    pr_url text NOT NULL CHECK (length(pr_url) > 0),
    expected_head text NOT NULL CHECK (expected_head ~ '^[0-9a-f]{40}$'),
    queued_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, pr_url)
);

CREATE FUNCTION backlog_review_priority_mark(
    p_task_id text, p_pr_url text, p_expected_head text
) RETURNS backlog_verdict
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'review_priority', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task'; RETURN v;
    END IF;
    IF p_pr_url IS NULL OR length(p_pr_url) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'review_priority', 'rejected', 'empty_value');
        v.reason := 'empty_value'; v.revision := t.revision; RETURN v;
    END IF;
    IF p_expected_head IS NULL OR p_expected_head !~ '^[0-9a-f]{40}$' THEN
        PERFORM _backlog_audit(p_task_id, 'review_priority', 'rejected', 'invalid_head');
        v.reason := 'invalid_head'; v.revision := t.revision; RETURN v;
    END IF;
    INSERT INTO backlog_review_priority (task_id, pr_url, expected_head)
    VALUES (p_task_id, p_pr_url, p_expected_head)
    ON CONFLICT (task_id, pr_url) DO UPDATE
       SET expected_head = EXCLUDED.expected_head, queued_at = now();
    PERFORM _backlog_audit(p_task_id, 'review_priority', 'granted', 'marked',
                           jsonb_build_object('pr', p_pr_url, 'head', p_expected_head));
    v.ok := true; v.reason := 'marked'; v.revision := t.revision; RETURN v;
END
$$;

CREATE FUNCTION backlog_review_priority_pop(p_limit integer)
RETURNS TABLE (task_id text, pr_url text, expected_head text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    RETURN QUERY
    -- Ordering the limited selection does not order DELETE's RETURNING rows.
    -- Preserve FIFO (with primary-key tie-breakers) in the returned batch too.
    WITH popped AS (
        DELETE FROM backlog_review_priority p
         WHERE p.ctid IN (
            SELECT r.ctid FROM backlog_review_priority r
             ORDER BY r.queued_at, r.task_id, r.pr_url
             LIMIT greatest(p_limit, 0)
         )
        RETURNING p.task_id, p.pr_url, p.expected_head, p.queued_at
    )
    SELECT q.task_id, q.pr_url, q.expected_head FROM popped q
     ORDER BY q.queued_at, q.task_id, q.pr_url;
END
$$;

REVOKE ALL ON FUNCTION backlog_review_priority_mark(text, text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION backlog_review_priority_pop(integer) FROM PUBLIC;
REVOKE ALL ON backlog_review_priority FROM PUBLIC;
