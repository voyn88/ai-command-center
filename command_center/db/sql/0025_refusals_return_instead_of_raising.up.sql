-- AICC PostgreSQL — a refusal is data, not an exception
-- (VOYN-W0-AICC-AUDIT-ROLLBACK-CLASS).
--
-- MEASURED DEFECT, as a class rather than an episode: audit rows after a
-- refusal that raises — 0; after a refusal that returns — 1. `RAISE` aborts
-- the transaction the audit row was written in, so the refusal erases its own
-- record. The signal that a refusal happened IS the security signal; a signal
-- that rolls itself back is not a signal.
--
-- 0003 already states the rule for the identity layer ("It RETURNS a verdict
-- rather than raising, so the caller can commit the denial audit. There is
-- deliberately NO raising wrapper"). This migration applies the SAME rule to
-- every remaining function in this schema that can write an audit row:
--
--   * `backlog_dispatch`            — transition refusal
--   * `backlog_ingest_results`      — transition and return-to-pool refusals
--   * `backlog_split_task`          — a refused child
--   * `backlog_set_task_class`      — an invalid class
--
-- After this migration no function that can reach `_backlog_audit`,
-- `_queue_audit` or `_principal_audit` contains a `RAISE` that aborts.
-- `tests/architecture/test_refusal_audit_survives_fitness.py` computes that
-- closure from the migration set itself and fails on the next one added.
--
-- ---------------------------------------------------------------------------
-- The compensation problem, and why a boolean captured before the acquire is
-- the wrong instrument
-- ---------------------------------------------------------------------------
-- Not raising means the caller's transaction commits, so anything the refused
-- act already took has to be given back deliberately. For dispatch that is the
-- repository lease. The lease is REPOSITORY-scoped and the protocol
-- deliberately lets one planner hold a single `repo:X` lease across more than
-- one dispatched task in that repo, so "release what I took" is not the same
-- question as "release the lease".
--
-- Reading "was the lease already mine?" BEFORE calling `backlog_lease_acquire`
-- answers it at a point in time that the acquire then invalidates: under READ
-- COMMITTED each statement takes a fresh snapshot, so two dispatches of two
-- tasks in the same repo by the same planner can both read "not mine", one
-- commits a fresh take, and the other — whose local flag is now stale —
-- releases the lease out from under the task that is still running. That is
-- the two-writer hazard this protocol exists to prevent, reintroduced by the
-- compensation.
--
-- `_backlog_lease_release_if_idle` answers the question that actually matters
-- — "is any other task in this repository still in flight?" — and answers it
-- UNDER the lease row's own lock, which is the point of serialisation every
-- concurrent dispatcher for that repository must pass through
-- (`backlog_lease_acquire` takes `FOR UPDATE` on the same row, and on the
-- not-yet-existing row the unique index serialises the upsert). A competing
-- dispatcher therefore either has committed before we take the lock — and its
-- task is visible as IN_PROGRESS to the statement we run after taking it — or
-- has not acquired yet, and will find the row gone and take it fresh. There is
-- no window in which the check is stale, because the check happens after the
-- lock and the lock is held to commit.
--
-- The conservative branch -- keep the lease -- cannot wedge a repository: a
-- lease is a deadline, not a flag, and `_backlog_repo_free` already treats an
-- expired one as free. A retained lease therefore costs that repository at
-- most the remainder of its TTL, while releasing one that is still protecting
-- a running task costs a second writer in the same working copy.

CREATE FUNCTION _backlog_lease_release_if_idle(
    p_repo text, p_owner text, p_except_task text
) RETURNS backlog_lease_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE l backlog_writer_lease%ROWTYPE; v backlog_lease_verdict;
        v_authority text := 'repo:' || p_repo; v_busy text;
BEGIN
    v.ok := false;
    -- The lock first, and nothing read before it: every ordering decision in
    -- this function is made while holding it.
    SELECT * INTO l FROM backlog_writer_lease w
     WHERE w.authority = v_authority FOR UPDATE;
    IF NOT FOUND OR l.owner <> p_owner THEN
        PERFORM _backlog_audit(p_except_task, 'lease_release', 'rejected', 'not_owner',
                               jsonb_build_object('authority', v_authority, 'caller', p_owner));
        v.reason := 'not_owner';
        IF FOUND THEN v.owner := l.owner; v.expires_at := l.expires_at; END IF;
        RETURN v;
    END IF;

    -- `p_except_task` is the task whose own act is being compensated: it is
    -- excluded because a refused dispatch leaves its task OPEN and a refused
    -- ingest leaves it IN_PROGRESS, and neither is a reason to keep the lease
    -- on its own behalf. Any OTHER in-flight task in this repository is.
    SELECT b.task_id INTO v_busy FROM backlog_task b
     WHERE b.repo = p_repo AND b.status = 'IN_PROGRESS'
       AND b.task_id IS DISTINCT FROM p_except_task
     LIMIT 1;
    IF FOUND THEN
        PERFORM _backlog_audit(p_except_task, 'lease_release', 'rejected',
                               'repo_has_in_flight_task',
                               jsonb_build_object('authority', v_authority, 'owner', p_owner,
                                                  'in_flight', v_busy));
        v.reason := 'repo_has_in_flight_task';
        v.owner := l.owner; v.expires_at := l.expires_at;
        RETURN v;
    END IF;

    DELETE FROM backlog_writer_lease w WHERE w.authority = v_authority;
    PERFORM _backlog_audit(p_except_task, 'lease_release', 'granted', NULL,
                           jsonb_build_object('authority', v_authority, 'owner', p_owner));
    v.ok := true; v.reason := 'released';
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- backlog_dispatch — same act, two changes.
-- ---------------------------------------------------------------------------
-- 1. The state transition now happens BEFORE the queue enqueue. The refusal it
--    can report is the one that used to raise, and taking it first means the
--    only thing left to compensate is the lease — there is no work item to
--    un-enqueue and no `queue_enqueue` audit row to erase.
-- 2. A refused transition returns `transition_refused` as a verdict, with the
--    refusal audited and the lease released only if this repository has no
--    other task in flight.
--
-- The idempotency key still uses the PRE-transition revision, so a re-tick of
-- the same revision lands on the same work item; the accepted transition bumps
-- the revision and therefore opens a new dispatch epoch.
CREATE OR REPLACE FUNCTION backlog_dispatch(
    p_task_id text, p_planner text, p_ttl_seconds integer,
    p_wip_limit integer, p_payload jsonb, p_max_attempts integer DEFAULT 3
) RETURNS backlog_dispatch_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_dispatch_verdict;
        lv backlog_lease_verdict; rv backlog_lease_verdict; tv backlog_verdict;
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

    tv := backlog_transition(p_task_id, 'IN_PROGRESS', t.revision);
    IF NOT tv.ok THEN
        -- Unreachable while this row lock is held (OPEN was re-checked above).
        -- It is nevertheless a REFUSAL and not an error: raising here would
        -- abort the caller's transaction and take every audit row this call
        -- wrote — including this one — with it. Compensate the lease, record
        -- the refusal, return it.
        rv := _backlog_lease_release_if_idle(t.repo, p_planner, p_task_id);
        PERFORM _backlog_audit(p_task_id, 'dispatch', 'rejected', 'transition_refused',
                               jsonb_build_object('transition_reason', tv.reason,
                                                  'repo', t.repo,
                                                  'planner', p_planner,
                                                  'lease_released', rv.ok,
                                                  'lease_release_reason', rv.reason));
        v.reason := 'transition_refused'; RETURN v;
    END IF;
    v.revision := tv.revision;

    v.work_item_id := queue_enqueue(
        'execution', v_key, p_payload, p_task_id, t.repo, p_max_attempts);

    PERFORM _backlog_audit(p_task_id, 'dispatch', 'granted', NULL,
                           jsonb_build_object('work_item_id', v.work_item_id,
                                              'idempotency_key', v_key,
                                              'repo', t.repo,
                                              'planner', p_planner));
    v.ok := true;
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- backlog_split_task — a refused child is a verdict.
-- ---------------------------------------------------------------------------
-- The validation pass is unchanged and still fail-closed: every malformed
-- entry is rejected before ANY child is created, which is the case the split
-- trailer actually produces. What changes is the second pass, where the child
-- upsert's own verdict used to be turned into an exception: the raise was
-- caught by `backlog_ingest_results`, and the subtransaction rollback erased
-- the upsert audit of every child created before it.
--
-- STATED PLAINLY, because it is a real narrowing and not a detail: the only
-- refusal the second pass can still see is a child id inserted CONCURRENTLY by
-- another writer between the two passes (`concurrent_insert`), and with no
-- raise those earlier children stay. They stay as valid OPEN tasks named in
-- the rejected-split audit row, the parent keeps its IN_PROGRESS status and
-- falls through to the ordinary return-to-pool path, and no parent->child
-- dependency is recorded — the dependency rows are now written once, after the
-- whole set exists, so a partial set cannot block the parent. The alternative
-- (delete what was created) is not available: those children already have
-- audit rows referencing them, and deleting them would destroy audit to undo a
-- refusal, which is the defect this migration exists to remove.
CREATE OR REPLACE FUNCTION backlog_split_task(p_parent text, p_children jsonb)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; c jsonb; v_suffix text; v_id text;
        v_title text; v_body text; v_priority text; v_ids text[] := '{}';
        v_made text[] := '{}'; uv record;
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
            PERFORM _backlog_audit(p_parent, 'split', 'rejected',
                                   'child_refused: ' || uv.reason,
                                   jsonb_build_object('child', v_id,
                                                      'created_before_refusal',
                                                      to_jsonb(v_made)));
            v.reason := 'child_refused: ' || v_id || ' (' || uv.reason || ')';
            RETURN v;
        END IF;
        UPDATE backlog_task b SET task_class = t.task_class WHERE b.task_id = v_id;
        v_made := v_made || v_id;
    END LOOP;
    INSERT INTO backlog_dependency (task_id, depends_on_task_id)
    SELECT p_parent, child FROM unnest(v_ids) AS child
    ON CONFLICT DO NOTHING;
    UPDATE backlog_task b SET status = 'SPLIT', revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_parent RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_parent, 'split', 'granted', array_to_string(v_ids, ','),
                           jsonb_build_object('children', to_jsonb(v_ids)));
    v.ok := true; v.reason := array_to_string(v_ids, ',');
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- backlog_ingest_results — one poisoned row costs one row, and leaves a record
-- ---------------------------------------------------------------------------
-- Both raises are gone. A refused transition or a refused return-to-pool is
-- now the finding of THAT task: audited, returned to the caller as an
-- `action = 'refused'` row, and the loop continues with the next task. The
-- batch keeps every audit row written for the tasks processed before it — with
-- the raise, one poisoned row erased the whole tick's audit.
--
-- The lease is NOT released on a refusal: the task is still IN_PROGRESS, so
-- the repository still has a writer. On the accepted paths the release now
-- goes through `_backlog_lease_release_if_idle`, which keeps the lease when
-- another task in the same repository is still in flight — releasing it
-- unconditionally handed that running task's repository to a second writer.
--
-- The split trailer's `::jsonb` cast is the one expression here that can still
-- raise (malformed JSON from a model). It is isolated in a block that writes
-- no audit, so its rollback undoes only the cast attempt.
CREATE OR REPLACE FUNCTION backlog_ingest_results(p_planner text)
    RETURNS TABLE (task_id text, queue_state text, action text, detail jsonb)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; t backlog_task%ROWTYPE; tv backlog_verdict; rv backlog_verdict;
        lv backlog_lease_verdict; v_result jsonb; v_pr text; v_sha text;
        v_task_status text; v_split text; v_split_json jsonb; sv backlog_verdict;
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
        sv.ok := false; sv.reason := NULL;
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
        v_split := NULL; v_split_json := NULL;
        IF r.q_state = 'succeeded' AND v_result ? 'result_text' THEN
            -- One line, bounded at end-of-line: prose after the array on a
            -- later line cannot widen the capture.
            v_split := substring(v_result ->> 'result_text'
                                 from 'SPLIT_TASKS_JSON:[[:space:]]*(\[[^\n]*\])');
        END IF;
        IF v_split IS NOT NULL THEN
            BEGIN
                v_split_json := v_split::jsonb;
            EXCEPTION
                WHEN others THEN
                    v_split_json := NULL;
                    sv.reason := 'split_trailer_not_json: ' || left(SQLERRM, 160);
            END;
            IF v_split_json IS NOT NULL THEN
                sv := backlog_split_task(r.t_id, v_split_json);
            END IF;
            IF sv.ok THEN
                action := 'split';
                detail := jsonb_build_object('children', sv.reason);
                lv := _backlog_lease_release_if_idle(r.repo, p_planner, r.t_id);
                PERFORM _backlog_audit(r.t_id, 'ingest', 'granted', action,
                                       detail || jsonb_build_object(
                                           'lease_released', lv.ok,
                                           'lease_release_reason', lv.reason));
                RETURN NEXT;
                CONTINUE;
            END IF;
            -- Any refusal of one task's split -- bad JSON, a child the upsert
            -- refused -- is that task's finding, never the batch's: it is
            -- recorded here and the task falls through to the ordinary
            -- return-to-pool path below.
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
                action := 'refused';
                detail := jsonb_build_object('refusal', 'transition_refused',
                                             'transition_reason', tv.reason,
                                             'lease_released', false);
                PERFORM _backlog_audit(r.t_id, 'ingest', 'rejected',
                                       'transition_refused', detail);
                RETURN NEXT;
                CONTINUE;
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
                action := 'refused';
                detail := jsonb_build_object('refusal', 'return_refused',
                                             'return_reason', rv.reason,
                                             'lease_released', false);
                PERFORM _backlog_audit(r.t_id, 'ingest', 'rejected',
                                       'return_refused', detail);
                RETURN NEXT;
                CONTINUE;
            END IF;
            action := CASE rv.reason WHEN 'DEFER_TO_USER'
                      THEN 'parked_for_owner' ELSE 'returned_to_pool' END;
            detail := jsonb_build_object('target', rv.reason,
                                         'task_status', v_task_status);
        END IF;

        lv := _backlog_lease_release_if_idle(r.repo, p_planner, r.t_id);
        PERFORM _backlog_audit(r.t_id, 'ingest', 'granted', action,
                               detail || jsonb_build_object(
                                   'lease_released', lv.ok,
                                   'lease_release_reason', lv.reason));
        RETURN NEXT;
    END LOOP;
END
$$;

-- An invalid class is a refusal like any other: recorded, returned false.
-- Raising here erased the audit of every act the caller had already taken in
-- the same transaction -- for the planner's tick, the whole ingest batch.
CREATE OR REPLACE FUNCTION backlog_set_task_class(p_task_id text, p_class text)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_n integer;
BEGIN
    IF p_class IS NULL OR p_class NOT IN ('functional', 'pipeline') THEN
        -- NULL task_id when the task does not exist: `backlog_event.task_id`
        -- is a foreign key, and a refusal must never fail to record because
        -- the id it refuses is unknown (the `backlog_upsert_task` idiom).
        PERFORM _backlog_audit(
            (SELECT b.task_id FROM backlog_task b WHERE b.task_id = p_task_id),
            'task_class', 'rejected', 'invalid_class',
            jsonb_build_object('requested_task_id', p_task_id,
                               'requested_class', p_class));
        RETURN false;
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
