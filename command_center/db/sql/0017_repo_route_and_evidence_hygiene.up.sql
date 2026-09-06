-- VOYN-W0-AICC-REPO-ROUTE-AND-EVIDENCE-HYGIENE.
--
-- Two independent sources of a task stuck forever, neither ever escalated:
--
-- 1. **A repo the planner can never route.** `unknown_repo_route` (and
--    `no_repo`) are report lines only (planner.py) -- correct, in that an
--    unroutable repo must never dispatch into a guaranteed dead-letter, but
--    incomplete, in that nothing ever moves the task off OPEN either. A repo
--    typo or a genuinely repo-less task (an idea, a decision record filed as
--    an executable task by mistake) sits reported, identically, forever.
--    `backlog_park_unroutable` is the machine's answer: the planner calls it
--    on every tick it cannot route a candidate, and once the SAME OPEN spell
--    has been unroutable continuously for `p_grace_seconds` (default 24h --
--    long enough for an operator to add a missing route without a task
--    flapping into NEEDS_REFINEMENT for what was really a two-minute
--    config gap), it parks the task NEEDS_REFINEMENT instead of reporting
--    it again next tick. Editing the task (repo field included), or any
--    other granted mutation, buys a fresh grace window -- an old streak
--    from before a fix must never park a task the fix already resolved.
--
-- 2. **'pr' evidence that was never a real, resolvable PR.** Every
--    downstream gate (review_once, publish_review_verdicts, merge_once)
--    resolves 'pr' evidence through the live GitHub API; a value that can
--    never resolve -- a self-reported URL an agent garbled in its own
--    result text (not the trusted `gh pr list`-derived path
--    `reconcile_pr_evidence` already uses), or a hand-inserted one from an
--    operator recovering a stuck task by hand -- produces the exact same
--    `pr_view_failed`/`pr_diff_fetch_failed` skip line every tick,
--    permanently, because nothing ever removes it. Two closes:
--    * At record time: `backlog_ingest_results` now refuses to record a
--      `pr_url` that is not shaped like a real GitHub PR URL
--      (`https://github.com/<owner>/<repo>/pull/<digits>`) -- treated
--      exactly like "no PR published", an exhausted cascade attempt, never
--      evidence.
--    * On each tick: `backlog_clear_evidence` lets the orchestrator (which
--      has the `gh` access to actually resolve a URL) remove a value it has
--      PROVEN can never become a review -- never one it merely failed to
--      look up this one time, which stays exactly as retryable as before.
--      Clearing the LAST 'pr' row on a READY_TO_REVIEW task would otherwise
--      trade one dead end for another -- no work_item is running for a
--      READY_TO_REVIEW task, so nothing would ever ingest it a fresh pr_url,
--      and it would sit invisible (no evidence to join on) instead of merely
--      skipped. `backlog_clear_evidence` closes that too: the same call that
--      empties a task's 'pr' evidence also sends it back for a fresh
--      attempt -- OPEN the first time, DEFER_TO_USER on a repeat (the same
--      escalation `backlog_return_to_pool` already uses for IN_PROGRESS
--      exhaustion), so a task that keeps producing bad evidence stops
--      silently re-dispatching into the same failure.

-- ---------------------------------------------------------------------------
-- backlog_park_unroutable -- OPEN -> NEEDS_REFINEMENT for a repo the
-- planner has been unable to route for a sustained period.
-- ---------------------------------------------------------------------------
CREATE FUNCTION backlog_park_unroutable(
    p_task_id text, p_reason text, p_grace_seconds integer DEFAULT 86400
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    t backlog_task%ROWTYPE;
    v backlog_verdict;
    v_first_seen timestamptz;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'repo_route', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF t.kind = 'gate' THEN
        PERFORM _backlog_audit(p_task_id, 'repo_route', 'rejected', 'gate_is_control_record');
        v.reason := 'gate_is_control_record'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF t.status <> 'OPEN' THEN
        PERFORM _backlog_audit(p_task_id, 'repo_route', 'rejected', 'not_open',
                               jsonb_build_object('status', t.status));
        v.reason := 'not_open'; v.revision := t.revision;
        RETURN v;
    END IF;

    -- The current streak's start: the earliest 'repo_route' rejection on
    -- record since the last GRANTED mutation that could plausibly have
    -- fixed it (the repo field changing via upsert, or the task moving at
    -- all). Without this boundary a task fixed and re-broken later would
    -- inherit its first, long-expired streak and park on the very next
    -- tick instead of getting its own fresh grace window.
    SELECT min(e.created_at) INTO v_first_seen
      FROM backlog_event e
     WHERE e.task_id = p_task_id
       AND e.event = 'repo_route' AND e.outcome = 'rejected'
       AND NOT EXISTS (
           SELECT 1 FROM backlog_event e2
            WHERE e2.task_id = p_task_id AND e2.outcome = 'granted'
              AND e2.event IN ('upsert', 'transition', 'triage',
                               'dispatch', 'return_to_pool', 'resume_deferred')
              AND e2.event_id > e.event_id);

    PERFORM _backlog_audit(p_task_id, 'repo_route', 'rejected', p_reason);

    IF v_first_seen IS NULL
       OR now() - v_first_seen < make_interval(secs => greatest(p_grace_seconds, 0)) THEN
        v.ok := true; v.reason := 'observed'; v.revision := t.revision;
        RETURN v;
    END IF;

    UPDATE backlog_task b
       SET status = 'NEEDS_REFINEMENT', revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'repo_route', 'granted', p_reason,
                           jsonb_build_object('target', 'NEEDS_REFINEMENT',
                                              'unrouted_since', v_first_seen));
    v.ok := true; v.reason := 'NEEDS_REFINEMENT';
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- backlog_clear_evidence -- remove one evidence row the caller has already
-- proven can never become real evidence. Idempotent: a value already gone
-- (a race with another tick, a re-run) is success, not a refusal. Emptying
-- the last 'pr' row on a READY_TO_REVIEW task also sends the task back for
-- a fresh attempt -- see the migration header comment.
-- ---------------------------------------------------------------------------
CREATE FUNCTION backlog_clear_evidence(
    p_task_id text, p_kind text, p_value text, p_reason text
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_deleted integer;
        v_remaining integer; v_prior integer; v_target text;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'evidence_clear', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF p_kind NOT IN ('pr', 'sha', 'ci', 'acceptance') THEN
        PERFORM _backlog_audit(p_task_id, 'evidence_clear', 'rejected', 'unknown_kind',
                               jsonb_build_object('kind', p_kind));
        v.reason := 'unknown_kind'; v.revision := t.revision;
        RETURN v;
    END IF;

    DELETE FROM backlog_evidence e
     WHERE e.task_id = p_task_id AND e.kind = p_kind AND e.value = p_value;
    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    PERFORM _backlog_audit(p_task_id, 'evidence_clear', 'granted',
                           coalesce(p_reason, 'unspecified'),
                           jsonb_build_object('kind', p_kind, 'value', p_value,
                                              'deleted', v_deleted > 0));
    v.ok := true;
    v.reason := CASE WHEN v_deleted > 0 THEN 'cleared' ELSE 'already_absent' END;
    v.revision := t.revision;

    IF v_deleted > 0 AND p_kind = 'pr' AND t.status = 'READY_TO_REVIEW' THEN
        SELECT count(*) INTO v_remaining FROM backlog_evidence e
         WHERE e.task_id = p_task_id AND e.kind = 'pr';
        IF v_remaining = 0 THEN
            SELECT count(*) INTO v_prior FROM backlog_event e
             WHERE e.task_id = p_task_id AND e.event = 'evidence_clear_return'
               AND e.outcome = 'granted';
            v_target := CASE WHEN v_prior >= 1 THEN 'DEFER_TO_USER' ELSE 'OPEN' END;
            UPDATE backlog_task b
               SET status = v_target, revision = b.revision + 1, updated_at = now()
             WHERE b.task_id = p_task_id
            RETURNING b.revision INTO v.revision;
            PERFORM _backlog_audit(p_task_id, 'evidence_clear_return', 'granted', p_reason,
                                   jsonb_build_object('target', v_target,
                                                      'prior_returns', v_prior));
            v.reason := 'cleared_and_' || lower(v_target);
        END IF;
    END IF;
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- backlog_ingest_results -- 0011's version plus a shape gate on a
-- self-reported pr_url. Everything else is byte-for-byte 0011.
-- ---------------------------------------------------------------------------
DROP FUNCTION IF EXISTS backlog_ingest_results(text);

CREATE FUNCTION backlog_ingest_results(p_planner text)
    RETURNS TABLE (task_id text, queue_state text, action text, detail jsonb)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; t backlog_task%ROWTYPE; tv backlog_verdict; rv backlog_verdict;
        lv backlog_lease_verdict; v_result jsonb; v_pr text; v_sha text;
        v_task_status text; v_pr_shape_ok boolean;
BEGIN
    FOR r IN
        SELECT t2.task_id AS t_id, t2.repo, i.state AS q_state, i.result_id
          FROM backlog_task t2
          JOIN LATERAL (
              SELECT i.state, i.result_id FROM work_item i
               WHERE i.task_id = t2.task_id
               ORDER BY i.created_at DESC LIMIT 1) i ON true
         WHERE t2.status = 'IN_PROGRESS'
           AND i.state IN ('succeeded', 'dead')
    LOOP
        SELECT * INTO t FROM backlog_task b WHERE b.task_id = r.t_id FOR UPDATE;
        task_id := r.t_id; queue_state := r.q_state; detail := NULL;

        v_task_status := NULL; v_pr := NULL; v_sha := NULL;
        IF r.q_state = 'succeeded' AND r.result_id IS NOT NULL THEN
            SELECT wr.payload INTO v_result FROM work_result wr
             WHERE wr.result_id = r.result_id;
            v_task_status := nullif(btrim(v_result ->> 'status'), '');
            v_pr  := nullif(btrim(v_result ->> 'pr_url'), '');
            v_sha := nullif(btrim(v_result ->> 'head_sha'), '');
        END IF;

        -- Untrusted input from the agent's own result text, not something
        -- GitHub has vouched for (unlike `reconcile_pr_evidence`, which only
        -- ever records a URL `gh pr list` itself returned). A value not even
        -- shaped like a real PR URL can never resolve through the GitHub API
        -- every downstream gate uses -- recording it would move the task to
        -- READY_TO_REVIEW and strand it there, invisible to every gate,
        -- forever.
        v_pr_shape_ok := v_pr IS NULL
            OR v_pr ~ '^https://github\.com/[^/]+/[^/]+/pull/[1-9][0-9]*$';

        IF r.q_state = 'succeeded' AND v_task_status = 'completed'
           AND v_pr IS NOT NULL AND v_pr_shape_ok THEN
            PERFORM backlog_record_evidence(r.t_id, 'pr', v_pr);
            IF v_sha IS NOT NULL THEN
                PERFORM backlog_record_evidence(r.t_id, 'sha', v_sha);
            END IF;
            tv := backlog_transition(r.t_id, 'READY_TO_REVIEW', t.revision);
            IF NOT tv.ok THEN
                RAISE EXCEPTION 'ingest transition refused: %', tv.reason;
            END IF;
            action := 'ready_to_review';
            detail := jsonb_build_object('pr', v_pr, 'sha', v_sha);
        ELSE
            rv := backlog_return_to_pool(
                r.t_id,
                'cascade_exhausted: ' || CASE
                    WHEN r.q_state = 'succeeded' THEN
                        CASE
                            WHEN v_task_status IS DISTINCT FROM 'completed'
                                THEN 'task_status_' || coalesce(v_task_status, 'missing')
                            WHEN v_pr IS NULL THEN 'no_pr_published'
                            ELSE 'malformed_pr_url'
                        END
                    ELSE
                        coalesce(
                            (SELECT i2.dead_reason FROM work_item i2
                              WHERE i2.task_id = r.t_id
                              ORDER BY i2.created_at DESC LIMIT 1), 'unspecified')
                    END);
            IF NOT rv.ok THEN
                RAISE EXCEPTION 'ingest return refused: %', rv.reason;
            END IF;
            action := CASE rv.reason WHEN 'DEFER_TO_USER'
                      THEN 'parked_for_owner' ELSE 'returned_to_pool' END;
            detail := jsonb_build_object('target', rv.reason,
                                         'task_status', v_task_status);
        END IF;

        lv := backlog_lease_release('repo:' || r.repo, p_planner);
        PERFORM _backlog_audit(r.t_id, 'ingest', 'granted', action,
                               detail || jsonb_build_object('lease_released', lv.ok));
        RETURN NEXT;
    END LOOP;
END
$$;
