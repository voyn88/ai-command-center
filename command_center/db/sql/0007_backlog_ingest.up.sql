-- AICC PostgreSQL — result ingest: the execution outcome returns to the
-- backlog in the same machine act (VOYN-W0-BACKLOG-ORCHESTRATOR BO-S3).
--
-- BO-S2's `backlog_release_terminal` freed the lane and deliberately left
-- the STATUS untouched; this migration replaces it (expand-contract inside
-- the same unreleased stack: 0006 shipped it in #327, nothing deployed runs
-- it) with the full ingest act:
--
-- * succeeded work item  -> evidence rows (pr, sha — read from the result
--   the worker persisted, never from a claim) + IN_PROGRESS ->
--   READY_TO_REVIEW through the EXISTING transition function + lane freed —
--   one transaction. Statuses are never written by a human coordinator.
-- * dead work item -> the task is a FINDING, not a loss: first exhaustion
--   returns it to OPEN (the world may have been transient — a fresh
--   dispatch epoch gets a new revision, hence a new idempotency key and a
--   fresh cascade budget); a SECOND exhaustion parks it in DEFER_TO_USER —
--   two full cascade budgets failing is a human's decision point, and an
--   OPEN<->dead pump would otherwise burn the fleet on one broken task.
-- * DONE stays exactly where BO-S1 put it: the merge is an external fact,
--   and when it is observed, READY_TO_REVIEW -> DONE goes through
--   `backlog_transition`, whose evidence gate the pr/sha rows recorded here
--   are precisely what satisfies.

DROP FUNCTION IF EXISTS backlog_release_terminal(text);

-- The system's return-to-pool. NOT a widening of backlog_transition: writers
-- still cannot move a task backwards; this is a distinct audited act owned
-- by the ingest path, the way queue_redrive is the DLQ's exit.
CREATE FUNCTION backlog_return_to_pool(p_task_id text, p_reason text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_target text; v_prior integer;
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
    v_target := CASE WHEN v_prior >= 1 THEN 'DEFER_TO_USER' ELSE 'OPEN' END;

    UPDATE backlog_task b
       SET status = v_target, revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'return_to_pool', 'granted', p_reason,
                           jsonb_build_object('target', v_target,
                                              'prior_returns', v_prior));
    v.ok := true; v.reason := v_target;
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- backlog_ingest_results — the planner tick's ingest phase.
-- ---------------------------------------------------------------------------
CREATE FUNCTION backlog_ingest_results(p_planner text)
    RETURNS TABLE (task_id text, queue_state text, action text, detail jsonb)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; t backlog_task%ROWTYPE; tv backlog_verdict; rv backlog_verdict;
        lv backlog_lease_verdict; v_result jsonb; v_pr text; v_sha text;
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

        IF r.q_state = 'succeeded' THEN
            v_pr := NULL; v_sha := NULL;
            IF r.result_id IS NOT NULL THEN
                SELECT wr.payload INTO v_result FROM work_result wr
                 WHERE wr.result_id = r.result_id;
                v_pr  := nullif(btrim(v_result ->> 'pr_url'), '');
                v_sha := nullif(btrim(v_result ->> 'head_sha'), '');
            END IF;
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
                'cascade_exhausted: ' || coalesce(
                    (SELECT i2.dead_reason FROM work_item i2
                      WHERE i2.task_id = r.t_id
                      ORDER BY i2.created_at DESC LIMIT 1), 'unspecified'));
            IF NOT rv.ok THEN
                RAISE EXCEPTION 'ingest return refused: %', rv.reason;
            END IF;
            action := CASE rv.reason WHEN 'DEFER_TO_USER'
                      THEN 'parked_for_owner' ELSE 'returned_to_pool' END;
            detail := jsonb_build_object('target', rv.reason);
        END IF;

        lv := backlog_lease_release('repo:' || r.repo, p_planner);
        PERFORM _backlog_audit(r.t_id, 'ingest', 'granted', action,
                               detail || jsonb_build_object('lease_released', lv.ok));
        RETURN NEXT;
    END LOOP;
END
$$;
