-- VOYN-W0-AICC-DISPATCH-REUSE-GATE: the machine's way to close a remediation
-- task whose parent's work is already merged, WITHOUT dispatching a run that
-- would re-implement it.
--
-- The incident (2026-09-06): a `-REM` task's branch (PR 636) re-implemented
-- `checkpoint_dirty_task_workspace`, a function PR 624 had already merged to
-- main. The two definitions collided on signature, and the merged result
-- failed CI with TypeErrors -- work that was never needed, paid for twice,
-- that then broke main for everyone. The planner had no way to notice: its
-- candidate query asks what is eligible, never what is already done.
--
-- The Python half (`orchestrator.planner`) reads main and decides; this
-- function is the only write it may make, and is deliberately narrow so it
-- cannot become a generic "close anything" backdoor:
--
-- * Only OPEN is eligible. A task already IN_PROGRESS belongs to the run
--   holding it, and a READY_TO_REVIEW/DONE/REJECTED task has its own path
--   through review and merge. So this function can only ever prevent a
--   dispatch that has not happened yet -- never truncate one in flight.
-- * The task must be a REMEDIATION of the named parent: either the lineage
--   row `backlog_record_remediation` (0010) wrote, or the `-REM`/`-RETRY`
--   suffix convention over a parent that exists. Both are mechanical
--   relationships between two rows, checked here rather than trusted from
--   the caller, so an ordinary task can never be closed through this path
--   no matter what the planner passes.
-- * DONE is a claim about the repositories and the machine demands the
--   receipts (`backlog_transition`, 0005): the merged pull request and the
--   merged sha that already satisfy the parent's acceptance are recorded as
--   this task's `pr`/`sha` evidence, plus one `acceptance` row naming what
--   was found on main. The task therefore lands in DONE with exactly the
--   evidence every other DONE task carries, and "why did this close without
--   a run" is answerable from the store alone.
-- * Grants and refusals are audited under `close_superseded`, which is also
--   the durable count of duplicate dispatches this gate prevented.

CREATE FUNCTION backlog_close_superseded(
    p_task_id text, p_parent_task_id text, p_pr_url text, p_merged_sha text,
    p_evidence text
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; parent backlog_task%ROWTYPE; v backlog_verdict;
        v_linked boolean;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'close_superseded', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF t.kind = 'gate' THEN
        PERFORM _backlog_audit(p_task_id, 'close_superseded', 'rejected',
                               'gate_is_control_record');
        v.reason := 'gate_is_control_record'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF t.status <> 'OPEN' THEN
        PERFORM _backlog_audit(p_task_id, 'close_superseded', 'rejected', 'not_open',
                               jsonb_build_object('status', t.status));
        v.reason := 'not_open'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF p_task_id = p_parent_task_id THEN
        PERFORM _backlog_audit(p_task_id, 'close_superseded', 'rejected', 'self_reference');
        v.reason := 'self_reference'; v.revision := t.revision;
        RETURN v;
    END IF;
    SELECT * INTO parent FROM backlog_task b WHERE b.task_id = p_parent_task_id;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(p_task_id, 'close_superseded', 'rejected',
                               'unknown_parent_task',
                               jsonb_build_object('requested_parent_task_id',
                                                  p_parent_task_id));
        v.reason := 'unknown_parent_task'; v.revision := t.revision;
        RETURN v;
    END IF;

    -- The remediation relationship, checked against the store: the recorded
    -- lineage first, then the `-REM`/`-RETRY` suffix convention the backlog
    -- file itself uses for follow-ups that were never spawned by a review.
    SELECT EXISTS (
        SELECT 1 FROM backlog_task_remediation r
         WHERE r.task_id = p_task_id AND r.parent_task_id = p_parent_task_id
    ) OR p_task_id IN (p_parent_task_id || '-REM', p_parent_task_id || '-RETRY')
      INTO v_linked;
    IF NOT v_linked THEN
        PERFORM _backlog_audit(p_task_id, 'close_superseded', 'rejected',
                               'not_a_remediation',
                               jsonb_build_object('parent', p_parent_task_id));
        v.reason := 'not_a_remediation'; v.revision := t.revision;
        RETURN v;
    END IF;

    IF p_pr_url IS NULL OR length(p_pr_url) = 0
       OR p_merged_sha IS NULL OR length(p_merged_sha) = 0
       OR p_evidence IS NULL OR length(p_evidence) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'close_superseded', 'rejected', 'empty_value');
        v.reason := 'empty_value'; v.revision := t.revision;
        RETURN v;
    END IF;

    INSERT INTO backlog_evidence (task_id, kind, value)
    VALUES (p_task_id, 'pr', p_pr_url),
           (p_task_id, 'sha', p_merged_sha),
           (p_task_id, 'acceptance', p_evidence)
    ON CONFLICT (task_id, kind, value) DO NOTHING;

    UPDATE backlog_task b
       SET status = 'DONE', revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'close_superseded', 'granted', p_parent_task_id,
                           jsonb_build_object('from', t.status, 'to', 'DONE',
                                              'parent', p_parent_task_id,
                                              'pr', p_pr_url, 'sha', p_merged_sha,
                                              'evidence', p_evidence));
    v.ok := true; v.reason := 'superseded';
    RETURN v;
END
$$;
