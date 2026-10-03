-- Reverse of 0025: the pre-0025 bodies, in which a refusal raises.
--
-- CREATE OR REPLACE rather than a no-op, so an up->down->up cycle actually
-- restores the old definitions and the second up is measurable (the pin
-- tests/db/test_backlog_planner.py keeps on 0009/0011/0012).
--
-- Reinstating these bodies reinstates the defect VOYN-W0-AICC-AUDIT-ROLLBACK-
-- CLASS records: the refusal audit is erased by the exception the refusal
-- itself raises. That is what "reversible" means here, and it is the reason
-- this file exists rather than a stub.

CREATE OR REPLACE FUNCTION backlog_dispatch(
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

    -- Re-check eligibility UNDER the lock: the caller's view snapshot may
    -- have gone stale between read and dispatch.
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

    -- The wave gate (approved decision 1): a later NUMERIC wave yields only
    -- while the earliest unfinished numeric wave still has a dispatchable
    -- candidate. Named lanes bypass. The candidate probe uses the same
    -- eligibility definition as the view, so gate and view cannot drift.
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

    -- WIP limit: repo leases this planner already holds (its global
    -- singleton lease has a different prefix and is excluded by shape).
    SELECT count(*) INTO v_wip FROM backlog_writer_lease w
     WHERE w.owner = p_planner AND w.authority LIKE 'repo:%'
       AND w.expires_at > now();
    IF v_wip >= greatest(p_wip_limit, 1) THEN
        PERFORM _backlog_audit(p_task_id, 'dispatch', 'rejected', 'wip_exhausted',
                               jsonb_build_object('wip', v_wip, 'limit', p_wip_limit));
        v.reason := 'wip_exhausted'; RETURN v;
    END IF;

    -- One writer per repository, machine-held: the lease refusal is the
    -- protocol working, not an error.
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
        -- Unreachable while this row lock is held (we re-checked OPEN above);
        -- kept as a hard error so a future edit that breaks the invariant
        -- rolls the whole act back instead of leaking a lease + work item.
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

CREATE OR REPLACE FUNCTION backlog_split_task(p_parent text, p_children jsonb)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; c jsonb; v_suffix text; v_id text;
        v_title text; v_body text; v_priority text; v_ids text[] := '{}'; uv record;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_parent FOR UPDATE;
    IF NOT FOUND THEN v.reason := 'unknown_task'; RETURN v; END IF;
    IF t.status <> 'IN_PROGRESS' THEN
        v.reason := 'not_in_progress'; v.revision := t.revision; RETURN v;
    END IF;
    IF jsonb_typeof(p_children) <> 'array' OR jsonb_array_length(p_children) < 2
       OR jsonb_array_length(p_children) > 8 THEN
        v.reason := 'children_must_be_2_to_8'; RETURN v;
    END IF;
    FOR c IN SELECT * FROM jsonb_array_elements(p_children) LOOP
        v_suffix := upper(btrim(c ->> 'suffix'));
        v_title := btrim(c ->> 'title');
        v_body := coalesce(c ->> 'body', '');
        v_priority := coalesce(nullif(btrim(c ->> 'priority'), ''), t.priority);
        IF v_suffix IS NULL OR v_suffix !~ '^[A-Z0-9][A-Z0-9-]{1,39}$' THEN
            v.reason := 'invalid_suffix'; RETURN v;
        END IF;
        IF v_title IS NULL OR length(v_title) < 8 OR length(v_body) < 20 THEN
            v.reason := 'child_title_or_body_too_short'; RETURN v;
        END IF;
        IF v_priority !~ '^P[0-9]$' THEN v.reason := 'invalid_priority'; RETURN v; END IF;
        v_id := p_parent || '-' || v_suffix;
        IF v_id = ANY (v_ids) THEN v.reason := 'duplicate_suffix'; RETURN v; END IF;
        IF EXISTS (SELECT 1 FROM backlog_task b WHERE b.task_id = v_id) THEN
            v.reason := 'child_exists: ' || v_id; RETURN v;
        END IF;
        v_ids := v_ids || v_id;
    END LOOP;
    FOR c IN SELECT * FROM jsonb_array_elements(p_children) LOOP
        v_suffix := upper(btrim(c ->> 'suffix'));
        v_id := p_parent || '-' || v_suffix;
        SELECT * INTO uv FROM backlog_upsert_task(
            v_id, t.wave, coalesce(nullif(btrim(c ->> 'priority'), ''), t.priority),
            'OPEN', 'task', btrim(c ->> 'title'),
            'Split of ' || p_parent || ' (' || t.title || '). ' || coalesce(c ->> 'body', ''),
            t.repo);
        IF NOT uv.ok THEN
            RAISE EXCEPTION 'split child refused: % (%)', v_id, uv.reason;
        END IF;
        UPDATE backlog_task b SET task_class = t.task_class WHERE b.task_id = v_id;
        INSERT INTO backlog_dependency (task_id, depends_on_task_id)
        VALUES (p_parent, v_id) ON CONFLICT DO NOTHING;
    END LOOP;
    UPDATE backlog_task b SET status = 'SPLIT', revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_parent RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_parent, 'split', 'granted', array_to_string(v_ids, ','),
                           jsonb_build_object('children', to_jsonb(v_ids)));
    v.ok := true; v.reason := array_to_string(v_ids, ',');
    RETURN v;
END
$$;

CREATE OR REPLACE FUNCTION backlog_ingest_results(p_planner text)
    RETURNS TABLE (task_id text, queue_state text, action text, detail jsonb)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; t backlog_task%ROWTYPE; tv backlog_verdict; rv backlog_verdict;
        lv backlog_lease_verdict; v_result jsonb; v_pr text; v_sha text;
        v_task_status text; v_split text; sv backlog_verdict;
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

CREATE OR REPLACE FUNCTION backlog_set_task_class(p_task_id text, p_class text)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_n integer;
BEGIN
    IF p_class NOT IN ('functional', 'pipeline') THEN
        RAISE EXCEPTION 'invalid task class: %', p_class;
    END IF;
    UPDATE backlog_task b SET task_class = p_class, revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id AND b.task_class <> p_class;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    IF v_n > 0 THEN
        PERFORM _backlog_audit(p_task_id, 'task_class', 'granted', p_class);
    END IF;
    RETURN v_n > 0;
END
$$;

DROP FUNCTION IF EXISTS _backlog_lease_release_if_idle(text, text, text);
