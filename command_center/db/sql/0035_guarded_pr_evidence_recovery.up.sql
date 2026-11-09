-- Automatic pr-evidence recovery is more conservative than the operator
-- escape hatch added in 0018.  A negative GitHub lookup may only drive one
-- retry, and never while READY_TO_REVIEW may still be settling.
CREATE FUNCTION backlog_recover_stale_ready_to_review(p_task_id text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_count integer;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN v.reason := 'unknown_task'; RETURN v; END IF;
    IF t.kind = 'gate' THEN v.reason := 'gate_is_control_record'; RETURN v; END IF;
    IF t.status <> 'READY_TO_REVIEW' THEN
        v.reason := 'not_ready_to_review'; v.revision := t.revision; RETURN v;
    END IF;
    IF EXISTS (SELECT 1 FROM backlog_evidence e
               WHERE e.task_id = p_task_id AND e.kind = 'pr') THEN
        v.reason := 'has_pr_evidence'; v.revision := t.revision; RETURN v;
    END IF;
    IF t.updated_at > now() - interval '15 minutes' THEN
        v.reason := 'recovery_grace_period'; v.revision := t.revision; RETURN v;
    END IF;
    SELECT count(*) INTO v_count FROM backlog_event e
     WHERE e.task_id = p_task_id
       AND e.event = 'recover_stale_ready_to_review' AND e.outcome = 'granted';
    IF v_count >= 1 THEN
        v.reason := 'recovery_budget_exhausted'; v.revision := t.revision; RETURN v;
    END IF;
    UPDATE backlog_task b
       SET status = 'OPEN', revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'recover_stale_ready_to_review', 'granted',
                           'no_pr_in_any_state',
                           jsonb_build_object('from', 'READY_TO_REVIEW', 'to', 'OPEN',
                                              'max_automatic_recoveries', 1));
    v.ok := true; v.reason := 'OPEN'; RETURN v;
END
$$;
REVOKE ALL ON FUNCTION backlog_recover_stale_ready_to_review(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION backlog_recover_stale_ready_to_review(text) TO aicc_app;
