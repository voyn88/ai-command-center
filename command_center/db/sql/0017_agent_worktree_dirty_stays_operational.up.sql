-- VOYN-W0-AICC-PUBLISH-PREP-UNTRACKED-FILES: the guarded-publish worktree
-- clean check is an infrastructure gate, not an owner decision.
--
-- `workspace_provisioning`'s `agent_worktree_clean` step refuses to publish
-- a task clone whose working tree does not exactly match the commit the
-- agent reported -- the same check whether the drift is an untracked NEW
-- file (a created migration never staged with `git add`) or a modified-
-- but-uncommitted file. `worker/handlers.py` always wraps it as one of
-- "guarded publish preparation failed at agent_worktree_clean: ...",
-- "local task checkpoint failed at agent_worktree_clean: ...", or
-- "infrastructure retry checkpoint failed at agent_worktree_clean: ..."
-- and already marks that `HandlerOutcome` `retryable=True` -- but 0012 never
-- taught `backlog_return_to_pool` that this cascade-exhaustion reason is the
-- same kind of "no owner action could help" failure as `no_pr_published` or
-- `publish_%`. A second occurrence therefore hit the two-epoch circuit
-- breaker and DEFER_TO_USER parked the task forever on the agent's own
-- unstaged/uncommitted work.
--
-- Live 2026-08-27 (task brief addendum): 8 dead work_items in 6h
-- (FLAKE-03b x3, DISPATCH-PLAN-FABRICATED-SPEND, MIRROR-HOOK-SWEEP,
-- PLAT-FIND-12-RETRY, AIOS-LEASE-TTL-CEILING) all
-- `max_attempts_exhausted` at `agent_worktree_clean` with M-status
-- (modified, not only untracked) files. A fresh dispatch reusing the
-- preserved clone (the prompt fix from VOYN-W0-AICC-AGENT-COMMIT-CONTRACT-
-- GAP, and the strengthened `git add -A` instruction from this task) is a
-- genuine chance to finish the commit -- an owner has nothing to decide
-- here, exactly as they have nothing to decide about a stale writer lease.
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
        OR p_reason LIKE 'cascade_exhausted: %agent_worktree_clean%';
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
