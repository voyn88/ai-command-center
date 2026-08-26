-- Triage the UNTRIAGED backlog (BO-S1 left the path unbuilt).
--
-- The status machine (backlog_transition) is strictly linear OPEN -> ... ->
-- DONE; UNTRIAGED / NEEDS_REFINEMENT / SPLIT / DECIDED have no transitions,
-- by design — a raw finding must not walk straight into the executable path.
-- But the importer stamps findings UNTRIAGED, and nothing could move them,
-- so 85 of them are stuck: neither runnable nor closable. This is the
-- missing seam.
--
-- backlog_triage decides ONE finding's fate, and only from UNTRIAGED:
--   * 'accept'  -> OPEN            (a real, runnable task)
--   * 'refine'  -> NEEDS_REFINEMENT(a placeholder that needs shaping first)
--   * 'done'    -> DONE            (already delivered — requires pr+sha
--                                   evidence, exactly like a normal DONE,
--                                   so "already done" cannot be asserted
--                                   without receipts)
--   * 'duplicate' -> DECIDED       (superseded; the original is named in the
--                                   audit detail so the decision is traceable)
-- Every refusal is data (a backlog_verdict), every decision is audited, and
-- the DONE path reuses the same pr+sha gate the linear machine enforces — a
-- finding cannot be triaged 'done' on a bare claim.
CREATE FUNCTION backlog_triage(
    p_task_id  text,
    p_decision text,
    p_detail   text DEFAULT NULL
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    t backlog_task%ROWTYPE;
    v backlog_verdict;
    v_target text;
    v_pr int;
    v_sha int;
BEGIN
    SELECT * INTO t FROM backlog_task WHERE task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        v.ok := false; v.reason := 'unknown_task'; RETURN v;
    END IF;
    IF t.status <> 'UNTRIAGED' THEN
        -- Triage is only from UNTRIAGED; anything else is the linear machine's
        -- job. Refusal as data, not an exception.
        PERFORM _backlog_audit(p_task_id, 'triage', 'rejected', 'not_untriaged',
                               jsonb_build_object('status', t.status));
        v.ok := false; v.reason := 'not_untriaged: ' || t.status; v.revision := t.revision;
        RETURN v;
    END IF;

    v_target := CASE p_decision
        WHEN 'accept'    THEN 'OPEN'
        WHEN 'refine'    THEN 'NEEDS_REFINEMENT'
        WHEN 'done'      THEN 'DONE'
        WHEN 'duplicate' THEN 'DECIDED'
        ELSE NULL
    END;
    IF v_target IS NULL THEN
        PERFORM _backlog_audit(p_task_id, 'triage', 'rejected', 'unknown_decision',
                               jsonb_build_object('decision', p_decision));
        v.ok := false; v.reason := 'unknown_decision: ' || coalesce(p_decision, '?');
        v.revision := t.revision; RETURN v;
    END IF;

    -- 'done' demands the same receipts as the linear machine's DONE.
    IF v_target = 'DONE' THEN
        SELECT count(*) FILTER (WHERE kind = 'pr'),
               count(*) FILTER (WHERE kind = 'sha')
          INTO v_pr, v_sha
          FROM backlog_evidence WHERE task_id = p_task_id;
        IF v_pr = 0 OR v_sha = 0 THEN
            PERFORM _backlog_audit(p_task_id, 'triage', 'rejected', 'done_needs_evidence',
                                   jsonb_build_object('pr', v_pr, 'sha', v_sha));
            v.ok := false; v.reason := 'done_needs_evidence (pr+sha)'; v.revision := t.revision;
            RETURN v;
        END IF;
    END IF;

    UPDATE backlog_task
       SET status = v_target, revision = revision + 1, updated_at = clock_timestamp()
     WHERE task_id = p_task_id;
    PERFORM _backlog_audit(p_task_id, 'triage', 'granted', p_decision,
                           jsonb_build_object('to', v_target, 'detail', p_detail));
    v.ok := true; v.reason := v_target; v.revision := t.revision + 1;
    RETURN v;
END
$$;

REVOKE ALL ON FUNCTION backlog_triage(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION backlog_triage(text, text, text) TO aicc_app;
