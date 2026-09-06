DROP FUNCTION backlog_reserve_gate_rerun(text, text, integer);

CREATE OR REPLACE FUNCTION backlog_record_evidence(p_task_id text, p_kind text, p_value text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'evidence', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF p_kind NOT IN ('pr', 'sha', 'ci', 'acceptance') THEN
        PERFORM _backlog_audit(p_task_id, 'evidence', 'rejected', 'unknown_kind',
                               jsonb_build_object('kind', p_kind));
        v.reason := 'unknown_kind'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF p_value IS NULL OR length(p_value) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'evidence', 'rejected', 'empty_value');
        v.reason := 'empty_value'; v.revision := t.revision;
        RETURN v;
    END IF;
    INSERT INTO backlog_evidence (task_id, kind, value)
    VALUES (p_task_id, p_kind, p_value)
    ON CONFLICT (task_id, kind, value) DO NOTHING;
    PERFORM _backlog_audit(p_task_id, 'evidence', 'granted', p_kind);
    v.ok := true; v.reason := 'recorded'; v.revision := t.revision;
    RETURN v;
END
$$;

DELETE FROM backlog_evidence WHERE kind IN ('gate_rerun', 'merge_latency');

ALTER TABLE backlog_evidence DROP CONSTRAINT backlog_evidence_kind_vocabulary;
ALTER TABLE backlog_evidence ADD CONSTRAINT backlog_evidence_kind_vocabulary
    CHECK (kind IN ('pr', 'sha', 'ci', 'acceptance'));
