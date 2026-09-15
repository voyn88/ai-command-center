-- AICC PostgreSQL — an authority failure is an OWNER decision, never a retry
-- (VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-TO-UNPRIVILEGED-EXECUTOR).
--
-- 0017 stops a task the fleet cannot authorize at the planner, before
-- dispatch. That is the main gate, but it is not the only way an authority
-- failure can reach the queue:
--
--   * the requirement is added to a task AFTER it was dispatched;
--   * the payload is enqueued directly, bypassing the planner;
--   * a worker host that was supposed to grant the authority does not
--     (the honest case the whole task exists for).
--
-- All three now end at the worker's own entry gate (`worker.handlers`),
-- which refuses the payload with `requires_privileged_authority: <tags>`
-- before the model is invoked. That refusal dead-letters, and
-- `backlog_ingest_results` folds a dead_reason into
-- `cascade_exhausted: <dead_reason>` -- at which point 0012's classifier
-- sees a reason it does not name, returns the task to OPEN (first park),
-- and the planner dispatches it again. Zero model calls per cycle now, but
-- still a cycle, and 0014's reconcile would resume it up to three more
-- times because the wrapped reason DOES match `cascade_exhausted:%`.
--
-- No retry can grant a privilege. So the reason itself carries the
-- classification, wherever it ends up wrapped, and both directions honour it:
--
--   backlog_return_to_pool      -> DEFER_TO_USER on the FIRST occurrence,
--                                  never OPEN, never 'technical';
--   backlog_resume_deferred     -> refuses to resume such a park.
--
-- Only an owner acting -- granting the privilege, adding a privileged
-- worker lane, or rewriting the task -- can change the answer.

-- The single vocabulary both directions match. Token-anchored rather than
-- prefix-anchored because the reason travels WRAPPED, twice over: the
-- planner writes it bare (`requires_privileged_authority: root`), `queue_fail`
-- (0002) prefixes a non-retryable refusal with `non_retryable: `, and
-- `backlog_ingest_results` (0011) folds the dead_reason into
-- `cascade_exhausted: `. What the store finally holds for a worker-gate
-- refusal is
--   `cascade_exhausted: non_retryable: requires_privileged_authority: root`
-- and a third wrapper must not silently reopen the loop by shifting the
-- offset again.
CREATE FUNCTION backlog_reason_requires_authority(p_reason text)
    RETURNS boolean
    LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, public AS $$
    SELECT coalesce(p_reason ~ '(^|[^a-z_])requires_privileged_authority:', false)
$$;

CREATE OR REPLACE FUNCTION backlog_return_to_pool(p_task_id text, p_reason text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_target text; v_prior integer;
        v_technical boolean; v_authority boolean;
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
    -- Checked FIRST and kept out of v_technical: a missing privilege is the
    -- one exhaustion that is not operational. Retrying it is free of model
    -- calls (the worker gate refuses before invoking one) and still useless.
    v_authority := backlog_reason_requires_authority(p_reason);
    v_technical := NOT v_authority AND (
           p_reason LIKE 'cascade_exhausted: no_pr_published%'
        OR p_reason LIKE 'cascade_exhausted: task_status_failed%'
        OR p_reason LIKE 'cascade_exhausted: executor infrastructure failure%'
        OR p_reason LIKE 'cascade_exhausted: publish_%');
    v_target := CASE
        WHEN v_authority THEN 'DEFER_TO_USER'
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
                                              'technical', v_technical,
                                              'authority', v_authority));
    v.ok := true; v.reason := v_target;
    RETURN v;
END
$$;

CREATE OR REPLACE FUNCTION backlog_resume_deferred(p_task_id text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict;
        v_park_reason text; v_park_event_id bigint; v_resumes integer;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'resume_deferred', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF t.kind = 'gate' THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'gate_is_control_record');
        v.reason := 'gate_is_control_record'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF t.status <> 'DEFER_TO_USER' THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected', 'not_deferred',
                               jsonb_build_object('status', t.status));
        v.reason := 'not_deferred'; v.revision := t.revision;
        RETURN v;
    END IF;

    SELECT e.reason, e.event_id INTO v_park_reason, v_park_event_id
      FROM backlog_event e
     WHERE e.task_id = p_task_id
       AND e.event = 'return_to_pool'
       AND e.outcome = 'granted'
       AND e.detail->>'target' = 'DEFER_TO_USER'
     ORDER BY e.event_id DESC
     LIMIT 1;
    IF NOT FOUND THEN
        -- Parked outside the machine (imported that way, or upserted by an
        -- operator): provenance unknown, so the park is treated as an owner
        -- decision. Fail closed.
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'no_machine_park_evidence');
        v.reason := 'no_machine_park_evidence'; v.revision := t.revision;
        RETURN v;
    END IF;
    -- An authority park is an owner decision even though its reason arrives
    -- wrapped in the technical `cascade_exhausted:` prefix: the wrapper
    -- describes HOW the failure surfaced, the token describes WHY, and only
    -- the why decides whether a retry could ever help.
    IF backlog_reason_requires_authority(v_park_reason) THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'requires_authority_park',
                               jsonb_build_object('park_reason', v_park_reason));
        v.reason := 'requires_authority_park'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF v_park_reason IS NULL OR v_park_reason NOT LIKE 'cascade_exhausted:%' THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'owner_decision_park',
                               jsonb_build_object('park_reason', v_park_reason));
        v.reason := 'owner_decision_park'; v.revision := t.revision;
        RETURN v;
    END IF;

    -- The park event must still be the mutation that PRODUCED the current
    -- DEFER_TO_USER state, not merely the newest technical park on record
    -- (independent review of PR #401 at 2bc73ac: a task technically parked,
    -- later resumed, and then hand-upserted back into DEFER_TO_USER for an
    -- owner decision still carries its old cascade_exhausted event -- which
    -- must not reopen it). Any granted mutating event after the park event
    -- means some other act may have set the current state: fail closed.
    IF EXISTS (
        SELECT 1 FROM backlog_event e
         WHERE e.task_id = p_task_id
           AND e.outcome = 'granted'
           AND e.event IN ('upsert', 'transition', 'triage',
                           'dispatch', 'return_to_pool', 'resume_deferred')
           AND e.event_id > v_park_event_id
    ) THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'superseded_park_evidence',
                               jsonb_build_object('park_event_id', v_park_event_id));
        v.reason := 'superseded_park_evidence'; v.revision := t.revision;
        RETURN v;
    END IF;

    SELECT count(*) INTO v_resumes FROM backlog_event e
     WHERE e.task_id = p_task_id
       AND e.event = 'resume_deferred'
       AND e.outcome = 'granted';
    IF v_resumes >= 3 THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'resume_budget_exhausted',
                               jsonb_build_object('prior_resumes', v_resumes));
        v.reason := 'resume_budget_exhausted'; v.revision := t.revision;
        RETURN v;
    END IF;

    UPDATE backlog_task b
       SET status = 'OPEN', revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'granted', v_park_reason,
                           jsonb_build_object('park_event_id', v_park_event_id,
                                              'prior_resumes', v_resumes));
    v.ok := true; v.reason := 'OPEN';
    RETURN v;
END
$$;
