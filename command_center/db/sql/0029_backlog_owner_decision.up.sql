-- Explicit human decisions, never a shortcut to DONE or past review gates.
-- Locking a row requires UPDATE privileges. Keep those unavailable to the
-- application and expose only this bounded protocol operation instead.
CREATE FUNCTION backlog_owner_lock(p_task_id text)
RETURNS TABLE(revision bigint, status text, kind text)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT b.revision, b.status, b.kind FROM backlog_task b
    WHERE b.task_id = p_task_id FOR UPDATE
$$;
REVOKE ALL ON FUNCTION backlog_owner_lock(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION backlog_owner_lock(text) TO aicc_app;

CREATE FUNCTION backlog_owner_decision(
    p_task_id text, p_expected_revision bigint, p_decision text, p_note text
) RETURNS backlog_verdict
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; target text;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task WHERE task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        v.reason := 'unknown_task';
    ELSIF t.revision <> p_expected_revision THEN
        v.reason := 'revision_conflict'; v.revision := t.revision;
    ELSIF t.kind <> 'task' OR t.status NOT IN ('DEFER_TO_USER','NEEDS_REFINEMENT','UNTRIAGED') THEN
        v.reason := 'not_awaiting_decision'; v.revision := t.revision;
    ELSIF p_note IS NULL OR length(btrim(p_note)) = 0 OR length(p_note) > 4000 THEN
        v.reason := 'invalid_note';
    ELSE
        target := CASE p_decision WHEN 'resume' THEN 'OPEN'
                  WHEN 'reject' THEN 'REJECTED' WHEN 'refine' THEN 'NEEDS_REFINEMENT' END;
        IF target IS NULL THEN
            v.reason := 'unknown_decision';
        ELSE
            UPDATE backlog_task SET status = target, revision = revision + 1,
                updated_at = now(), body = coalesce(body, '') || E'\n\nOwner decision (' ||
                p_decision || E'):\n' || p_note
            WHERE task_id = p_task_id RETURNING revision INTO v.revision;
            v.ok := true; v.reason := target;
        END IF;
    END IF;
    PERFORM _backlog_audit(CASE WHEN t.task_id IS NULL THEN NULL ELSE p_task_id END,
        'owner_decision', CASE WHEN v.ok THEN 'granted' ELSE 'rejected' END,
        v.reason, jsonb_build_object('decision', p_decision, 'revision', v.revision));
    RETURN v;
END
$$;
REVOKE ALL ON FUNCTION backlog_owner_decision(text, bigint, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION backlog_owner_decision(text, bigint, text, text) TO aicc_app;
