-- VOYN-W0-AICC-PLANNER-AUTO-SPLIT-PIPELINE-TASKS (+ deploy smoke, monitor findings)
--
-- 2026-09-08: three pipeline-class tasks the owner ordered fixed (CI gate by
-- review window, installer integration test, versioned rotation) were each
-- returned 4-5 times by fleet lanes without a technical cause and parked
-- DEFER_TO_USER -- the fleet cannot take a task that is too large for one
-- run, and parking it hands the pipeline back to a human. A pipeline task
-- returned twice without a technical cause is now DECOMPOSED: the planner
-- dispatches a split run whose only result is a SPLIT_TASKS_JSON trailer;
-- ingest creates the bounded subtasks (dependencies on the parent, which
-- closes as SPLIT). Only after two failed split attempts does the task park.
--
-- Also here, both small and required by the same "no hands" objective:
--   * backlog_dispatch_smoke(): the read-only preflight self-deploy runs after
--     a migration -- it exercises exactly the privileges backlog_dispatch
--     needs (0019 recreated a view as the wrong owner and every planner tick
--     died for 18 minutes; import-smoke cannot see that).
--   * monitor_finding + monitor_record_finding/monitor_clear_finding: the
--     fail-closed monitors record what they measured; the planner turns open
--     findings into pipeline tasks instead of a permanently failed unit.

CREATE OR REPLACE FUNCTION backlog_return_to_pool(p_task_id text, p_reason text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_target text; v_prior integer;
        v_split_requested boolean := false;
        v_technical boolean;
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
    v_target := CASE
        WHEN v_technical THEN 'OPEN'
        -- Pipeline class: the second non-technical return asks for a split
        -- (planner dispatches decomposition), the fourth parks. Functional
        -- tasks keep the original rule (second return parks).
        WHEN t.task_class = 'pipeline' AND v_prior >= 3 THEN 'DEFER_TO_USER'
        WHEN t.task_class = 'pipeline' AND v_prior >= 1 THEN 'OPEN'
        WHEN v_prior >= 1 THEN 'DEFER_TO_USER'
        ELSE 'OPEN'
    END;
    v_split_requested := (NOT v_technical) AND t.task_class = 'pipeline'
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

-- Decompose a task into bounded subtasks named by a JSON array of
-- {"suffix","title","body"[,"priority"]}. Children inherit wave, repo and
-- (by default) priority; the parent depends on every child and closes as
-- SPLIT. Fail-closed on any malformed entry: nothing is created.
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

-- Read-only preflight with exactly backlog_dispatch's privileges (definer
-- aicc_migrator reading backlog_eligible and the wave candidate). Run by
-- self-deploy after every migration; a refusal here rolls the deploy back.
CREATE OR REPLACE FUNCTION backlog_dispatch_smoke()
    RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_n bigint;
BEGIN
    SELECT count(*) INTO v_n FROM backlog_eligible;
    PERFORM * FROM _backlog_earliest_wave_candidate('smoke');
    RETURN true;
END
$$;
GRANT EXECUTE ON FUNCTION backlog_dispatch_smoke() TO aicc_app;

-- The control plane never updates backlog_task directly; the class is the
-- one machine field the planner sets itself (split children inherit the
-- parent's class inside backlog_split_task; monitor tasks are pipeline).
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
GRANT EXECUTE ON FUNCTION backlog_set_task_class(text, text) TO aicc_app;

-- What a fail-closed monitor measured. One row per (source, failure) while
-- open; the planner turns open rows into pipeline tasks (idempotently) and
-- the monitor clears them when the measurement is healthy again.
CREATE TABLE monitor_finding (
    finding_id  bigserial PRIMARY KEY,
    source      text        NOT NULL,
    failure     text        NOT NULL,
    detail      jsonb,
    state       text        NOT NULL DEFAULT 'open'
                CONSTRAINT monitor_finding_state CHECK (state IN ('open', 'cleared')),
    task_id     text,
    opened_at   timestamptz NOT NULL DEFAULT now(),
    seen_at     timestamptz NOT NULL DEFAULT now(),
    cleared_at  timestamptz
);
CREATE UNIQUE INDEX monitor_finding_open_unique
    ON monitor_finding (source, failure) WHERE state = 'open';

CREATE OR REPLACE FUNCTION monitor_record_finding(p_source text, p_failure text, p_detail jsonb DEFAULT NULL)
    RETURNS bigint
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_id bigint;
BEGIN
    IF p_source !~ '^[A-Za-z0-9._:-]{1,120}$' OR length(p_failure) NOT BETWEEN 1 AND 200 THEN
        RAISE EXCEPTION 'invalid monitor finding';
    END IF;
    INSERT INTO monitor_finding (source, failure, detail)
    VALUES (p_source, p_failure, p_detail)
    ON CONFLICT (source, failure) WHERE state = 'open'
    DO UPDATE SET seen_at = now(), detail = EXCLUDED.detail
    RETURNING finding_id INTO v_id;
    RETURN v_id;
END
$$;

CREATE OR REPLACE FUNCTION monitor_clear_finding(p_source text)
    RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_n integer;
BEGIN
    UPDATE monitor_finding SET state = 'cleared', cleared_at = now()
     WHERE source = p_source AND state = 'open';
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END
$$;
GRANT EXECUTE ON FUNCTION monitor_record_finding(text, text, jsonb) TO aicc_app, aicc_worker;
GRANT EXECUTE ON FUNCTION monitor_clear_finding(text) TO aicc_app, aicc_worker;
GRANT SELECT ON monitor_finding TO aicc_app;

-- The one write the control plane makes to a finding: link the task it filed.
CREATE OR REPLACE FUNCTION monitor_link_task(p_finding_id bigint, p_task_id text)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_n integer;
BEGIN
    IF p_task_id !~ '^[A-Z0-9][A-Z0-9-]{2,120}$' THEN
        RAISE EXCEPTION 'invalid task id for finding link';
    END IF;
    UPDATE monitor_finding SET task_id = p_task_id
     WHERE finding_id = p_finding_id AND state = 'open' AND task_id IS NULL;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n > 0;
END
$$;
GRANT EXECUTE ON FUNCTION monitor_link_task(bigint, text) TO aicc_app;
