-- AICC PostgreSQL — ingest must read the task's own outcome, not the
-- queue's (VOYN-W0-AICC-INGEST-SUCCEEDED-MEANS-NOT-REDELIVERED).
--
-- The worker daemon's `queue_state = 'succeeded'` means "this attempt is
-- terminal, do not redeliver it" — deliberately true even when the agent's
-- own run failed or timed out (command_center/worker/handlers.py:
-- redelivering an already-executed mutating run would re-apply its side
-- effects; pinned by tests/worker/test_handlers.py
-- test_agent_failure_is_still_ok_not_redelivered and
-- test_timed_out_run_is_ok_not_redelivered). 0007's
-- `backlog_ingest_results` read that queue-level "succeeded" as task-level
-- success and moved the task straight to READY_TO_REVIEW with whatever
-- pr/sha it could find in the payload (often neither, on a failed run) —
-- proven live 2026-08-20: 4 tasks reached READY_TO_REVIEW with
-- pr=NULL, sha=NULL after result.payload->>'status' = 'failed'
-- (exit_code=1, "Credit balance is too low"), and no path returns them.
--
-- Fix: only `result.payload ->> 'status' = 'completed'` counts as a task
-- outcome eligible for READY_TO_REVIEW. Any other terminal payload status
-- (failed, timed_out, or a succeeded queue item with no payload at all)
-- takes the SAME cascade-exhaustion path 0007 already built for `dead`
-- work items — `backlog_return_to_pool`, OPEN on first exhaustion,
-- DEFER_TO_USER on the second — because a queue-terminal-but-task-failed
-- attempt is exactly that: an exhausted attempt, not a review-ready one.

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

        v_task_status := NULL;
        IF r.q_state = 'succeeded' AND r.result_id IS NOT NULL THEN
            SELECT wr.payload INTO v_result FROM work_result wr
             WHERE wr.result_id = r.result_id;
            v_task_status := nullif(btrim(v_result ->> 'status'), '');
        END IF;

        IF r.q_state = 'succeeded' AND v_task_status = 'completed' THEN
            v_pr  := nullif(btrim(v_result ->> 'pr_url'), '');
            v_sha := nullif(btrim(v_result ->> 'head_sha'), '');
            IF v_pr IS NOT NULL THEN
                PERFORM backlog_record_evidence(r.t_id, 'pr', v_pr);
            END IF;
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
                        'task_status_' || coalesce(v_task_status, 'missing')
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
