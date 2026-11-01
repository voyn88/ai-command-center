-- VOYN-W0-AICC-DEFER-AUTO-RESUME: a supported, mechanical exit from
-- DEFER_TO_USER for TECHNICAL parks only.
--
-- DEFER_TO_USER accumulates with no autonomous exit: every
-- backlog_transition from it is illegal, so the only escape has been
-- backlog_upsert_task -- which bypasses the machine entirely (no
-- classification, no budget, no audit of WHY the park was lifted). 0012
-- already stopped the most common technical exhaustions from parking new
-- tasks, but everything parked before it, and every reason 0012's
-- allowlist does not name, sits forever even when no owner action could
-- possibly help.
--
-- This function is the machine's answer, deliberately narrower than a
-- generic DEFER_TO_USER -> OPEN transition (which would let any caller
-- unpark a genuine owner decision):
--
-- * Only a task whose LATEST parking event -- the most recent granted
--   `return_to_pool` that targeted DEFER_TO_USER -- carries a technical
--   `cascade_exhausted:%` reason may resume. A task parked by an owner
--   decision, or with no machine park evidence at all (imported already
--   parked, or hand-upserted), is refused and stays visible to the owner.
-- * The resume budget is bounded: after 3 granted resumes the task stays
--   parked (`resume_budget_exhausted`) -- a task that re-parks every time
--   it runs is a fact for the owner, not fuel for an infinite
--   resume/exhaust loop.
-- * Every grant and refusal is audited to backlog_event
--   (`resume_deferred`), with the original park reason recorded on the
--   grant, so "why did this task come back" is always answerable.

CREATE FUNCTION backlog_resume_deferred(p_task_id text)
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
