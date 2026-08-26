-- AICC PostgreSQL — the backlog planner's dispatch protocol
-- (VOYN-W0-BACKLOG-ORCHESTRATOR BO-S2, planner).
--
-- BO-S1 made the backlog a store; this migration makes dispatch an ATOMIC
-- ACT. "The task is eligible" ∧ "the repo lease is mine" ∧ "the work item is
-- enqueued" ∧ "the status is IN_PROGRESS" must become true together or not
-- at all — two planners racing, or a crash between any two of those steps,
-- must never produce two writers or a half-dispatched task. Everything
-- involved lives in this one database, so the transaction is the mechanism.
--
-- Decisions (approved plan, 2026-08-19):
--
-- * **The view is the readable definition of eligibility; the wave gate
--   lives in the dispatch function.** The earliest-wave-first protocol is
--   NOT a hard blockade: a later numeric wave is dispatchable exactly when
--   the earliest unfinished numeric wave has no dispatchable candidate LEFT
--   RIGHT NOW (each blocked by dependencies, a busy repo, or a missing
--   repo). Named lanes (COM/WOW/…) are always parallel.
-- * **Lease authority is `repo:<repo column as recorded>` — uncanonicalised
--   on purpose, and that is a named trap**: `~/x/repo`, `/home/u/x/repo`
--   and `repo ` would be three authorities and three simultaneous writers,
--   exactly what AIOS's repo_lease review demonstrated. Canonical repository
--   identity is a separately recorded task; until it lands, the importer's
--   repo values are the single spelling source.
-- * **Idempotency key = dispatch:<task_id>:<revision>.** A re-tick of the
--   planner over the same task revision lands on the SAME work item
--   (queue_enqueue's upsert); any accepted transition bumps the revision, so
--   a genuinely new dispatch epoch gets a new key.

CREATE VIEW backlog_eligible AS
    SELECT t.task_id, t.wave, t.priority, t.status, t.title, t.body, t.repo,
           t.revision,
           (t.wave ~ '^[0-9]+(\.[0-9]+)?$') AS numeric_wave,
           (t.repo IS NOT NULL) AS dispatchable
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
              coalesce(t.priority, 'P9') ASC,
              t.created_at ASC;

-- Is this repo's lease takeable by p_planner right now? (free, expired, or
-- already ours.)
CREATE FUNCTION _backlog_repo_free(p_repo text, p_planner text) RETURNS boolean
    LANGUAGE sql STABLE AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM backlog_writer_lease w
         WHERE w.authority = 'repo:' || p_repo
           AND w.owner <> p_planner
           AND w.expires_at > now())
$$;

-- A dispatchable candidate of the earliest unfinished numeric wave, if any.
-- "Dispatchable right now" = eligible ∧ has a repo ∧ that repo's lease is
-- takeable. Used by the wave gate: a later wave is refused only while this
-- returns a row.
CREATE FUNCTION _backlog_earliest_wave_candidate(p_planner text)
    RETURNS TABLE (task_id text, wave text)
    LANGUAGE sql STABLE AS $$
    WITH earliest AS (
        SELECT min(e.wave::numeric) AS w
          FROM backlog_eligible e
         WHERE e.numeric_wave
    )
    SELECT e.task_id, e.wave
      FROM backlog_eligible e, earliest
     WHERE e.numeric_wave
       AND e.wave::numeric = earliest.w
       AND e.dispatchable
       AND _backlog_repo_free(e.repo, p_planner)
     LIMIT 1
$$;

CREATE TYPE backlog_dispatch_verdict AS (
    ok            boolean,
    reason        text,
    work_item_id  text,
    revision      bigint
);

-- ---------------------------------------------------------------------------
-- backlog_dispatch — the atomic act. Refusals are data; any refusal rolls
-- back every step taken inside this call (subtransaction via EXCEPTION-free
-- sequencing: each step either returns a refusal before mutating further, or
-- the whole function's transaction commits together with the caller's).
-- ---------------------------------------------------------------------------
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

-- ---------------------------------------------------------------------------
-- backlog_release_terminal — the S2 sliver of result handling: when a
-- dispatched task's work item has reached a terminal queue state, the repo
-- lease is released so the lane frees for the next dispatch. STATUS ingest
-- (READY_TO_REVIEW / evidence) is BO-S3's act, deliberately not taken here.
-- ---------------------------------------------------------------------------
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
