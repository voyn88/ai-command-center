-- 0018: bounded Acceptance-gate reconciliation and verdict->merge latency
-- evidence (VOYN-W0-AICC-ACCEPTANCE-TAIL-LATENCY).
--
-- Two new backlog_evidence kinds:
--
--   'gate_rerun'    -- one row per bounded reconciliation attempt against a
--                       definitively-red Acceptance-gate check standing
--                       under an already-accepted head. Value is
--                       "<head_sha>:<attempt_number>"; at most three per
--                       head. Written by review_merge._reconcile_stale_
--                       acceptance_gate BEFORE the GitHub rerun is
--                       dispatched, never after, so a crash between
--                       dispatch and bookkeeping can never under-count.
--   'merge_latency' -- one best-effort row per merged PR recording the
--                       verdict->merge latency sample as JSON
--                       ({"verdict_at", "merged_at", "seconds"}).
--
-- backlog_reserve_gate_rerun makes the cap check and the reservation ONE
-- atomic, row-locked operation -- the same FOR UPDATE-on-backlog_task idiom
-- backlog_transition and backlog_record_evidence already use for every other
-- backlog mutation -- so two concurrent merge ticks against the same task
-- can never both observe room under the cap and both reserve the same
-- attempt slot (independent review of an earlier attempt at this task,
-- CONFIRMED: counting attempts and recording the next evidence row in
-- separate transactions let concurrent ticks exceed the promised per-head
-- cap).

ALTER TABLE backlog_evidence DROP CONSTRAINT backlog_evidence_kind_vocabulary;
ALTER TABLE backlog_evidence ADD CONSTRAINT backlog_evidence_kind_vocabulary
    CHECK (kind IN ('pr', 'sha', 'ci', 'acceptance', 'gate_rerun', 'merge_latency'));

CREATE OR REPLACE FUNCTION backlog_record_evidence(p_task_id text, p_kind text, p_value text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'evidence', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF p_kind NOT IN ('pr', 'sha', 'ci', 'acceptance', 'gate_rerun', 'merge_latency') THEN
        PERFORM _backlog_audit(p_task_id, 'evidence', 'rejected', 'unknown_kind',
                               jsonb_build_object('kind', p_kind));
        v.reason := 'unknown_kind'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF p_value IS NULL OR length(p_value) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'evidence', 'rejected', 'empty_value');
        v.reason := 'empty_value'; v.revision := t.revision;
        RETURN v;
    END IF;
    INSERT INTO backlog_evidence (task_id, kind, value)
    VALUES (p_task_id, p_kind, p_value)
    ON CONFLICT (task_id, kind, value) DO NOTHING;
    PERFORM _backlog_audit(p_task_id, 'evidence', 'granted', p_kind);
    v.ok := true; v.reason := 'recorded'; v.revision := t.revision;
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- backlog_reserve_gate_rerun — atomic cap check + reservation for the bounded
-- per-head Acceptance-gate reconciliation.
-- ---------------------------------------------------------------------------
-- Returns ok=true with the reserved attempt number (1-based) in `reason` once
-- reserved, or ok=false with reason='gate_rerun_cap_reached' once
-- p_max_attempts reservations already stand for this exact head. The caller
-- (review_merge._reconcile_stale_acceptance_gate) only calls this AFTER it
-- has already found a real matching failing run to dispatch -- a lookup that
-- finds nothing never reaches here, so a transient `gh` hiccup or a
-- momentarily absent run costs no reservation and is retried for free on the
-- next tick; only a call that gets past this reservation may dispatch, and
-- once dispatched the attempt is never refunded (an ambiguous `gh run rerun`
-- exit code cannot be trusted to mean "did not dispatch" -- see that
-- function's docstring).
CREATE FUNCTION backlog_reserve_gate_rerun(
    p_task_id text, p_head_sha text, p_max_attempts integer
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_count integer; v_next integer;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'gate_rerun', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF p_head_sha IS NULL OR length(p_head_sha) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'gate_rerun', 'rejected', 'empty_head_sha');
        v.reason := 'empty_head_sha'; v.revision := t.revision;
        RETURN v;
    END IF;

    SELECT count(*) INTO v_count FROM backlog_evidence e
     WHERE e.task_id = p_task_id AND e.kind = 'gate_rerun'
       AND e.value LIKE p_head_sha || ':%';
    IF v_count >= p_max_attempts THEN
        PERFORM _backlog_audit(p_task_id, 'gate_rerun', 'rejected', 'gate_rerun_cap_reached',
                               jsonb_build_object('head_sha', p_head_sha, 'attempts', v_count));
        v.reason := 'gate_rerun_cap_reached'; v.revision := t.revision;
        RETURN v;
    END IF;

    v_next := v_count + 1;
    INSERT INTO backlog_evidence (task_id, kind, value)
    VALUES (p_task_id, 'gate_rerun', p_head_sha || ':' || v_next)
    ON CONFLICT (task_id, kind, value) DO NOTHING;
    PERFORM _backlog_audit(p_task_id, 'gate_rerun', 'granted',
                           p_head_sha || ':' || v_next,
                           jsonb_build_object('head_sha', p_head_sha, 'attempt', v_next));
    v.ok := true; v.reason := v_next::text; v.revision := t.revision;
    RETURN v;
END
$$;
