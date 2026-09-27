-- VOYN-W0-AICC-PRIVILEGED-TASK-ROUTED-TO-UNPRIVILEGED-EXECUTOR: a task that
-- needs authority the fleet's executors do not have must be BLOCKED for the
-- owner, not re-dispatched into the loop that discovers the same wall again.
--
-- Measured 2026-08-30 on the restored queue: 32 dispatches, 13 returns to the
-- pool, of which 8 were `cascade_exhausted: task_status_failed`. One of them
-- (VOYN-W0-AICC-CONTROL-PLANE-RESILIENCE) was an agent honestly running
-- `sudo /usr/bin/true` and `sudo -u postgres psql -c 'select 1'` in its task
-- clone, being told `a password is required`, and failing -- three times,
-- once per cascade link. The worker principal is unprivileged BY DESIGN
-- (ADR-0010); no retry, no other link and no healthier host can cure that.
--
-- Three things change here, all of them machine fields, none of them inferred
-- from task prose (see `command_center/authority_preflight.py` for why three
-- successive prompt-scanning designs were rejected in review):
--
--   1. `backlog_task.required_authorities` -- the DECLARATION. The planner
--      copies it into the dispatch payload, and the worker refuses the
--      dispatch before launching a model when it cannot satisfy it.
--   2. `backlog_return_to_pool` learns a third park class. Until now a park
--      was either "technical" (re-OPEN, the substrate failed) or ordinary
--      (OPEN once, then DEFER_TO_USER). An authority block is neither: it is
--      a permanent fact about who may do this work, so it parks for the owner
--      on the FIRST return instead of spending another full cascade to
--      rediscover it. It is also explicitly NOT technical, so it can never be
--      re-opened by the technical branch.
--   3. `backlog_ingest_results` reads the `REQUIRES_AUTHORITY:` trailer a run
--      emits when it hits an authority wall, records it on the task, and
--      parks under the authority reason. That is how an UNDECLARED
--      requirement becomes a declared one after exactly one run instead of
--      after every remaining cascade link, which is what drives the
--      missing-authority share of `task_status_failed` to zero.

ALTER TABLE backlog_task
    ADD COLUMN required_authorities text[] NOT NULL DEFAULT '{}'
        CONSTRAINT backlog_task_required_authorities_vocabulary
        CHECK (required_authorities <@ ARRAY['root', 'postgres_role', 'external_credential']::text[]);

COMMENT ON COLUMN backlog_task.required_authorities IS
    'Authority this task needs beyond an ordinary unprivileged workspace. '
    'Declared (by the owner, or by a run''s REQUIRES_AUTHORITY: trailer), '
    'never inferred from the task body. Vocabulary is closed and matches '
    'command_center.authority_preflight.AUTHORITY_VOCABULARY.';

-- The view the planner dispatches from now carries the declaration, so the
-- payload can. Recreated (not REPLACEd) to add the column, with the owner
-- restore in this same migration -- 0019 recreated this view without one and
-- every planner tick died for 18 minutes with "permission denied for view
-- backlog_eligible" (0020, and the policy test that now enforces it).
DROP VIEW IF EXISTS backlog_eligible;
CREATE VIEW backlog_eligible AS
    SELECT t.task_id, t.wave, t.priority, t.status, t.title, t.body, t.repo,
           t.revision,
           (t.wave ~ '^[0-9]+(\.[0-9]+)?$') AS numeric_wave,
           (t.repo IS NOT NULL) AS dispatchable,
           t.task_class,
           t.required_authorities
      FROM backlog_task t
     WHERE t.kind = 'task'
       AND t.status = 'OPEN'
       AND NOT EXISTS (
           SELECT 1 FROM backlog_dependency d
             JOIN backlog_task dep ON dep.task_id = d.depends_on_task_id
            WHERE d.task_id = t.task_id AND dep.status <> 'DONE')
     ORDER BY (t.wave ~ '^[0-9]+(\.[0-9]+)?$') DESC,
              CASE WHEN t.wave ~ '^[0-9]+(\.[0-9]+)?$'
                   THEN t.wave::numeric ELSE NULL END ASC,
              (t.task_class = 'pipeline') DESC,
              coalesce(t.priority, 'P9') ASC,
              t.created_at ASC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aicc_migrator') THEN
        EXECUTE 'ALTER VIEW backlog_eligible OWNER TO aicc_migrator';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aicc_app') THEN
        EXECUTE 'GRANT SELECT ON backlog_eligible TO aicc_app';
    END IF;
END
$$;

-- Record an authority requirement a run discovered. Idempotent and additive:
-- a second discovery unions rather than replaces, because two runs can hit
-- two different walls. Fail-closed on an unknown name -- the vocabulary is
-- the contract, and an unrecognized authority is nothing the fleet can route.
CREATE FUNCTION backlog_declare_required_authority(p_task_id text, p_authorities text[])
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_merged text[];
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        v.reason := 'unknown_task'; RETURN v;
    END IF;
    IF p_authorities IS NULL OR cardinality(p_authorities) = 0 THEN
        v.reason := 'no_authorities'; v.revision := t.revision; RETURN v;
    END IF;
    IF NOT (p_authorities <@ ARRAY['root', 'postgres_role', 'external_credential']::text[]) THEN
        v.reason := 'unknown_authority'; v.revision := t.revision; RETURN v;
    END IF;
    SELECT array_agg(DISTINCT a ORDER BY a) INTO v_merged
      FROM unnest(t.required_authorities || p_authorities) AS a;
    UPDATE backlog_task b
       SET required_authorities = v_merged, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'declare_authority', 'granted',
                           array_to_string(v_merged, ','),
                           jsonb_build_object('added', to_jsonb(p_authorities),
                                              'merged', to_jsonb(v_merged)));
    v.ok := true; v.reason := array_to_string(v_merged, ',');
    RETURN v;
END
$$;

-- Park class three: authority. Not technical (never re-opened as a substrate
-- blip) and not "one free return" (retrying cannot grant a privilege), so it
-- goes to the owner on the first return with the missing authority named.
CREATE OR REPLACE FUNCTION backlog_return_to_pool(p_task_id text, p_reason text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_target text; v_prior integer;
        v_split_requested boolean := false;
        v_technical boolean;
        v_authority boolean;
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
    -- Evaluated BEFORE the technical test and given priority over it: the
    -- worker's authority refusal is non-retryable and reaches ingest as a
    -- dead_reason, which would otherwise have to be read as "unspecified"
    -- and re-opened forever.
    v_authority := p_reason LIKE 'cascade_exhausted: authority_unavailable%'
        OR p_reason LIKE 'cascade_exhausted: authority_required%';
    v_technical := (NOT v_authority) AND (
           p_reason LIKE 'cascade_exhausted: no_pr_published%'
        OR p_reason LIKE 'cascade_exhausted: task_status_failed%'
        OR p_reason LIKE 'cascade_exhausted: executor infrastructure failure%'
        OR p_reason LIKE 'cascade_exhausted: publish_%');
    v_target := CASE
        WHEN v_authority THEN 'DEFER_TO_USER'
        WHEN v_technical THEN 'OPEN'
        -- Pipeline class: the second non-technical return asks for a split
        -- (planner dispatches decomposition), the fourth parks. Functional
        -- tasks keep the original rule (second return parks).
        WHEN t.task_class = 'pipeline' AND v_prior >= 3 THEN 'DEFER_TO_USER'
        WHEN t.task_class = 'pipeline' AND v_prior >= 1 THEN 'OPEN'
        WHEN v_prior >= 1 THEN 'DEFER_TO_USER'
        ELSE 'OPEN'
    END;
    -- An authority block is never a size problem, so it must not be handed to
    -- the decomposer: splitting a task that needs root yields subtasks that
    -- each need root.
    v_split_requested := (NOT v_technical) AND (NOT v_authority)
                         AND t.task_class = 'pipeline'
                         AND v_prior >= 1 AND v_target = 'OPEN';

    UPDATE backlog_task b
       SET status = v_target, revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'return_to_pool', 'granted', p_reason,
                           jsonb_build_object('target', v_target,
                                              'prior_returns', v_prior,
                                              'technical', v_technical,
                                              'authority', v_authority,
                                              'split_requested', v_split_requested));
    v.ok := true; v.reason := v_target;
    RETURN v;
END
$$;

-- Ingest learns the authority trailer. Everything else is 0021's body
-- verbatim; the only additions are `v_auth`/`av` and the branch that records
-- the declaration and selects the authority park reason.
CREATE OR REPLACE FUNCTION backlog_ingest_results(p_planner text)
    RETURNS TABLE (task_id text, queue_state text, action text, detail jsonb)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; t backlog_task%ROWTYPE; tv backlog_verdict; rv backlog_verdict;
        lv backlog_lease_verdict; v_result jsonb; v_pr text; v_sha text;
        v_task_status text; v_split text; sv backlog_verdict;
        v_auth text[]; av backlog_verdict; v_reason text;
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
        v_auth := '{}';
        IF r.q_state = 'succeeded' AND r.result_id IS NOT NULL THEN
            SELECT wr.payload INTO v_result FROM work_result wr
             WHERE wr.result_id = r.result_id;
            v_task_status := nullif(btrim(v_result ->> 'status'), '');
            v_pr  := nullif(btrim(v_result ->> 'pr_url'), '');
            v_sha := nullif(btrim(v_result ->> 'head_sha'), '');
        END IF;

        -- The authority a run DISCOVERED, from the worker's
        -- `_machine_outcome` reading of the run's own REQUIRES_AUTHORITY:
        -- trailer. Only a succeeded item carries a work_result at all --
        -- `daemon._execute` persists a result solely for `ok` outcomes -- so
        -- this is deliberately not read on the dead path. A task blocked by
        -- the worker's preflight needs no recording here: it was blocked
        -- BECAUSE the declaration was already on the task, and its
        -- `authority_unavailable:` dead_reason is what routes it below.
        -- Narrowed to the closed vocabulary, so a malformed trailer can only
        -- ever reduce to nothing.
        IF v_result IS NOT NULL
           AND jsonb_typeof(v_result -> 'required_authorities') = 'array' THEN
            SELECT coalesce(array_agg(DISTINCT a ORDER BY a), '{}') INTO v_auth
              FROM jsonb_array_elements_text(v_result -> 'required_authorities') AS a
             WHERE a = ANY (ARRAY['root', 'postgres_role', 'external_credential']::text[]);
        END IF;

        -- A split run (the planner's decomposition mode for a pipeline task the
        -- fleet returned twice without a technical cause) reports no PR: its
        -- result is a SPLIT_TASKS_JSON trailer naming bounded subtasks. Ingest
        -- creates them and closes the parent as SPLIT instead of parking it
        -- for the owner (VOYN-W0-AICC-PLANNER-AUTO-SPLIT-PIPELINE-TASKS).
        -- Skipped when the run reported an authority wall: decomposing a task
        -- that needs root produces subtasks that all need root.
        v_split := NULL;
        IF r.q_state = 'succeeded' AND cardinality(v_auth) = 0
           AND v_result ? 'result_text' THEN
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

        -- Record the requirement BEFORE the park, so the park's own audit row
        -- and every later dispatch of this task see the declaration. A task
        -- that both published a PR and reported an authority wall keeps the
        -- PR path: work that produced reviewable output is not blocked, and
        -- the declaration stands for whatever comes next.
        IF cardinality(v_auth) > 0 THEN
            av := backlog_declare_required_authority(r.t_id, v_auth);
            IF NOT av.ok THEN
                PERFORM _backlog_audit(r.t_id, 'declare_authority', 'rejected',
                                       av.reason, jsonb_build_object('authorities', to_jsonb(v_auth)));
            END IF;
            SELECT * INTO t FROM backlog_task b WHERE b.task_id = r.t_id FOR UPDATE;
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
            -- An authority wall outranks whatever generic failure text the
            -- attempt also produced: it is the one cause that names WHO can
            -- do this work, and it is exactly the cause that used to hide
            -- inside the `task_status_failed` population.
            IF cardinality(v_auth) > 0 THEN
                v_reason := 'cascade_exhausted: authority_required: '
                            || array_to_string(v_auth, ',');
            ELSIF r.q_state = 'succeeded' THEN
                v_reason := 'cascade_exhausted: ' || CASE
                    WHEN v_task_status IS DISTINCT FROM 'completed'
                        THEN 'task_status_' || coalesce(v_task_status, 'missing')
                    ELSE 'no_pr_published'
                    END;
            ELSE
                v_reason := 'cascade_exhausted: ' || coalesce(
                    (SELECT i2.dead_reason FROM work_item i2
                      WHERE i2.task_id = r.t_id
                      ORDER BY i2.created_at DESC LIMIT 1), 'unspecified');
            END IF;
            rv := backlog_return_to_pool(r.t_id, v_reason);
            IF NOT rv.ok THEN
                RAISE EXCEPTION 'ingest return refused: %', rv.reason;
            END IF;
            action := CASE rv.reason WHEN 'DEFER_TO_USER'
                      THEN 'parked_for_owner' ELSE 'returned_to_pool' END;
            detail := jsonb_build_object('target', rv.reason,
                                         'task_status', v_task_status,
                                         'required_authorities', to_jsonb(v_auth));
        END IF;

        lv := backlog_lease_release('repo:' || r.repo, p_planner);
        PERFORM _backlog_audit(r.t_id, 'ingest', 'granted', action,
                               detail || jsonb_build_object('lease_released', lv.ok));
        RETURN NEXT;
    END LOOP;
END
$$;

-- Auto-resume must not reclaim an authority park.
--
-- `backlog_resume_deferred` (0014, windowed by 0017) reopens any park whose
-- reason starts with `cascade_exhausted:`, on the reasoning that such parks
-- are TRANSIENT -- a dead executor, an exhausted quota, an outage the task
-- can outlive. An authority park is the opposite: no amount of waiting
-- grants a principal a privilege it is designed never to have (ADR-0010).
-- Left unguarded, every task blocked here would be reopened within the
-- 48-hour window and walk straight back into the loop this change exists to
-- close -- the park would be a 48-hour pause, not a decision.
--
-- Only an owner act clears it: grant the authority and reopen the task, or
-- route it to an executor that holds it. Everything else about the 0017 gate
-- -- the window, superseded-evidence refusal, row lock, SECURITY DEFINER
-- posture -- is unchanged; this adds one refusal before the budget check.
CREATE OR REPLACE FUNCTION backlog_resume_deferred(p_task_id text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict;
        v_park_reason text; v_park_event_id bigint; v_resumes integer;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'resume_deferred', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF t.kind = 'gate' THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'gate_is_control_record');
        v.reason := 'gate_is_control_record'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF t.status <> 'DEFER_TO_USER' THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected', 'not_deferred',
                               jsonb_build_object('status', t.status));
        v.reason := 'not_deferred'; v.revision := t.revision;
        RETURN v;
    END IF;

    SELECT e.reason, e.event_id INTO v_park_reason, v_park_event_id
      FROM backlog_event e
     WHERE e.task_id = p_task_id
       AND e.event = 'return_to_pool'
       AND e.outcome = 'granted'
       AND e.detail->>'target' = 'DEFER_TO_USER'
     ORDER BY e.event_id DESC
     LIMIT 1;
    IF NOT FOUND THEN
        -- Parked outside the machine (imported that way, or upserted by an
        -- operator): provenance unknown, so the park is treated as an owner
        -- decision. Fail closed.
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'no_machine_park_evidence');
        v.reason := 'no_machine_park_evidence'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF v_park_reason IS NULL OR v_park_reason NOT LIKE 'cascade_exhausted:%' THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'owner_decision_park',
                               jsonb_build_object('park_reason', v_park_reason));
        v.reason := 'owner_decision_park'; v.revision := t.revision;
        RETURN v;
    END IF;
    -- The authority refusal. Checked on the PARK REASON (what the fleet
    -- decided) rather than on `t.required_authorities` (what the task
    -- declares): once the owner grants the authority the declaration
    -- legitimately stays on the task, and a still-declared task must be
    -- reopenable. It is the park itself that is permanent, not the field.
    IF v_park_reason LIKE 'cascade_exhausted: authority_unavailable%'
       OR v_park_reason LIKE 'cascade_exhausted: authority_required%' THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'authority_park_needs_owner',
                               jsonb_build_object('park_reason', v_park_reason,
                                                  'required_authorities',
                                                  to_jsonb(t.required_authorities)));
        v.reason := 'authority_park_needs_owner'; v.revision := t.revision;
        RETURN v;
    END IF;

    -- The park event must still be the mutation that PRODUCED the current
    -- DEFER_TO_USER state, not merely the newest technical park on record
    -- (independent review of PR #401 at 2bc73ac: a task technically parked,
    -- later resumed, and then hand-upserted back into DEFER_TO_USER for an
    -- owner decision still carries its old cascade_exhausted event -- which
    -- must not reopen it). Any granted mutating event after the park event
    -- means some other act may have set the current state: fail closed.
    IF EXISTS (
        SELECT 1 FROM backlog_event e
         WHERE e.task_id = p_task_id
           AND e.outcome = 'granted'
           AND e.event IN ('upsert', 'transition', 'triage',
                           'dispatch', 'return_to_pool', 'resume_deferred')
           AND e.event_id > v_park_event_id
    ) THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'superseded_park_evidence',
                               jsonb_build_object('park_event_id', v_park_event_id));
        v.reason := 'superseded_park_evidence'; v.revision := t.revision;
        RETURN v;
    END IF;

    SELECT count(*) INTO v_resumes FROM backlog_event e
     WHERE e.task_id = p_task_id
       AND e.event = 'resume_deferred'
       AND e.outcome = 'granted'
       AND e.created_at > now() - interval '48 hours';
    IF v_resumes >= 3 THEN
        PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'rejected',
                               'resume_budget_exhausted',
                               jsonb_build_object('prior_resumes', v_resumes));
        v.reason := 'resume_budget_exhausted'; v.revision := t.revision;
        RETURN v;
    END IF;

    UPDATE backlog_task b
       SET status = 'OPEN', revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'resume_deferred', 'granted', v_park_reason,
                           jsonb_build_object('park_event_id', v_park_event_id,
                                              'prior_resumes', v_resumes));
    v.ok := true; v.reason := 'OPEN';
    RETURN v;
END
$$;
