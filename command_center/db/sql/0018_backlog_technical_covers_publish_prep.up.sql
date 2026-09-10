-- VOYN-W0-AICC-DEFER-RESUME-COVER-PUBLISH-PREP-REM: guarded publish
-- preparation failures are technical exhaustions too.
--
-- 0012's `v_technical` allowlist named four cascade_exhausted shapes as
-- operational retries; a fifth was already live and uncovered. The worker's
-- guarded-publish-prep step (`handlers.py`, `WorkspaceVerificationError`
-- from `agent_worktree_clean`) raises when the task clone is not clean
-- (`uncommitted_changes: ...`), and the queue wraps that as
-- `max_attempts_exhausted: guarded publish preparation failed at
-- agent_worktree_clean: uncommitted_changes: ...` once its attempt budget
-- is spent; the same happens for the local-checkpoint-only variant of the
-- same guard (`local task checkpoint failed at agent_worktree_clean: ...`).
-- A dirty worktree is never something the task's owner can fix -- it is
-- purely a side effect of a prior attempt's own work being left uncommitted
-- -- so, like the four existing patterns, its second cascade must stay
-- OPEN rather than park in DEFER_TO_USER.
--
-- A REJECTED first attempt at this fix (PR #678, HEAD_SHA
-- 0766675ea8bea33b5594827aea9bfb0301f56599) matched the queue's generic
-- `max_attempts_exhausted:%` wrapper itself, rather than the specific
-- failure it wraps. `queue_fail` applies that exact prefix to ANY
-- `retryable=true` failure that exhausts its attempt budget -- not just
-- publish-prep ones -- so that pattern silently exempted the queue's
-- entire retryable-failure vocabulary from the `v_prior >= 1 ->
-- DEFER_TO_USER` circuit breaker: a task broken for an unrelated reason
-- (any bug some code path marks retryable) would loop OPEN -> fail -> OPEN
-- forever with no escalation to a human. This migration matches only the
-- two known publish-prep failure signatures by their `agent_worktree_clean`
-- failed_step, preserving the circuit breaker for every other retryable
-- failure that happens to share the generic wrapper prefix.
--
-- `visibility_timeout_exhausted` is kept from that same rejected attempt,
-- but it is not the same kind of pattern: `queue_reap` (0002) sets it as a
-- single fixed literal with no caller-supplied content ever interpolated
-- in, so `cascade_exhausted: visibility_timeout%` can only ever match that
-- one exact reap-driven cause (a worker that died or stalled past its
-- lease), never an arbitrary wrapped reason -- it carries none of the
-- blast radius the `max_attempts_exhausted:%` wrapper did.

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
        OR p_reason LIKE 'cascade_exhausted: max_attempts_exhausted: '
            'guarded publish preparation failed at agent_worktree_clean:%'
        OR p_reason LIKE 'cascade_exhausted: max_attempts_exhausted: '
            'local task checkpoint failed at agent_worktree_clean:%'
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
