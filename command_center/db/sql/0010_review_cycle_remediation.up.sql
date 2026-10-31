-- AICC PostgreSQL — reject closes the loop into a linked remediation task,
-- not a cycle back into the linear state machine
-- (VOYN-W0-AICC-REVIEW-REJECT-REMEDIATION-LOOP).
--
-- Found live 2026-08-21, the first time an adversarial review agent ever
-- actually read a real diff and returned a real verdict (both REJECT, with
-- concrete, actionable feedback): there was no code path anywhere that did
-- anything with a REJECT beyond skipping it, forever. review_once()'s
-- enqueue key was permanently `review:<task_id>`, so once a task's review
-- concluded (either verdict), no future review could ever be enqueued for
-- it again -- a remediation push to the same PR would never get reviewed.
--
-- Two designs were considered for "what happens after REJECT": (a) add
-- READY_TO_REVIEW -> IN_PROGRESS as a new adjacency so the SAME task cycles
-- back for another attempt, or (b) leave the rejected task exactly as it
-- is, terminal, and dispatch a NEW, explicitly linked remediation task.
-- (a) was rejected on the owner's call: the state machine is deliberately
-- documented as linear, "one step, no skips" (0008's own comment) -- a
-- cycle for this one case would make one task_id's history ambiguous
-- (which pass does a given evidence row belong to?) and blur the audit
-- trail backlog_transition's revision counter is built to keep exact. (b)
-- keeps every existing invariant: REJECTED is a second terminal leaf next
-- to DONE (still reachable only from READY_TO_REVIEW, still never leads
-- back into OPEN/IN_PROGRESS/READY_TO_REVIEW), and the remediation task is
-- an ordinary new backlog_task the planner dispatches exactly like any
-- other -- no new dispatch code, no new state-machine edge into the
-- productive part of the graph.
--
-- The review-cycle key is fixed in the same migration because the two are
-- one connected design, not two: the remediation task opens its OWN new PR
-- (the rejected PR is left as-is, superseded), so it needs its own fresh
-- review identity rather than inheriting the permanently-consumed
-- `review:<task_id>` key its REJECTED parent used -- otherwise the exact
-- same permanent-first-review problem this migration is fixing for
-- remediation would just as easily hit an ordinary task that gets a second
-- push while still IN_PROGRESS. Both cases need the same fix: identity per
-- (task, pr, head sha, review policy version), not per task_id alone.
-- review_merge.py's review_once()/publish_review_verdicts() are updated in
-- the same PR to build and look up that key; see their module docstring
-- for the Python-side half of this design.

-- REJECTED joins DONE as the only two states READY_TO_REVIEW can reach.
ALTER TABLE backlog_task DROP CONSTRAINT backlog_task_status_vocabulary;
ALTER TABLE backlog_task ADD CONSTRAINT backlog_task_status_vocabulary CHECK (status IN
    ('OPEN', 'IN_PROGRESS', 'READY_TO_REVIEW', 'DONE', 'REJECTED', 'UNTRIAGED',
     'DEFER_TO_USER', 'SPLIT', 'NEEDS_REFINEMENT', 'DECIDED'));

DROP FUNCTION IF EXISTS backlog_transition(text, text, bigint);

CREATE FUNCTION backlog_transition(
    p_task_id text, p_to_status text, p_expected_revision bigint
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_allowed text[];
        v_pr integer; v_sha integer;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'transition', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF t.kind = 'gate' THEN
        PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'gate_is_control_record');
        v.reason := 'gate_is_control_record'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF t.revision <> p_expected_revision THEN
        PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'revision_conflict',
                               jsonb_build_object('expected', p_expected_revision,
                                                  'actual', t.revision));
        v.reason := 'revision_conflict'; v.revision := t.revision;
        RETURN v;
    END IF;

    -- Adjacency, not reachability: OPEN -> DONE does not exist as a move.
    -- READY_TO_REVIEW now fans out to two terminal leaves (DONE, REJECTED)
    -- instead of one -- still no edge leads back into the productive part
    -- of the graph from either.
    v_allowed := CASE t.status
        WHEN 'OPEN'            THEN ARRAY['IN_PROGRESS']
        WHEN 'IN_PROGRESS'     THEN ARRAY['READY_TO_REVIEW']
        WHEN 'READY_TO_REVIEW' THEN ARRAY['DONE', 'REJECTED']
        ELSE NULL
    END;
    IF v_allowed IS NULL OR NOT (p_to_status = ANY(v_allowed)) THEN
        PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'illegal_transition',
                               jsonb_build_object('from', t.status, 'to', p_to_status));
        v.reason := 'illegal_transition: ' || t.status || ' -> ' || coalesce(p_to_status, '?');
        v.revision := t.revision;
        RETURN v;
    END IF;

    IF p_to_status = 'DONE' THEN
        -- DONE is a claim about the repositories; the machine demands the
        -- receipts: a PR and a merged SHA, recorded as evidence rows.
        -- REJECTED makes no such claim -- it is an honest "this attempt did
        -- not land," so it carries no evidence requirement.
        SELECT count(*) FILTER (WHERE kind = 'pr'),
               count(*) FILTER (WHERE kind = 'sha')
          INTO v_pr, v_sha
          FROM backlog_evidence e WHERE e.task_id = p_task_id;
        IF v_pr = 0 OR v_sha = 0 THEN
            PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'missing_evidence',
                                   jsonb_build_object('pr', v_pr, 'sha', v_sha));
            v.reason := 'missing_evidence: DONE requires pr and sha';
            v.revision := t.revision;
            RETURN v;
        END IF;
    END IF;

    UPDATE backlog_task b
       SET status = p_to_status, revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'transition', 'granted', NULL,
                           jsonb_build_object('from', t.status, 'to', p_to_status));
    v.ok := true;
    RETURN v;
END
$$;

-- Lineage for a remediation task: queryable, not prose in a body field.
-- Deliberately NOT backlog_dependency -- that table means "must be DONE
-- before I may be dispatched," which would wrongly block the remediation
-- task forever (its parent is REJECTED, a terminal leaf that can never
-- become DONE). This is audit lineage, not a readiness gate: the planner's
-- ordinary dependency-free dispatch path picks up the remediation task like
-- any other OPEN task.
CREATE TABLE backlog_task_remediation (
    task_id           text NOT NULL PRIMARY KEY REFERENCES backlog_task(task_id),
    parent_task_id    text NOT NULL REFERENCES backlog_task(task_id),
    pr_url            text NOT NULL,
    rejected_head_sha text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT backlog_task_remediation_not_self CHECK (task_id <> parent_task_id)
);

CREATE INDEX idx_backlog_task_remediation_parent
    ON backlog_task_remediation(parent_task_id);

REVOKE ALL ON backlog_task_remediation FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- backlog_record_remediation — the one write path onto the lineage table,
-- mirroring backlog_record_evidence's shape: idempotent, audited, and the
-- only way any role (including the control plane's own aicc_app) reaches
-- backlog_task_remediation. Direct table access stays revoked from
-- everyone, same as every other backlog table (see the 0005 comment this
-- migration's header restates: every write travels through a SECURITY
-- DEFINER function so the status machine and the audit trail cannot be
-- bypassed by a client that simply issues its own INSERT).
-- ---------------------------------------------------------------------------
CREATE FUNCTION backlog_record_remediation(
    p_task_id text, p_parent_task_id text, p_pr_url text, p_rejected_head_sha text
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; parent backlog_task%ROWTYPE; v backlog_verdict;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'remediation', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    SELECT * INTO parent FROM backlog_task b WHERE b.task_id = p_parent_task_id;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(p_task_id, 'remediation', 'rejected', 'unknown_parent_task',
                               jsonb_build_object('requested_parent_task_id', p_parent_task_id));
        v.reason := 'unknown_parent_task'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF p_task_id = p_parent_task_id THEN
        PERFORM _backlog_audit(p_task_id, 'remediation', 'rejected', 'self_reference');
        v.reason := 'self_reference'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF p_pr_url IS NULL OR length(p_pr_url) = 0
       OR p_rejected_head_sha IS NULL OR length(p_rejected_head_sha) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'remediation', 'rejected', 'empty_value');
        v.reason := 'empty_value'; v.revision := t.revision;
        RETURN v;
    END IF;
    INSERT INTO backlog_task_remediation (task_id, parent_task_id, pr_url, rejected_head_sha)
    VALUES (p_task_id, p_parent_task_id, p_pr_url, p_rejected_head_sha)
    ON CONFLICT (task_id) DO NOTHING;
    PERFORM _backlog_audit(p_task_id, 'remediation', 'granted', p_parent_task_id);
    v.ok := true; v.reason := 'recorded'; v.revision := t.revision;
    RETURN v;
END
$$;
