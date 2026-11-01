-- Bounded cleanup of malformed PR evidence. Route gaps are reported only:
-- the planner must not infer a continuous outage from stale task events.

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
-- Give a malformed self-reported PR one retry, then defer; never split it.
-- Transient infrastructure failures retain their existing operational behavior.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION backlog_return_to_pool(p_task_id text, p_reason text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_target text; v_prior integer;
        v_split_requested boolean := false;
        v_technical boolean; v_malformed boolean; v_bad_returns integer;
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
        OR p_reason LIKE 'cascade_exhausted: publish_%';
    v_malformed := p_reason LIKE 'cascade_exhausted: malformed_pr_url%';
    SELECT count(*) INTO v_bad_returns FROM backlog_event e
     WHERE e.task_id = p_task_id AND e.event = 'return_to_pool'
       AND e.outcome = 'granted'
       AND e.reason LIKE 'cascade_exhausted: malformed_pr_url%';
    v_target := CASE
        WHEN v_malformed AND v_bad_returns >= 1 THEN 'DEFER_TO_USER'
        WHEN v_malformed THEN 'OPEN'
        WHEN v_technical THEN 'OPEN'
        WHEN t.task_class = 'pipeline' AND v_prior >= 3 THEN 'DEFER_TO_USER'
        WHEN t.task_class = 'pipeline' AND v_prior >= 1 THEN 'OPEN'
        WHEN v_prior >= 1 THEN 'DEFER_TO_USER'
        ELSE 'OPEN'
    END;
    v_split_requested := (NOT v_technical) AND (NOT v_malformed) AND t.task_class = 'pipeline'
                         AND v_prior >= 1 AND v_target = 'OPEN';

    UPDATE backlog_task b
       SET status = v_target, revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'return_to_pool', 'granted', p_reason,
                           jsonb_build_object('target', v_target,
                                              'prior_returns', v_prior,
                                              'technical', v_technical,
                                              'split_requested', v_split_requested));
    v.ok := true; v.reason := v_target;
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- backlog_ingest_results -- 0021's split-aware body plus a shape gate on a
-- self-reported pr_url.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION backlog_ingest_results(p_planner text)
    RETURNS TABLE (task_id text, queue_state text, action text, detail jsonb)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; t backlog_task%ROWTYPE; tv backlog_verdict; rv backlog_verdict;
        lv backlog_lease_verdict; v_result jsonb; v_pr text; v_sha text;
        v_task_status text; v_split text; sv backlog_verdict;
        v_pr_shape_ok boolean;
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

        v_task_status := NULL; v_pr := NULL; v_sha := NULL; v_result := NULL;
        IF r.q_state = 'succeeded' AND r.result_id IS NOT NULL THEN
            SELECT wr.payload INTO v_result FROM work_result wr
             WHERE wr.result_id = r.result_id;
            v_task_status := nullif(btrim(v_result ->> 'status'), '');
            v_pr  := nullif(btrim(v_result ->> 'pr_url'), '');
            v_sha := nullif(btrim(v_result ->> 'head_sha'), '');
        END IF;

        -- A split run (the planner's decomposition mode for a pipeline task the
        -- fleet returned twice without a technical cause) reports no PR: its
        -- result is a SPLIT_TASKS_JSON trailer naming bounded subtasks. Ingest
        -- creates them and closes the parent as SPLIT instead of parking it
        -- for the owner (VOYN-W0-AICC-PLANNER-AUTO-SPLIT-PIPELINE-TASKS).
        v_split := NULL;
        IF r.q_state = 'succeeded' AND v_result ? 'result_text' THEN
            -- One line, bounded at end-of-line: prose after the array on a
            -- later line cannot widen the capture.
            v_split := substring(v_result ->> 'result_text'
                                 from 'SPLIT_TASKS_JSON:[[:space:]]*(\[[^\n]*\])');
        END IF;
        IF v_split IS NOT NULL THEN
            BEGIN
                sv := backlog_split_task(r.t_id, v_split::jsonb);
            EXCEPTION
                -- Any refusal of one task's split -- bad JSON, a child the
                -- upsert refused, a constraint -- is that task's finding, never
                -- the batch's: the loop continues and the task falls through
                -- to the ordinary return-to-pool path below.
                WHEN OTHERS THEN
                    sv.ok := false; sv.reason := 'split_refused: ' || left(SQLERRM, 160);
            END;
            IF sv.ok THEN
                action := 'split';
                detail := jsonb_build_object('children', sv.reason);
                lv := backlog_lease_release('repo:' || r.repo, p_planner);
                PERFORM _backlog_audit(r.t_id, 'ingest', 'granted', action,
                                       detail || jsonb_build_object('lease_released', lv.ok));
                RETURN NEXT;
                CONTINUE;
            END IF;
            PERFORM _backlog_audit(r.t_id, 'split', 'rejected', sv.reason,
                                   jsonb_build_object('trailer', left(v_split, 400)));
        END IF;

        -- Untrusted input from the agent's own result text, not something
        -- GitHub has vouched for (unlike `reconcile_pr_evidence`, which only
        -- ever records a URL `gh pr list` itself returned). A value not even
        -- shaped like a real PR URL can never resolve through the GitHub API
        -- every downstream gate uses -- recording it would move the task to
        -- READY_TO_REVIEW and strand it there, invisible to every gate,
        -- forever.
        v_pr_shape_ok := v_pr IS NULL
            OR v_pr ~ '^https://github\.com/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+/pull/[1-9][0-9]*$';

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
