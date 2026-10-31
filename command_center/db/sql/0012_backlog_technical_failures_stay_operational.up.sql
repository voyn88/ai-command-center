-- Technical exhaustion is an operational retry, never an owner decision.
-- In particular, a failed publish leaves no PR and used to become
-- DEFER_TO_USER on the second cascade even though no owner action could help.
CREATE OR REPLACE FUNCTION backlog_return_to_pool(p_task_id text, p_reason text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_target text; v_prior integer;
        v_technical boolean;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'return_to_pool', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task'; RETURN v;
    END IF;
    IF t.status <> 'IN_PROGRESS' THEN
        PERFORM _backlog_audit(p_task_id, 'return_to_pool', 'rejected', 'not_in_progress',
                               jsonb_build_object('status', t.status));
        v.reason := 'not_in_progress'; v.revision := t.revision; RETURN v;
    END IF;

    SELECT count(*) INTO v_prior FROM backlog_event e
     WHERE e.task_id = p_task_id AND e.event = 'return_to_pool'
       AND e.outcome = 'granted';
    v_technical := p_reason LIKE 'cascade_exhausted: no_pr_published%'
        OR p_reason LIKE 'cascade_exhausted: task_status_failed%'
        OR p_reason LIKE 'cascade_exhausted: executor infrastructure failure%'
        OR p_reason LIKE 'cascade_exhausted: publish_%';
    v_target := CASE
        WHEN v_technical THEN 'OPEN'
        WHEN v_prior >= 1 THEN 'DEFER_TO_USER'
        ELSE 'OPEN'
    END;

    UPDATE backlog_task b
       SET status = v_target, revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'return_to_pool', 'granted', p_reason,
                           jsonb_build_object('target', v_target,
                                              'prior_returns', v_prior,
                                              'technical', v_technical));
    v.ok := true; v.reason := v_target;
    RETURN v;
END
$$;
