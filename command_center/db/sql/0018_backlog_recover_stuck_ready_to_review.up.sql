-- VOYN-W0-AICC-NO-RECOVERY-PATH-STUCK-READY-TO-REVIEW: a sanctioned recovery
-- path for a task stuck in READY_TO_REVIEW with no `pr` evidence.
--
-- 0011 closed the gap that PRODUCED this state (a completed run whose
-- publish step failed used to land in READY_TO_REVIEW anyway, with no `pr`
-- evidence and therefore no way to ever reach DONE: `review_once`,
-- `publish_review_verdicts` and `merge_once` all JOIN `backlog_evidence` on
-- `kind = 'pr'`). That fix stops NEW cases. It gives no path out for
-- whatever was already stuck there before it shipped, or for any future
-- bug in a different corner of the same pipeline that reintroduces the
-- same evidence-free READY_TO_REVIEW row -- `backlog_transition`'s
-- adjacency has no READY_TO_REVIEW -> anything-but-DONE move, and
-- `backlog_return_to_pool` (0007/0009/0012) only accepts a task that is
-- currently IN_PROGRESS, so a stuck READY_TO_REVIEW row is invisible to
-- both of the machine's existing recovery mechanisms.
--
-- This function is that path, deliberately narrow so it cannot become a
-- generic "unstick anything" backdoor:
--
-- * Only READY_TO_REVIEW is eligible -- a task with a `pr` already on
--   record belongs to review/merge, not recovery.
-- * The gate MUST be the absence of `pr` evidence specifically: a task
--   that reached READY_TO_REVIEW with a `pr` recorded is genuinely
--   reviewable (or already rejected/merged through that path) and this
--   function refuses it (`has_pr_evidence`), leaving it to the real
--   review machinery.
-- * The landing state is OPEN, exactly where `backlog_return_to_pool`
--   sends the technical-exhaustion case this same bug should have hit the
--   first time -- a fresh dispatch gets another attempt at a real PR.
-- * Every grant and refusal is audited (`recover_stuck_ready_to_review`),
--   so "why did this task move" stays answerable from the audit alone.

CREATE FUNCTION backlog_recover_stuck_ready_to_review(p_task_id text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_pr integer;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'recover_stuck_ready_to_review', 'rejected',
                               'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF t.kind = 'gate' THEN
        PERFORM _backlog_audit(p_task_id, 'recover_stuck_ready_to_review', 'rejected',
                               'gate_is_control_record');
        v.reason := 'gate_is_control_record'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF t.status <> 'READY_TO_REVIEW' THEN
        PERFORM _backlog_audit(p_task_id, 'recover_stuck_ready_to_review', 'rejected',
                               'not_ready_to_review',
                               jsonb_build_object('status', t.status));
        v.reason := 'not_ready_to_review'; v.revision := t.revision;
        RETURN v;
    END IF;

    SELECT count(*) INTO v_pr FROM backlog_evidence e
     WHERE e.task_id = p_task_id AND e.kind = 'pr';
    IF v_pr > 0 THEN
        PERFORM _backlog_audit(p_task_id, 'recover_stuck_ready_to_review', 'rejected',
                               'has_pr_evidence', jsonb_build_object('pr_evidence', v_pr));
        v.reason := 'has_pr_evidence'; v.revision := t.revision;
        RETURN v;
    END IF;

    UPDATE backlog_task b
       SET status = 'OPEN', revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'recover_stuck_ready_to_review', 'granted', 'no_pr_evidence',
                           jsonb_build_object('from', 'READY_TO_REVIEW', 'to', 'OPEN'));
    v.ok := true; v.reason := 'OPEN';
    RETURN v;
END
$$;
