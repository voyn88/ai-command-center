-- VOYN-W0-AICC-DISPATCH-REUSE-GATE: a sanctioned exit for a task whose
-- acceptance criteria are ALREADY satisfied on the target branch, so the
-- planner can close it instead of dispatching a duplicate implementation.
--
-- Live case, 2026-09-06: a REM (remediation) task's branch (PR 636)
-- re-implemented `checkpoint_dirty_task_workspace`, already merged to main
-- via PR 624 under its parent task -- the two implementations collided and
-- broke CI with TypeErrors once both landed. Nothing before this migration
-- ever asked "is this already done?" before dispatching a REM task or a
-- resumed retry: `backlog_eligible`/`backlog_dispatch` only check that the
-- task itself is OPEN with no outstanding dependency, which says nothing
-- about whether some OTHER task (the parent a remediation follows up on, or
-- an earlier attempt of the very same task) already shipped the same work.
--
-- This function is the closing half of that check (the planner-side lookup
-- that decides whether to call it lives in `orchestrator/planner.py`,
-- `_reuse_anchor`/`_merged_commit_for`). Deliberately narrow, the same shape
-- as 0018's `backlog_recover_stuck_ready_to_review`:
--
-- * Only OPEN is eligible -- a task already IN_PROGRESS/READY_TO_REVIEW/DONE
--   has either already been superseded by this same act, or is far enough
--   into the pipeline that closing it out from under a live attempt would
--   race a real writer instead of preventing a redundant one.
-- * `p_source_task_id` and `p_evidence_sha` are both required: the reason
--   MUST name the commit that already satisfies this task and the task_id
--   that commit belongs to (the remediation's parent, or the task's own
--   prior attempt) -- "close it because" is not optional here the way it
--   would be for a routine transition, because this is the one door that
--   lets a task reach DONE without ever producing its own PR.
-- * The landing state is DONE: the acceptance criteria the task named ARE
--   met on the target branch, which is exactly what DONE asserts elsewhere
--   in this machine -- just evidenced by someone else's commit instead of
--   this task's own. `reconcile_merge_evidence` (review_merge.py) already
--   tolerates a DONE row with no `pr` evidence, only `sha`, so this does not
--   need to invent a PR URL for a PR that may never have existed under this
--   task_id at all.
-- * Every grant and refusal is audited (`close_superseded`), naming the
--   source task, so "why is this DONE with no PR of its own" stays
--   answerable from the audit trail alone.

CREATE FUNCTION backlog_close_superseded(
    p_task_id text, p_source_task_id text, p_evidence_sha text,
    p_detail text DEFAULT NULL
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict;
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
    IF p_source_task_id IS NULL OR length(p_source_task_id) = 0
       OR p_evidence_sha IS NULL OR length(p_evidence_sha) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'close_superseded', 'rejected', 'empty_value');
        v.reason := 'empty_value'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF p_task_id = p_source_task_id THEN
        -- A task's own prior attempt still names ITS OWN task_id as the
        -- source (a resumed retry checks its own history), and that is
        -- allowed -- what is refused is a source that does not exist at all.
        IF NOT EXISTS (SELECT 1 FROM backlog_task WHERE task_id = p_source_task_id) THEN
            v.reason := 'unknown_source_task'; v.revision := t.revision;
            RETURN v;
        END IF;
    ELSIF NOT EXISTS (SELECT 1 FROM backlog_task WHERE task_id = p_source_task_id) THEN
        PERFORM _backlog_audit(p_task_id, 'close_superseded', 'rejected',
                               'unknown_source_task',
                               jsonb_build_object('source_task_id', p_source_task_id));
        v.reason := 'unknown_source_task'; v.revision := t.revision;
        RETURN v;
    END IF;

    INSERT INTO backlog_evidence (task_id, kind, value)
    VALUES (p_task_id, 'sha', p_evidence_sha)
    ON CONFLICT (task_id, kind, value) DO NOTHING;
    INSERT INTO backlog_evidence (task_id, kind, value)
    VALUES (p_task_id, 'acceptance',
            'superseded_by:' || p_source_task_id || ':' || p_evidence_sha)
    ON CONFLICT (task_id, kind, value) DO NOTHING;

    UPDATE backlog_task b
       SET status = 'DONE', revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'close_superseded', 'granted', p_source_task_id,
                           jsonb_build_object('sha', p_evidence_sha, 'detail', p_detail));
    v.ok := true; v.reason := 'DONE';
    RETURN v;
END
$$;
