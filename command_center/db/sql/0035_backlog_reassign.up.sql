-- AICC PostgreSQL — priority/wave reassignment (VOYN-W0-APP-CONTROL-S6d).
--
-- backlog_transition (0005) moves a task's STATUS one step at a time; nothing
-- moves its WAVE or PRIORITY once the task exists, except backlog_upsert_task
-- (0005), and that path is reserved for Markdown reconciliation — it may
-- overwrite status/title/body too, which is exactly wrong for a targeted "the
-- owner reprioritized this task" act (chat, voice, or the UI). This is that
-- act's own function, sized the way backlog_transition is: one row, one
-- optimistic-revision check, one audit entry, no side door into the fields it
-- does not own (status, title, body, repo are untouched).
--
-- Gates are not excluded here the way backlog_transition excludes them: a
-- gate still belongs to a wave and can still be reprioritized, it just never
-- executes. Only the status machine's adjacency rule is gate-specific.

CREATE FUNCTION backlog_reassign(
    p_task_id text, p_wave text, p_priority text, p_expected_revision bigint
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'reassign', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF t.revision <> p_expected_revision THEN
        PERFORM _backlog_audit(p_task_id, 'reassign', 'rejected', 'revision_conflict',
                               jsonb_build_object('expected', p_expected_revision,
                                                  'actual', t.revision));
        v.reason := 'revision_conflict'; v.revision := t.revision;
        RETURN v;
    END IF;

    IF t.wave = p_wave AND t.priority IS NOT DISTINCT FROM p_priority THEN
        v.ok := true; v.reason := 'unchanged'; v.revision := t.revision;
        RETURN v;
    END IF;

    BEGIN
        UPDATE backlog_task b
           SET wave = p_wave, priority = p_priority,
               revision = b.revision + 1, updated_at = now()
         WHERE b.task_id = p_task_id
        RETURNING b.revision INTO v.revision;
    EXCEPTION WHEN check_violation THEN
        PERFORM _backlog_audit(p_task_id, 'reassign', 'rejected', 'constraint: ' || SQLERRM);
        v.reason := 'constraint: ' || SQLERRM; v.revision := t.revision;
        RETURN v;
    END;
    PERFORM _backlog_audit(p_task_id, 'reassign', 'granted', NULL,
                           jsonb_build_object('from_wave', t.wave, 'to_wave', p_wave,
                                              'from_priority', t.priority,
                                              'to_priority', p_priority));
    v.ok := true; v.reason := 'reassigned';
    RETURN v;
END
$$;
