-- Revert BO-S3 ingest; restore BO-S2's lane-release-only behaviour.
DROP FUNCTION IF EXISTS backlog_ingest_results(text);
DROP FUNCTION IF EXISTS backlog_return_to_pool(text, text);
CREATE FUNCTION backlog_release_terminal(p_planner text)
    RETURNS TABLE (task_id text, queue_state text, action text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; lv backlog_lease_verdict;
BEGIN
    FOR r IN
        SELECT t.task_id AS t_id, t.repo, i.state AS q_state
          FROM backlog_task t
          JOIN backlog_writer_lease w
            ON w.authority = 'repo:' || t.repo AND w.owner = p_planner
          JOIN LATERAL (
              SELECT i.state FROM work_item i
               WHERE i.task_id = t.task_id
               ORDER BY i.created_at DESC LIMIT 1) i ON true
         WHERE t.status = 'IN_PROGRESS'
           AND i.state IN ('succeeded', 'dead')
    LOOP
        lv := backlog_lease_release('repo:' || r.repo, p_planner);
        PERFORM _backlog_audit(r.t_id, 'dispatch_terminal', 'granted', r.q_state,
                               jsonb_build_object('lease_released', lv.ok));
        task_id := r.t_id; queue_state := r.q_state; action := 'lease_released';
        RETURN NEXT;
    END LOOP;
END
$$;
