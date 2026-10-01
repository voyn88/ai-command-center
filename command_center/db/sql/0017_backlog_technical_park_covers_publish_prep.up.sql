-- VOYN-W0-AICC-DEFER-RESUME-COVER-PUBLISH-PREP: guarded publish preparation
-- failures are a technical cascade cause too, not an owner decision.
--
-- 0012 taught backlog_return_to_pool that some cascade_exhausted reasons are
-- operational retries a human cannot help with, so a SECOND exhaustion of
-- those reasons still returns to OPEN instead of parking in DEFER_TO_USER.
-- Its allowlist named the specific failure shapes known at the time
-- (no_pr_published, task_status_failed, executor infrastructure failure,
-- publish_*) but missed a whole other source: guarded_publish_clone /
-- publish_run raising WorkspaceVerificationError (worker/handlers.py) before
-- publish.py ever runs -- e.g. `agent_worktree_clean` finding
-- `uncommitted_changes` in the verification clone. That reason reaches
-- backlog_return_to_pool wrapped by queue_fail's own dead-letter prefix
-- (0002_queue_claim.sql): `cascade_exhausted: max_attempts_exhausted: guarded
-- publish preparation failed at agent_worktree_clean: uncommitted_changes:
-- ...`. Unmatched, a second occurrence parked the task in DEFER_TO_USER, and
-- 0014's `backlog_resume_deferred` only has a 3-resume budget before it
-- refuses further and the task is stuck looking exactly like an owner
-- decision -- ~9 tasks did, live.
--
-- The underlying rule (this session's finding, DEFER-AUTO-RESUME-REM):
-- `max_attempts_exhausted:` is queue_fail's own label for "the retry budget
-- ran out on a *retryable* failure" -- retryable is precisely the set of
-- failures the queue itself judged curable by trying again, never a park
-- reason a human was meant to adjudicate. The matching reap-driven cause
-- (`visibility_timeout_exhausted`, queue_reap: attempts exhausted after a
-- worker vanished) is the same operational shape under a different name.
-- Genuine owner decisions -- ambiguous product choice, credentials, spend,
-- platform/org limits -- never flow through this cascade path at all; they
-- reach DEFER_TO_USER by direct upsert/transition, which this function does
-- not touch. So the two new patterns below are deliberately broad rather
-- than naming each failed_step: any `max_attempts_exhausted:` or
-- `visibility_timeout` cascade is technical.
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
        OR p_reason LIKE 'cascade_exhausted: publish_%'
        OR p_reason LIKE 'cascade_exhausted: max_attempts_exhausted:%'
        OR p_reason LIKE 'cascade_exhausted: visibility_timeout%';
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
