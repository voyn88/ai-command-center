-- AICC PostgreSQL — pre-dispatch authority preflight
-- (VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-TO-UNPRIVILEGED-EXECUTOR).
--
-- Found live 2026-08-30: a task whose body demanded `sudo` / a specific
-- PostgreSQL role reached the executor cascade anyway. The agent honestly
-- tried `sudo -u postgres psql ...`, was refused (`a password is
-- required`), and the queue recorded that as `cascade_exhausted:
-- task_status_failed` -- which 0012 classifies as TECHNICAL and returns the
-- task straight to OPEN. Every worker on this fleet is deliberately
-- unprivileged (no sudo, no postgres role), so the retry could never
-- succeed: the planner re-dispatched the same task, burned all three
-- cascade accounts (claude/codex/copilot) again, and looped -- 8 tasks did
-- this in one 90-minute window.
--
-- The fix belongs BEFORE dispatch, not after. The planner's own preflight
-- (`command_center.orchestrator.authority_preflight`) reads a candidate
-- task's title/body for a required authority (root, a named Postgres role,
-- an external credential) this fleet does not grant, and calls THIS
-- function INSTEAD OF `backlog_dispatch` when it finds one -- so the task
-- never claims a WIP slot, never enters the cascade, and never spends a
-- single model call. It parks straight from OPEN to DEFER_TO_USER (the one
-- state that means "an owner, not a retry, decides next") with the specific
-- missing authority recorded as the audited reason.
--
-- Deliberately narrower than `backlog_transition`: it moves OPEN, and only
-- OPEN, tasks. A task already IN_PROGRESS is mid-cascade and belongs to
-- `backlog_return_to_pool` (0007/0012) instead, not this gate -- so a task
-- whose authority requirement is only discovered after dispatch (a prompt
-- an author edited post-hoc, say) is not silently reclassified out from
-- under a live worker.
CREATE FUNCTION backlog_park_requires_authority(p_task_id text, p_reason text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'authority_preflight', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF t.kind = 'gate' THEN
        PERFORM _backlog_audit(p_task_id, 'authority_preflight', 'rejected',
                               'gate_is_control_record');
        v.reason := 'gate_is_control_record'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF t.status <> 'OPEN' THEN
        PERFORM _backlog_audit(p_task_id, 'authority_preflight', 'rejected', 'not_open',
                               jsonb_build_object('status', t.status));
        v.reason := 'not_open'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF p_reason IS NULL OR length(p_reason) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'authority_preflight', 'rejected', 'empty_reason');
        v.reason := 'empty_reason'; v.revision := t.revision;
        RETURN v;
    END IF;

    UPDATE backlog_task b
       SET status = 'DEFER_TO_USER', revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'authority_preflight', 'granted', p_reason,
                           jsonb_build_object('from', 'OPEN', 'to', 'DEFER_TO_USER'));
    v.ok := true; v.reason := 'DEFER_TO_USER';
    RETURN v;
END
$$;
