-- AICC PostgreSQL — ingest must require a real PR, not just a clean exit
-- (VOYN-W0-AICC-INGEST-REQUIRES-REAL-PR-NOT-JUST-COMPLETED).
--
-- 0009 already closed one gap in this same function: a queue-terminal
-- "succeeded" work_item whose TASK actually failed (agent crashed, hit an
-- API error, timed out) was being read as task-level success and sent
-- straight to READY_TO_REVIEW with whatever pr/sha it could find --
-- usually neither. 0009's fix was to require `result.status = 'completed'`
-- (the CLI process itself exited cleanly) before trusting the payload at
-- all.
--
-- That fix was necessary but not sufficient. Proven live 2026-08-21 running
-- this session's own first genuine end-to-end test through the real queue
-- (VOYN-W0-AICC-DOCUMENT-IMPORT-EXEMPTION-GRADUATION, deliberately small and
-- low-risk, added specifically to get honest proof the pipeline works end
-- to end rather than trusting unit tests alone): the agent ran cleanly
-- (`status = 'completed'`) and reported a real `HEAD_SHA` trailer, but
-- `publish_run`'s own `git push` failed underneath it (`lease_unavailable`
-- -- a stale writer-lease left behind by a dead worker process, unrelated
-- to this bug but what exposed it). `result.pr_url` was `NULL`, yet this
-- function's only condition was `status = 'completed'` -- it recorded the
-- `sha` evidence, transitioned the task to READY_TO_REVIEW anyway, and left
-- it there permanently: `review_once`/`publish_review_verdicts`/`merge_once`
-- all `JOIN backlog_evidence ON kind = 'pr'`, which this task will never
-- have. A completed-but-unpublished run is not review-ready; it is exactly
-- as exhausted an attempt as a failed or timed-out one, and belongs on the
-- same `backlog_return_to_pool` path 0007/0009 already built for those.
--
-- Fix: READY_TO_REVIEW now requires BOTH `status = 'completed'` AND a
-- non-empty `pr_url` in the same payload. Everything else -- including a
-- clean run whose publish step failed -- takes the cascade-exhaustion path.

DROP FUNCTION IF EXISTS backlog_ingest_results(text);

CREATE FUNCTION backlog_ingest_results(p_planner text)
    RETURNS TABLE (task_id text, queue_state text, action text, detail jsonb)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; t backlog_task%ROWTYPE; tv backlog_verdict; rv backlog_verdict;
        lv backlog_lease_verdict; v_result jsonb; v_pr text; v_sha text;
        v_task_status text;
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

        IF r.q_state = 'succeeded' AND v_task_status = 'completed' AND v_pr IS NOT NULL THEN
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
                            ELSE 'no_pr_published'
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
