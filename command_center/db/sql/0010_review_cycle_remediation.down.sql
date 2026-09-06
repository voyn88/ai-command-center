-- Restore 0005's backlog_transition and status vocabulary (no REJECTED, no
-- remediation lineage table). A row already sitting at status='REJECTED' or
-- a populated backlog_task_remediation table make this a schema-only
-- rollback, matching the project's existing downgrade contract (structure
-- reverts; live data compatibility with the restored constraint is not
-- guaranteed, same as every other down.sql in this set).

DROP FUNCTION IF EXISTS backlog_record_remediation(text, text, text, text);
DROP INDEX IF EXISTS idx_backlog_task_remediation_parent;
DROP TABLE IF EXISTS backlog_task_remediation;

DROP FUNCTION IF EXISTS backlog_transition(text, text, bigint);

CREATE FUNCTION backlog_transition(
    p_task_id text, p_to_status text, p_expected_revision bigint
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_allowed text;
        v_pr integer; v_sha integer;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'transition', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF t.kind = 'gate' THEN
        PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'gate_is_control_record');
        v.reason := 'gate_is_control_record'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF t.revision <> p_expected_revision THEN
        PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'revision_conflict',
                               jsonb_build_object('expected', p_expected_revision,
                                                  'actual', t.revision));
        v.reason := 'revision_conflict'; v.revision := t.revision;
        RETURN v;
    END IF;

    v_allowed := CASE t.status
        WHEN 'OPEN'            THEN 'IN_PROGRESS'
        WHEN 'IN_PROGRESS'     THEN 'READY_TO_REVIEW'
        WHEN 'READY_TO_REVIEW' THEN 'DONE'
        ELSE NULL
    END;
    IF v_allowed IS NULL OR p_to_status <> v_allowed THEN
        PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'illegal_transition',
                               jsonb_build_object('from', t.status, 'to', p_to_status));
        v.reason := 'illegal_transition: ' || t.status || ' -> ' || coalesce(p_to_status, '?');
        v.revision := t.revision;
        RETURN v;
    END IF;

    IF p_to_status = 'DONE' THEN
        SELECT count(*) FILTER (WHERE kind = 'pr'),
               count(*) FILTER (WHERE kind = 'sha')
          INTO v_pr, v_sha
          FROM backlog_evidence e WHERE e.task_id = p_task_id;
        IF v_pr = 0 OR v_sha = 0 THEN
            PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'missing_evidence',
                                   jsonb_build_object('pr', v_pr, 'sha', v_sha));
            v.reason := 'missing_evidence: DONE requires pr and sha';
            v.revision := t.revision;
            RETURN v;
        END IF;
    END IF;

    UPDATE backlog_task b
       SET status = p_to_status, revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'transition', 'granted', NULL,
                           jsonb_build_object('from', t.status, 'to', p_to_status));
    v.ok := true;
    RETURN v;
END
$$;

ALTER TABLE backlog_task DROP CONSTRAINT backlog_task_status_vocabulary;
ALTER TABLE backlog_task ADD CONSTRAINT backlog_task_status_vocabulary CHECK (status IN
    ('OPEN', 'IN_PROGRESS', 'READY_TO_REVIEW', 'DONE', 'UNTRIAGED',
     'DEFER_TO_USER', 'SPLIT', 'NEEDS_REFINEMENT', 'DECIDED'));
