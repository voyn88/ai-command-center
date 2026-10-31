-- AICC PostgreSQL — minor (non-blocking) review findings become a tracked
-- follow-up task instead of disappearing once the audit comment scrolls off
-- (VOYN-W0-AICC-REVIEW-FULLCONTEXT-TRIAGE).
--
-- VOYN-W0-AICC-REVIEW-AUTO-ACCEPT (93f205d) taught finding verification to
-- classify every rejecting finding against the real tree at the exact head
-- and override a chunk-isolation REJECT when nothing survives as
-- CONFIRMED_BLOCKING -- the fix for a zero-tool chunk reviewer flagging
-- cross-chunk context or minor issues as REJECT. What that migration did
-- NOT do is keep the CONFIRMED_MINOR findings anywhere but a GitHub PR
-- comment: real, non-blocking issues the verifier itself confirmed true
-- (style, naming, test hygiene, optional hardening) were surfaced once and
-- then had no home -- no backlog entry, nothing the planner could ever
-- dispatch. This table is that home: at most one row per (auto-accepted)
-- parent task, pointing at the new OPEN follow-up task the minor findings
-- were copied into. The parent keeps merging zero-touch regardless of
-- whether this write succeeds -- see review_merge.py's
-- `_record_minor_followup` docstring for why this is best-effort and never
-- gates the acceptance marker.
--
-- Modelled on backlog_task_remediation (0010), not merged into it: that
-- table's lineage means "task_id fixes parent_task_id's REJECTED verdict,"
-- and its own comment documents relying on that meaning (the planner's
-- dependency-free dispatch, the depth-limited chain). A minor-findings
-- follow-up is a different relationship -- the parent is never REJECTED and
-- is not blocked by the follow-up, and the follow-up fixes nothing that
-- failed review -- so it gets its own table rather than overloading the
-- existing one's semantics.
CREATE TABLE backlog_task_followup (
    task_id           text NOT NULL PRIMARY KEY REFERENCES backlog_task(task_id),
    parent_task_id    text NOT NULL REFERENCES backlog_task(task_id),
    pr_url            text NOT NULL,
    head_sha          text NOT NULL,
    kind              text NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT backlog_task_followup_not_self CHECK (task_id <> parent_task_id),
    -- One vocabulary member today (minor findings auto-accept confirmed
    -- real but non-blocking); a future follow-up kind adds a value here
    -- rather than reusing this one for a different meaning.
    CONSTRAINT backlog_task_followup_kind_vocabulary CHECK (kind IN ('minor_findings'))
);

-- At most one minor-findings follow-up per parent: a task can only be
-- auto-accepted once (the marker check short-circuits every later tick,
-- see review_merge.py's publish_review_verdicts), so a second row for the
-- same (parent, kind) would only ever be a retried write after the first
-- already landed.
CREATE UNIQUE INDEX idx_backlog_task_followup_parent_kind
    ON backlog_task_followup(parent_task_id, kind);

REVOKE ALL ON backlog_task_followup FROM PUBLIC;

-- ---------------------------------------------------------------------------
-- backlog_record_followup — the one write path onto the lineage table,
-- mirroring backlog_record_remediation's shape: idempotent, audited, and
-- the only way any role (including the control plane's own aicc_app)
-- reaches backlog_task_followup. Direct table access stays revoked from
-- everyone, same as every other backlog table.
-- ---------------------------------------------------------------------------
CREATE FUNCTION backlog_record_followup(
    p_task_id text, p_parent_task_id text, p_pr_url text, p_head_sha text, p_kind text
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; parent backlog_task%ROWTYPE; v backlog_verdict; v_row_count int;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'followup', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    SELECT * INTO parent FROM backlog_task b WHERE b.task_id = p_parent_task_id;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(p_task_id, 'followup', 'rejected', 'unknown_parent_task',
                               jsonb_build_object('requested_parent_task_id', p_parent_task_id));
        v.reason := 'unknown_parent_task'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF p_task_id = p_parent_task_id THEN
        PERFORM _backlog_audit(p_task_id, 'followup', 'rejected', 'self_reference');
        v.reason := 'self_reference'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF p_pr_url IS NULL OR length(p_pr_url) = 0
       OR p_head_sha IS NULL OR length(p_head_sha) = 0
       OR p_kind IS NULL OR length(p_kind) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'followup', 'rejected', 'empty_value');
        v.reason := 'empty_value'; v.revision := t.revision;
        RETURN v;
    END IF;
    INSERT INTO backlog_task_followup (task_id, parent_task_id, pr_url, head_sha, kind)
    VALUES (p_task_id, p_parent_task_id, p_pr_url, p_head_sha, p_kind)
    ON CONFLICT (parent_task_id, kind) DO NOTHING;
    GET DIAGNOSTICS v_row_count = ROW_COUNT;
    IF v_row_count = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'followup', 'rejected', 'followup_already_recorded',
                               jsonb_build_object('parent_task_id', p_parent_task_id, 'kind', p_kind));
        v.reason := 'followup_already_recorded'; v.revision := t.revision;
        RETURN v;
    END IF;
    PERFORM _backlog_audit(p_task_id, 'followup', 'granted', p_parent_task_id);
    v.ok := true; v.reason := 'recorded'; v.revision := t.revision;
    RETURN v;
END
$$;
