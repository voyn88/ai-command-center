-- AICC PostgreSQL — a refusal may not roll back its own audit
-- (VOYN-W0-AICC-AUDIT-ROLLBACK-CLASS, raise-rolls-back-the-denial-audit).
--
-- 0002 stated the rule and measured it for the queue layer: two probes
-- differing only in HOW the same refusal is delivered leave 0 audit rows
-- after `RAISE` and 1 after `RETURN`, because the exception aborts the very
-- transaction the audit row lives in. 0003 restated it for identity and
-- built `identity_assert()` around it — a verdict returned, deliberately no
-- raising wrapper, because the refusal of a single-use ticket IS the theft
-- signal and a signal that rolls itself back is not a signal.
--
-- Both statements were prose, and the defect reappeared one layer up, where
-- it is harder to see: a function that RETURNS its refusal correctly,
-- called by one that turns that returned verdict back into an exception.
-- The audit row belongs to the callee; the exception belongs to the caller;
-- the rollback takes both. That is why this is a CLASS and not an episode —
-- every call site of an auditing function is a place it can reappear, and
-- two of them were live in this schema:
--
--   * `backlog_dispatch` (0006) — `RAISE EXCEPTION 'dispatch transition
--     refused: %'` after `backlog_transition()` had already audited the
--     refusal.
--   * `backlog_ingest_results` (0009, inheriting 0007's shape) — the same,
--     twice: once for `backlog_transition()`, once for
--     `backlog_return_to_pool()`, inside a loop over EVERY in-flight task.
--
-- The ingest one is the worse of the two and shows why "unreachable
-- defence-in-depth" is not a defence. `backlog_ingest_results` iterates
-- every IN_PROGRESS task with a terminal work item. A `kind = 'gate'`
-- record parked in IN_PROGRESS is reachable straight through the importer
-- — `backlog_upsert_task` may set any status and the ingest loop does not
-- filter on kind — and `backlog_transition` refuses it with
-- `gate_is_control_record`. Before this migration that refusal aborted the
-- WHOLE tick: every healthy task's ingest in that pass was rolled back with
-- it, the dispatch phase never ran (it runs after ingest in `plan_once`),
-- and no audit row survived to say why. Tick after tick, identically,
-- because the poisoned row is still there on the next pass.
--
-- The fix is the rule the queue and identity layers already follow: refuse
-- by returning, with the refusal recorded as data.
--
--   * `backlog_ingest_results` refuses PER ROW. A refused task yields
--     `action = 'ingest_refused'` with the callee's reason in `detail`, and
--     the loop continues to the next task. One poisoned row costs one row.
--   * `backlog_dispatch` returns `transition_refused: <reason>`.
--
-- Dispatch needs one more change to do that safely. Refusing by returning
-- means the caller's transaction COMMITS, so the steps already taken are no
-- longer undone for free — the abort is what used to guarantee "a refusal
-- leaves no lease and no work item". So the order changes: the transition
-- moves BEFORE `queue_enqueue`, which makes the enqueue unreachable on the
-- refusing path, and the lease is released by a compensating call.
--
-- That compensation is conditional, and the condition is not incidental.
-- `backlog_lease_acquire` succeeds when the lease is ALREADY ours — a
-- planner holding `repo:X` for one in-flight task gets a renewal, not a
-- fresh take. Releasing unconditionally would hand that repository to
-- another writer while the FIRST task's run is still going: the two-writer
-- outcome the lease exists to prevent, introduced by the fix for a lease
-- leak. So the release happens only when THIS call is what acquired it —
-- read BEFORE the acquire, because the acquire itself destroys the
-- evidence (a renewal and a fresh take are indistinguishable afterwards).
--
-- Same-shape ingest evidence ordering also changes: evidence is recorded
-- AFTER `backlog_transition` succeeds rather than before. It was free to
-- record it first while a refusal aborted the transaction along with it;
-- now that a refusal commits, recording pr/sha evidence ahead of a
-- transition this function just declined to make would leave evidence on a
-- task (or a `kind = 'gate'` record) that never advances.

DROP FUNCTION IF EXISTS backlog_dispatch(text, text, integer, integer, jsonb, integer);

CREATE FUNCTION backlog_dispatch(
    p_task_id text, p_planner text, p_ttl_seconds integer,
    p_wip_limit integer, p_payload jsonb, p_max_attempts integer DEFAULT 3
) RETURNS backlog_dispatch_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_dispatch_verdict;
        lv backlog_lease_verdict; tv backlog_verdict;
        v_key text; v_wip integer; v_gate record;
        v_authority text; v_lease_was_ours boolean;
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

    -- The wave gate (0006 decision 1): a later NUMERIC wave yields only
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

    v_authority := 'repo:' || t.repo;

    -- Read BEFORE the acquire, because the acquire destroys the evidence:
    -- `backlog_lease_acquire` renews a lease that is already ours and
    -- reports `ok` either way, so afterwards a renewal and a fresh take are
    -- indistinguishable. Only a fresh take may be compensated below.
    SELECT EXISTS (
        SELECT 1 FROM backlog_writer_lease w
         WHERE w.authority = v_authority AND w.owner = p_planner
           AND w.expires_at > now())
      INTO v_lease_was_ours;

    -- One writer per repository, machine-held: the lease refusal is the
    -- protocol working, not an error.
    lv := backlog_lease_acquire(v_authority, p_planner, p_ttl_seconds);
    IF NOT lv.ok THEN
        PERFORM _backlog_audit(p_task_id, 'dispatch', 'rejected', 'repo_busy',
                               jsonb_build_object('repo', t.repo, 'holder', lv.owner));
        v.reason := 'repo_busy'; RETURN v;
    END IF;

    -- The transition BEFORE the enqueue. Reachable only if a future edit
    -- breaks the OPEN re-check above under this row lock — which is
    -- precisely the case in which an operator has nothing to read but the
    -- audit, so it must be a refusal that commits and not an exception that
    -- erases the reason `backlog_transition()` just wrote down.
    tv := backlog_transition(p_task_id, 'IN_PROGRESS', t.revision);
    IF NOT tv.ok THEN
        IF NOT v_lease_was_ours THEN
            PERFORM backlog_lease_release(v_authority, p_planner);
        END IF;
        PERFORM _backlog_audit(p_task_id, 'dispatch', 'rejected', 'transition_refused',
                               jsonb_build_object('transition_reason', tv.reason,
                                                  'repo', t.repo,
                                                  'lease_released', NOT v_lease_was_ours));
        v.reason := 'transition_refused: ' || coalesce(tv.reason, 'unspecified');
        v.revision := coalesce(tv.revision, t.revision);
        RETURN v;
    END IF;

    -- Idempotency key = dispatch:<task_id>:<revision>, on the revision read
    -- under the lock, NOT the one the transition just produced: a re-tick of
    -- the same task revision must land on the same work item.
    v_key := 'dispatch:' || p_task_id || ':' || t.revision;
    v.work_item_id := queue_enqueue(
        'execution', v_key, p_payload, p_task_id, t.repo, p_max_attempts);
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
        v_task_status text; v_refused_at text; v_refused_reason text;
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

        -- Reset per iteration: a refusal on one task must not be read as a
        -- refusal on the next.
        v_refused_at := NULL; v_refused_reason := NULL;

        v_task_status := NULL; v_pr := NULL; v_sha := NULL;
        IF r.q_state = 'succeeded' AND r.result_id IS NOT NULL THEN
            SELECT wr.payload INTO v_result FROM work_result wr
             WHERE wr.result_id = r.result_id;
            v_task_status := nullif(btrim(v_result ->> 'status'), '');
            v_pr  := nullif(btrim(v_result ->> 'pr_url'), '');
            v_sha := nullif(btrim(v_result ->> 'head_sha'), '');
        END IF;

        IF r.q_state = 'succeeded' AND v_task_status = 'completed' THEN
            -- Transition FIRST, evidence second — the reverse of 0007/0009.
            -- The order was free while a refusal raised, because the abort
            -- discarded the evidence rows along with everything else. Now
            -- that the refusal commits, recording evidence first would leave
            -- `pr`/`sha` rows on a task this function just declined to
            -- advance — and on a `kind = 'gate'` record, evidence that no
            -- transition will ever consume.
            tv := backlog_transition(r.t_id, 'READY_TO_REVIEW', t.revision);
            IF tv.ok THEN
                IF v_pr IS NOT NULL THEN
                    PERFORM backlog_record_evidence(r.t_id, 'pr', v_pr);
                END IF;
                IF v_sha IS NOT NULL THEN
                    PERFORM backlog_record_evidence(r.t_id, 'sha', v_sha);
                END IF;
                action := 'ready_to_review';
                detail := jsonb_build_object('pr', v_pr, 'sha', v_sha);
            ELSE
                v_refused_at := 'transition_refused';
                v_refused_reason := tv.reason;
            END IF;
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
            IF rv.ok THEN
                action := CASE rv.reason WHEN 'DEFER_TO_USER'
                          THEN 'parked_for_owner' ELSE 'returned_to_pool' END;
                detail := jsonb_build_object('target', rv.reason,
                                             'task_status', v_task_status);
            ELSE
                v_refused_at := 'return_refused';
                v_refused_reason := rv.reason;
            END IF;
        END IF;

        -- The lane is freed on BOTH paths. The work item is terminal either
        -- way, so nothing is running in that repository; keeping the lease
        -- because a task could not be advanced would wedge the repo for
        -- every OTHER task in it, which is the tick-wide damage this
        -- migration exists to stop, merely narrowed to one lane.
        lv := backlog_lease_release('repo:' || r.repo, p_planner);

        IF v_refused_at IS NOT NULL THEN
            action := 'ingest_refused';
            detail := jsonb_build_object('refused', v_refused_reason,
                                         'at', v_refused_at,
                                         'task_status', v_task_status);
            PERFORM _backlog_audit(r.t_id, 'ingest', 'rejected', v_refused_at,
                                   detail || jsonb_build_object('lease_released', lv.ok));
        ELSE
            PERFORM _backlog_audit(r.t_id, 'ingest', 'granted', action,
                                   detail || jsonb_build_object('lease_released', lv.ok));
        END IF;
        RETURN NEXT;
    END LOOP;
END
$$;
