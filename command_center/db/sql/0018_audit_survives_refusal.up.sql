-- AICC PostgreSQL — a refusal must not roll back its own audit row
-- (VOYN-W0-AICC-AUDIT-ROLLBACK-CLASS-REM-REM).
--
-- The same defect SRV-04 found in identity_assert_strict() -- a wrapper that
-- calls a soft, self-auditing verdict function and then escalates a refusal
-- into RAISE EXCEPTION, which aborts the transaction and deletes the audit
-- row the callee just wrote about the very refusal that triggered the raise
-- -- exists here as a CLASS, not an episode: every call site in the backlog
-- machine that treats "the transition/return I just asked for was refused"
-- as a hard error has the same shape. Measured the same way SRV-04 was: a
-- refusal via RAISE leaves 0 audit rows; the identical refusal returned as
-- data leaves 1. A denial that erases itself on its way out is not a
-- signal -- the ticket is one-shot precisely so its refusal can be trusted
-- as evidence, and evidence that unmakes itself proves nothing.
--
-- Two call sites, found by grepping every `RAISE EXCEPTION` that follows a
-- `backlog_transition` / `backlog_return_to_pool` call in the currently
-- live function bodies (0006's `backlog_dispatch`, 0011's
-- `backlog_ingest_results` -- 0007/0009's older bodies of the latter are
-- dead, superseded):
--
-- * `backlog_ingest_results` — REACHABLE. The outer scan is an unlocked
--   SELECT, not filtered by lease owner; two overlapping ingest calls (two
--   planner processes, or two ticks of the same one racing a slow query)
--   can both pick up the same completed task. The loser's per-row
--   `FOR UPDATE` blocks on the winner, then unblocks onto a row the winner
--   already moved past IN_PROGRESS -- an `illegal_transition`, not a
--   `revision_conflict` (the loser re-reads the revision fresh under its
--   own lock, so it always matches). Today's RAISE aborts the ENTIRE call:
--   under `plan_once`'s autocommit connection, one lost race on one task
--   silently rolls back every OTHER task this same tick already ingested
--   -- their evidence, their transitions, their lease releases, their
--   audit rows, gone, with a bare exception the only trace. Fixed by
--   treating the loss as data: audit it at the ingest layer (on top of the
--   audit backlog_transition/backlog_return_to_pool already wrote for
--   themselves), skip to the next row, and let the rest of the tick's work
--   stand. Self-healing: the loser's task is no longer IN_PROGRESS once the
--   winner committed, so the next tick's outer scan will not pick it up
--   again.
--
-- * `backlog_dispatch` — believed unreachable: the row lock taken at entry
--   is held continuously across `backlog_transition`'s own re-read, so
--   nothing can move the row out from under this call between the two
--   checks. Kept RAISE-free anyway, on the same principle: an invariant
--   violation that fires despite that guarantee deserves to be audited as
--   data (this layer's own refusal, alongside the one backlog_transition
--   already wrote), not to erase its own evidence via an aborted
--   transaction. Deliberately does NOT attempt to release the repo lease or
--   dequeue the work item it granted moments earlier in that branch: doing
--   so safely would require telling a fresh acquire apart from a renewal
--   of a lease another in-flight task in the same repo still depends on --
--   exactly the distinction a prior remediation attempt on this same class
--   (VOYN-W0-AICC-AUDIT-ROLLBACK-CLASS-REM, PR #634) got wrong under
--   concurrency. Failing closed (lease and queue item left exactly as they
--   are) is safer than a compensating release that can itself become a
--   second-writer hazard.

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
        -- See the migration header: this should be unreachable, and RAISE
        -- would destroy the audit row backlog_transition just wrote (plus
        -- the lease grant and enqueue above) instead of merely recording a
        -- should-never-happen refusal. Audit this layer's own denial and
        -- fail closed -- no lease release, no dequeue -- so a human sees an
        -- honest anomaly instead of an automatic cleanup racing a second
        -- writer.
        PERFORM _backlog_audit(p_task_id, 'dispatch', 'rejected',
                               'transition_refused: ' || tv.reason,
                               jsonb_build_object('work_item_id', v.work_item_id,
                                                  'idempotency_key', v_key,
                                                  'repo', t.repo,
                                                  'planner', p_planner));
        v.reason := 'transition_refused: ' || tv.reason;
        RETURN v;
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

CREATE OR REPLACE FUNCTION backlog_ingest_results(p_planner text)
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
                -- See the migration header: REACHABLE under two ingest
                -- calls racing the same completed task. backlog_transition
                -- already recorded its own refusal; RAISE-ing here used to
                -- abort this whole call, taking every OTHER task this tick
                -- had already ingested down with it. Audit the loss at this
                -- layer too, leave the lease and the task's status exactly
                -- as the winner left them, and move on -- the loser's task
                -- is no longer IN_PROGRESS, so the next tick will not pick
                -- it up again.
                PERFORM _backlog_audit(r.t_id, 'ingest', 'rejected',
                                       'transition_refused: ' || tv.reason,
                                       jsonb_build_object('attempted', 'ready_to_review'));
                action := 'ingest_refused';
                detail := jsonb_build_object('reason', tv.reason);
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
                -- Same class, same fix as the branch above.
                PERFORM _backlog_audit(r.t_id, 'ingest', 'rejected',
                                       'return_refused: ' || rv.reason,
                                       jsonb_build_object('attempted', 'return_to_pool'));
                action := 'ingest_refused';
                detail := jsonb_build_object('reason', rv.reason);
                RETURN NEXT;
                CONTINUE;
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
