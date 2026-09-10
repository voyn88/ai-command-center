-- Restore 0006's backlog_dispatch (transition after enqueue, RAISE on
-- refusal) and 0011's backlog_ingest_results (RAISE on either refusal,
-- unconditional lease release) -- the two raising, lease-unsafe bodies this
-- migration replaced.

DROP FUNCTION IF EXISTS backlog_dispatch(text, text, integer, integer, jsonb, integer);

CREATE FUNCTION backlog_dispatch(
    p_task_id text, p_planner text, p_ttl_seconds integer,
    p_wip_limit integer, p_payload jsonb, p_max_attempts integer DEFAULT 3
) RETURNS backlog_dispatch_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_dispatch_verdict;
        lv backlog_lease_verdict; tv backlog_verdict;
        v_key text; v_wip integer; v_gate record;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'dispatch', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task'; RETURN v;
    END IF;
    v.revision := t.revision;

    IF t.kind <> 'task' OR t.status <> 'OPEN' THEN
        PERFORM _backlog_audit(p_task_id, 'dispatch', 'rejected', 'not_eligible',
                               jsonb_build_object('status', t.status, 'kind', t.kind));
        v.reason := 'not_eligible'; RETURN v;
    END IF;
    IF EXISTS (SELECT 1 FROM backlog_dependency d
                 JOIN backlog_task dep ON dep.task_id = d.depends_on_task_id
                WHERE d.task_id = p_task_id AND dep.status <> 'DONE') THEN
        PERFORM _backlog_audit(p_task_id, 'dispatch', 'rejected', 'dependencies_unsatisfied');
        v.reason := 'dependencies_unsatisfied'; RETURN v;
    END IF;
    IF t.repo IS NULL THEN
        PERFORM _backlog_audit(p_task_id, 'dispatch', 'rejected', 'no_repo');
        v.reason := 'no_repo'; RETURN v;
    END IF;

    IF t.wave ~ '^[0-9]+(\.[0-9]+)?$' THEN
        SELECT * INTO v_gate FROM _backlog_earliest_wave_candidate(p_planner);
        IF FOUND AND v_gate.wave::numeric < t.wave::numeric THEN
            PERFORM _backlog_audit(p_task_id, 'dispatch', 'rejected',
                                   'earlier_wave_has_eligible_work',
                                   jsonb_build_object('earliest_wave', v_gate.wave,
                                                      'candidate', v_gate.task_id));
            v.reason := 'earlier_wave_has_eligible_work'; RETURN v;
        END IF;
    END IF;

    SELECT count(*) INTO v_wip FROM backlog_writer_lease w
     WHERE w.owner = p_planner AND w.authority LIKE 'repo:%'
       AND w.expires_at > now();
    IF v_wip >= greatest(p_wip_limit, 1) THEN
        PERFORM _backlog_audit(p_task_id, 'dispatch', 'rejected', 'wip_exhausted',
                               jsonb_build_object('wip', v_wip, 'limit', p_wip_limit));
        v.reason := 'wip_exhausted'; RETURN v;
    END IF;

    lv := backlog_lease_acquire('repo:' || t.repo, p_planner, p_ttl_seconds);
    IF NOT lv.ok THEN
        PERFORM _backlog_audit(p_task_id, 'dispatch', 'rejected', 'repo_busy',
                               jsonb_build_object('repo', t.repo, 'holder', lv.owner));
        v.reason := 'repo_busy'; RETURN v;
    END IF;

    v_key := 'dispatch:' || p_task_id || ':' || t.revision;
    v.work_item_id := queue_enqueue(
        'execution', v_key, p_payload, p_task_id, t.repo, p_max_attempts);

    tv := backlog_transition(p_task_id, 'IN_PROGRESS', t.revision);
    IF NOT tv.ok THEN
        RAISE EXCEPTION 'dispatch transition refused: %', tv.reason;
    END IF;
    v.revision := tv.revision;

    PERFORM _backlog_audit(p_task_id, 'dispatch', 'granted', NULL,
                           jsonb_build_object('work_item_id', v.work_item_id,
                                              'idempotency_key', v_key,
                                              'repo', t.repo,
                                              'planner', p_planner));
    v.ok := true;
    RETURN v;
END
$$;


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
