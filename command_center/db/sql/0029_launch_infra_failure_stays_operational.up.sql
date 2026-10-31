-- AICC_AGENT_LAUNCH_INFRA_FAILURE and infra_wait exhaustion are substrate
-- failures, never owner decisions. Fleet outage 2026-09-11: group-writable
-- /opt/aicc made the launcher refuse every executor; attempts burned into
-- cascade_exhausted parks instead of infra_wait refunds. 0024 refunds the
-- attempt, but when a prior generation already dead-lettered with
-- infra_wait_exhausted / AICC_AGENT_LAUNCH_INFRA_FAILURE the ingest path
-- still wrapped it as cascade_exhausted: infra_wait_exhausted: ... which
-- did NOT match 0012/0021's technical patterns (those look for
-- "cascade_exhausted: executor infrastructure failure%" at the start of
-- the suffix). Widen the technical allowlist so these return to OPEN.
-- Body otherwise matches 0021's backlog_return_to_pool.
CREATE OR REPLACE FUNCTION backlog_return_to_pool(p_task_id text, p_reason text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_target text; v_prior integer;
        v_split_requested boolean := false;
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
        OR p_reason LIKE 'cascade_exhausted: publish_%'
        OR p_reason LIKE 'cascade_exhausted: infra_wait_exhausted%'
        OR p_reason LIKE 'cascade_exhausted: %AICC_AGENT_LAUNCH_INFRA_FAILURE%';
    v_target := CASE
        WHEN v_technical THEN 'OPEN'
        -- Pipeline class: the second non-technical return asks for a split
        -- (planner dispatches decomposition), the fourth parks. Functional
        -- tasks keep the original rule (second return parks).
        WHEN t.task_class = 'pipeline' AND v_prior >= 3 THEN 'DEFER_TO_USER'
        WHEN t.task_class = 'pipeline' AND v_prior >= 1 THEN 'OPEN'
        WHEN v_prior >= 1 THEN 'DEFER_TO_USER'
        ELSE 'OPEN'
    END;
    v_split_requested := (NOT v_technical) AND t.task_class = 'pipeline'
                         AND v_prior >= 1 AND v_target = 'OPEN';

    UPDATE backlog_task b
       SET status = v_target, revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'return_to_pool', 'granted', p_reason,
                           jsonb_build_object('target', v_target,
                                              'prior_returns', v_prior,
                                              'technical', v_technical,
                                              'split_requested', v_split_requested));
    v.ok := true; v.reason := v_target;
    RETURN v;
END
$$;
