-- 0017: the resume budget becomes a sliding window, not a lifetime score
-- (VOYN-W0-AICC-DEFER-AUTO-RESUME-REM).
--
-- 0014 bounded auto-resume at three GRANTED resumes per task, counted over
-- the task's whole life. That bound answers the wrong question: three
-- resumes within hours mean "this park re-arms faster than automation can
-- help -- a human should look", but three resumes spread over weeks mean
-- the task met three DIFFERENT transient outages and outlived them all.
-- Live consequence (2026-09): parks from the dead-codex era exhausted their
-- lifetime budget, and after the pipeline was fixed the tasks stayed
-- DEFER_TO_USER forever -- unreachable by the very machinery built to
-- reclaim them, resurrectable only by manual triage.
--
-- The window is 48 hours: wide enough that a nightly recurring failure
-- still trips it, narrow enough that a fixed pipeline reclaims its parks
-- within two days with no human in the loop. Everything else about the
-- 0014 gate -- the technical-park-only rule, superseded-evidence refusal,
-- row lock, SECURITY DEFINER posture -- is unchanged; this replaces only
-- the budget query inside the same function.

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
       AND e.outcome = 'granted'
       AND e.created_at > now() - interval '48 hours';
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
