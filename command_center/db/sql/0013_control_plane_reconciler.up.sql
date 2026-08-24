-- Durable control-plane state for VOYN-W0-AICC-CONTROL-PLANE-RECONCILER.
--
-- Timers are wake-up mechanisms, not memory.  The previous control plane kept
-- its only "next step" in the fact that a particular timer happened to be
-- enabled.  When planner/review timers stopped, green work therefore had no
-- durable owner, deadline or recovery path.  These rows are the authority for
-- delivery progress; systemd merely calls the reconciler that advances them.

CREATE TABLE control_plane_lane (
    task_id          text PRIMARY KEY REFERENCES backlog_task(task_id),
    state            text NOT NULL DEFAULT 'READY'
                     CHECK (state IN ('READY', 'RUNNING', 'WAITING', 'BLOCKED', 'DONE')),
    next_action      text NOT NULL
                     CHECK (next_action IN (
                         'GUARDED_PUBLISH', 'CI_WAIT', 'INDEPENDENT_REVIEW',
                         'ACCEPTANCE', 'MERGE', 'DEPLOY', 'BACKLOG_SYNC', 'NONE'
                     )),
    owner            text NOT NULL,
    claimant         text,
    deadline_at      timestamptz NOT NULL,
    heartbeat_at     timestamptz,
    progress_at      timestamptz NOT NULL DEFAULT now(),
    progress_token   text,
    interrupt_requested_at timestamptz,
    lease_expires_at timestamptz,
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 100),
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    blocked_by       text,
    last_error       text,
    revision         bigint NOT NULL DEFAULT 1,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CHECK ((state = 'RUNNING') = (claimant IS NOT NULL)),
    CHECK ((state = 'RUNNING') = (lease_expires_at IS NOT NULL))
);

CREATE INDEX idx_control_plane_lane_due
    ON control_plane_lane(state, deadline_at, lease_expires_at);

CREATE TABLE control_plane_event (
    event_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id      text REFERENCES backlog_task(task_id),
    component    text NOT NULL,
    event        text NOT NULL,
    outcome      text NOT NULL,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_control_plane_event_task_time
    ON control_plane_event(task_id, created_at DESC, event_id DESC);

-- One authoritative current delivery attempt per task. Append-only backlog
-- evidence remains an audit log; it is deliberately not used to guess which
-- historical PR/SHA pair is current.
CREATE TABLE control_plane_delivery_attempt (
    delivery_attempt_id text PRIMARY KEY REFERENCES work_attempt(attempt_id),
    work_item_id        text NOT NULL REFERENCES work_item(work_item_id),
    task_id             text NOT NULL REFERENCES backlog_task(task_id),
    worker_role         text NOT NULL,
    worktree_path       text NOT NULL CHECK (worktree_path ~ '^/'),
    head_sha            text NOT NULL CHECK (head_sha ~ '^[0-9a-f]{40}$'),
    pr_url              text NOT NULL CHECK (
                            pr_url ~ '^https://github[.]com/[^/]+/[^/]+/pull/[1-9][0-9]*$'),
    pr_head_sha         text,
    ci_sha              text,
    ci_checks           jsonb,
    ci_observed_at      timestamptz,
    review_sha          text,
    review_policy       text,
    review_result_id    text REFERENCES work_result(result_id),
    review_worker_role  text,
    review_key          text,
    marker_sha          text,
    marker_reviewer     text,
    merge_sha           text,
    deployed_sha        text,
    stage               text NOT NULL DEFAULT 'PUBLISHED' CHECK (stage IN (
                            'PUBLISHED','CI_GREEN','REVIEWED','ACCEPTED',
                            'MERGED','DEPLOYED','DONE','SUPERSEDED')),
    is_current          boolean NOT NULL DEFAULT true,
    revision            bigint NOT NULL DEFAULT 1,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (pr_head_sha IS NULL OR pr_head_sha = head_sha),
    CHECK (ci_sha IS NULL OR ci_sha = head_sha),
    CHECK (review_sha IS NULL OR review_sha = head_sha),
    CHECK ((review_result_id IS NULL) = (review_sha IS NULL)),
    CHECK ((review_worker_role IS NULL) = (review_sha IS NULL)),
    CHECK ((review_key IS NULL) = (review_sha IS NULL)),
    CHECK (marker_sha IS NULL OR marker_sha = head_sha),
    CHECK (merge_sha IS NULL OR merge_sha ~ '^[0-9a-f]{40}$'),
    CHECK (deployed_sha IS NULL OR deployed_sha = merge_sha),
    CHECK (is_current OR stage = 'SUPERSEDED')
);

CREATE UNIQUE INDEX uq_control_plane_current_delivery_task
    ON control_plane_delivery_attempt(task_id) WHERE is_current;
CREATE INDEX idx_control_plane_delivery_stage
    ON control_plane_delivery_attempt(stage, task_id) WHERE is_current;

CREATE FUNCTION control_plane_bind_delivery_attempt(
    p_task_id text, p_result_id text
) RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, public AS $$
DECLARE r record; existing control_plane_delivery_attempt%ROWTYPE;
BEGIN
    SELECT wr.attempt_id, wr.work_item_id, a.claimed_by_role, wr.payload,
           i.task_id
      INTO r
      FROM work_result wr
      JOIN work_attempt a ON a.attempt_id=wr.attempt_id
      JOIN work_item i ON i.work_item_id=wr.work_item_id
     WHERE wr.result_id=p_result_id AND a.result_id=p_result_id
       AND i.result_id=p_result_id AND i.state='succeeded';
    IF r IS NULL OR r.task_id IS DISTINCT FROM p_task_id THEN
        RAISE EXCEPTION 'delivery attempt/result/task binding is not authoritative';
    END IF;
    IF r.payload->>'status' IS DISTINCT FROM 'completed'
       OR coalesce(r.payload->>'head_sha','') !~ '^[0-9a-f]{40}$'
       OR coalesce(r.payload->>'pr_url','') !~
          '^https://github[.]com/[^/]+/[^/]+/pull/[1-9][0-9]*$'
       OR coalesce(r.payload->>'worktree_path','') !~ '^/' THEN
        RAISE EXCEPTION 'delivery result lacks exact publish provenance';
    END IF;
    SELECT * INTO existing FROM control_plane_delivery_attempt
     WHERE delivery_attempt_id=r.attempt_id FOR UPDATE;
    IF FOUND THEN
        IF existing.task_id IS DISTINCT FROM p_task_id
           OR existing.head_sha IS DISTINCT FROM lower(r.payload->>'head_sha')
           OR existing.pr_url IS DISTINCT FROM r.payload->>'pr_url'
           OR existing.worktree_path IS DISTINCT FROM r.payload->>'worktree_path'
           OR existing.worker_role IS DISTINCT FROM r.claimed_by_role THEN
            RAISE EXCEPTION 'delivery attempt immutable provenance mismatch';
        END IF;
        RETURN r.attempt_id;
    END IF;
    UPDATE control_plane_delivery_attempt
       SET is_current=false,stage='SUPERSEDED',revision=revision+1,updated_at=now()
     WHERE task_id=p_task_id AND is_current;
    INSERT INTO control_plane_delivery_attempt(
        delivery_attempt_id,work_item_id,task_id,worker_role,worktree_path,
        head_sha,pr_url)
    VALUES (r.attempt_id,r.work_item_id,p_task_id,r.claimed_by_role,
            r.payload->>'worktree_path',lower(r.payload->>'head_sha'),
            r.payload->>'pr_url');
    RETURN r.attempt_id;
END
$$;

-- Upgrade the already-deployed 0011 ingest function in this new migration so
-- READY_TO_REVIEW and the canonical attempt projection commit atomically.
CREATE OR REPLACE FUNCTION backlog_ingest_results(p_planner text)
    RETURNS TABLE (task_id text, queue_state text, action text, detail jsonb)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; t backlog_task%ROWTYPE; tv backlog_verdict; rv backlog_verdict;
        lv backlog_lease_verdict; v_result jsonb; v_pr text; v_sha text;
        v_task_status text; v_delivery_attempt text;
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
            v_pr := nullif(btrim(v_result ->> 'pr_url'), '');
            v_sha := nullif(btrim(v_result ->> 'head_sha'), '');
        END IF;
        IF r.q_state = 'succeeded' AND v_task_status = 'completed'
           AND v_pr IS NOT NULL AND v_sha ~ '^[0-9a-f]{40}$' THEN
            v_delivery_attempt := control_plane_bind_delivery_attempt(
                r.t_id, r.result_id);
            PERFORM backlog_record_evidence(r.t_id, 'pr', v_pr);
            PERFORM backlog_record_evidence(r.t_id, 'sha', lower(v_sha));
            tv := backlog_transition(r.t_id, 'READY_TO_REVIEW', t.revision);
            IF NOT tv.ok THEN
                RAISE EXCEPTION 'ingest transition refused: %', tv.reason;
            END IF;
            action := 'ready_to_review';
            detail := jsonb_build_object('pr', v_pr, 'sha', lower(v_sha),
                                         'delivery_attempt', v_delivery_attempt);
        ELSE
            rv := backlog_return_to_pool(
                r.t_id,
                'cascade_exhausted: ' || CASE
                    WHEN r.q_state = 'succeeded' THEN
                        CASE WHEN v_task_status IS DISTINCT FROM 'completed'
                             THEN 'task_status_' || coalesce(v_task_status, 'missing')
                             ELSE 'publish_provenance_missing' END
                    ELSE coalesce(
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

CREATE FUNCTION control_plane_advance_delivery(
    p_task_id text, p_attempt_id text, p_revision bigint,
    p_stage text, p_head_sha text, p_detail jsonb
) RETURNS bigint
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, public AS $$
DECLARE current control_plane_delivery_attempt%ROWTYPE; next_revision bigint;
BEGIN
    SELECT * INTO current FROM control_plane_delivery_attempt
     WHERE task_id=p_task_id AND delivery_attempt_id=p_attempt_id
       AND is_current FOR UPDATE;
    IF NOT FOUND OR current.revision <> p_revision
       OR current.head_sha IS DISTINCT FROM p_head_sha THEN
        RETURN 0;
    END IF;
    IF p_stage='CI_GREEN' AND current.stage='PUBLISHED'
       AND jsonb_typeof(p_detail)='object'
       AND jsonb_typeof(p_detail->'checks')='array'
       AND jsonb_array_length(p_detail->'checks') > 0
       AND coalesce(p_detail->>'checks_sha256','') ~ '^[0-9a-f]{64}$'
       AND (SELECT bool_and(value->>'head_sha'=p_head_sha)
              FROM jsonb_array_elements(p_detail->'checks')) IS TRUE THEN
        UPDATE control_plane_delivery_attempt
           SET pr_head_sha=p_head_sha,ci_sha=p_head_sha,
               ci_checks=p_detail->'checks',ci_observed_at=now(),
               stage='CI_GREEN',revision=revision+1,updated_at=now()
         WHERE delivery_attempt_id=p_attempt_id RETURNING revision INTO next_revision;
    ELSIF p_stage='REVIEWED' AND current.stage='CI_GREEN'
       AND jsonb_typeof(p_detail)='object'
       AND (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_detail) key)
           = ARRAY['idempotency_key','policy','result_id','reviewer_role']
       AND coalesce(p_detail->>'policy','') <> ''
       AND coalesce(p_detail->>'idempotency_key','') <> ''
       AND coalesce(p_detail->>'reviewer_role','') <> ''
       AND EXISTS (
           SELECT 1 FROM work_result wr
           JOIN work_attempt a ON a.attempt_id=wr.attempt_id
           JOIN work_item i ON i.work_item_id=wr.work_item_id
          WHERE wr.result_id=p_detail->>'result_id'
            AND a.result_id=wr.result_id AND i.result_id=wr.result_id
            AND i.task_id=current.task_id
            AND i.idempotency_key=p_detail->>'idempotency_key'
            AND a.claimed_by_role=p_detail->>'reviewer_role'
            AND wr.payload->>'status'='completed') THEN
        UPDATE control_plane_delivery_attempt
           SET review_sha=p_head_sha,review_policy=p_detail->>'policy',
               review_result_id=p_detail->>'result_id',
               review_worker_role=p_detail->>'reviewer_role',
               review_key=p_detail->>'idempotency_key',
               stage='REVIEWED',revision=revision+1,updated_at=now()
         WHERE delivery_attempt_id=p_attempt_id RETURNING revision INTO next_revision;
    ELSIF p_stage='ACCEPTED' AND current.stage='REVIEWED'
       AND (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_detail) key)
           = ARRAY['policy','reviewer']
       AND p_detail->>'policy'=current.review_policy
       AND coalesce(p_detail->>'reviewer','') <> '' THEN
        UPDATE control_plane_delivery_attempt
           SET marker_sha=p_head_sha,marker_reviewer=p_detail->>'reviewer',
               stage='ACCEPTED',revision=revision+1,updated_at=now()
         WHERE delivery_attempt_id=p_attempt_id RETURNING revision INTO next_revision;
    ELSIF p_stage='MERGED' AND current.stage='ACCEPTED'
       AND (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(p_detail) key)
           = ARRAY['merge_sha']
       AND coalesce(p_detail->>'merge_sha','') ~ '^[0-9a-f]{40}$' THEN
        UPDATE control_plane_delivery_attempt
           SET merge_sha=p_detail->>'merge_sha',stage='MERGED',
               revision=revision+1,updated_at=now()
         WHERE delivery_attempt_id=p_attempt_id RETURNING revision INTO next_revision;
    ELSE
        RETURN 0;
    END IF;
    RETURN next_revision;
END
$$;

CREATE TABLE control_plane_notification (
    notification_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id          text REFERENCES backlog_task(task_id),
    component        text,
    kind             text NOT NULL,
    owner            text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    state            text NOT NULL DEFAULT 'PENDING'
                     CHECK (state IN ('PENDING', 'DELIVERING', 'SENT', 'DEAD')),
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     integer NOT NULL DEFAULT 8 CHECK (max_attempts BETWEEN 1 AND 100),
    available_at     timestamptz NOT NULL DEFAULT now(),
    claimed_by       text,
    lease_expires_at timestamptz,
    last_error       text,
    dedupe_key       text NOT NULL UNIQUE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    sent_at          timestamptz,
    CHECK ((task_id IS NOT NULL)::integer + (component IS NOT NULL)::integer = 1)
);

CREATE INDEX idx_control_plane_notification_due
    ON control_plane_notification(state, available_at, lease_expires_at);

-- Deployment is a separate authority. aicc_app may read this attestation but
-- cannot write it; the only writer is the dedicated deploy login through the
-- function below, which additionally proves the exact merge commit.
CREATE TABLE control_plane_deployment (
    task_id       text NOT NULL REFERENCES backlog_task(task_id),
    merged_sha    text NOT NULL CHECK (merged_sha ~ '^[0-9a-f]{40}$'),
    environment   text NOT NULL CHECK (length(environment) > 0),
    deployed_by   text NOT NULL,
    deployed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, merged_sha, environment)
);

CREATE FUNCTION control_plane_record_deployment(
    p_task_id text, p_merged_sha text, p_environment text
) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER
    SET search_path = pg_catalog, public AS $$
DECLARE verdict backlog_verdict;
BEGIN
    IF session_user <> 'aicc_deployer' THEN
        RAISE EXCEPTION 'deployment attestation requires aicc_deployer'
            USING ERRCODE = '42501';
    END IF;
    IF p_merged_sha !~ '^[0-9a-f]{40}$' OR coalesce(p_environment, '') = '' THEN
        RETURN false;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM control_plane_delivery_attempt
         WHERE task_id=p_task_id AND is_current AND stage='MERGED'
           AND merge_sha=p_merged_sha
    ) THEN
        RETURN false;
    END IF;
    INSERT INTO control_plane_deployment(task_id, merged_sha, environment, deployed_by)
    VALUES (p_task_id, p_merged_sha, p_environment, session_user)
    ON CONFLICT DO NOTHING;
    verdict := backlog_record_evidence(p_task_id, 'ci', 'DEPLOYED:' || p_merged_sha);
    IF NOT verdict.ok THEN
        RETURN false;
    END IF;
    UPDATE control_plane_delivery_attempt
       SET deployed_sha=p_merged_sha,stage='DEPLOYED',revision=revision+1,
           updated_at=now()
     WHERE task_id=p_task_id AND is_current AND stage='MERGED'
       AND merge_sha=p_merged_sha;
    RETURN FOUND;
END
$$;

CREATE TABLE control_plane_component (
    component         text PRIMARY KEY,
    owner             text NOT NULL,
    desired_state     text NOT NULL CHECK (desired_state IN ('ACTIVE', 'INACTIVE')),
    observed_state    text NOT NULL DEFAULT 'UNKNOWN',
    next_action       text NOT NULL DEFAULT 'PROBE',
    deadline_at       timestamptz NOT NULL,
    heartbeat_at      timestamptz,
    consecutive_failures integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    circuit_open_until timestamptz,
    last_error        text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

REVOKE ALL ON control_plane_lane, control_plane_event, control_plane_notification,
    control_plane_delivery_attempt, control_plane_deployment,
    control_plane_component FROM PUBLIC;
REVOKE ALL ON FUNCTION control_plane_bind_delivery_attempt(text, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION control_plane_advance_delivery(
    text, text, bigint, text, text, jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION control_plane_record_deployment(text, text, text) FROM PUBLIC;
