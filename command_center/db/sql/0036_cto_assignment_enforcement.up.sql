-- PROVISIONAL: deliberately outside the numbered migration loader.
-- Existing queue/backlog remain authoritative. Schema-first is disabled;
-- activation requires an operator after every worker implements authorize-start.
CREATE TABLE cto_control (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
 enabled boolean NOT NULL DEFAULT false,
 activated_at timestamptz,
 activation_reference text
);
INSERT INTO cto_control(singleton) VALUES(true);
CREATE TABLE cto_assignment (
 assignment_id text PRIMARY KEY,
 task_id text NOT NULL REFERENCES backlog_task(task_id),
 revision bigint NOT NULL,
 repo text NOT NULL,
 principal_id text NOT NULL REFERENCES principal(principal_id),
 payload_digest text NOT NULL,
 reference text NOT NULL,
 issued_by text NOT NULL,
 issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 revoked_at timestamptz,
 revoke_reason text
);
CREATE UNIQUE INDEX cto_assignment_active_identity ON cto_assignment(task_id,revision,payload_digest) WHERE revoked_at IS NULL;
CREATE TABLE cto_work_binding (
 work_item_id text PRIMARY KEY REFERENCES work_item(work_item_id),
 assignment_id text NOT NULL UNIQUE REFERENCES cto_assignment(assignment_id),
 task_revision bigint NOT NULL
);
CREATE INDEX cto_assignment_active_principal ON cto_assignment(principal_id,task_id,assignment_id) WHERE revoked_at IS NULL;
CREATE INDEX cto_work_binding_assignment_revision ON cto_work_binding(assignment_id,task_revision,work_item_id);
CREATE TABLE cto_execution_start (
 attempt_id text PRIMARY KEY REFERENCES work_attempt(attempt_id),
 started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 legacy_drain boolean NOT NULL DEFAULT false
);
CREATE TABLE cto_assignment_event (
 event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 action text NOT NULL, reason text NOT NULL, actor text NOT NULL,
 assignment_id text, work_item_id text, attempt_id text,
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION _cto_audit(p_action text,p_reason text,p_assignment text DEFAULT NULL,
 p_work text DEFAULT NULL,p_attempt text DEFAULT NULL) RETURNS void
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 INSERT INTO cto_assignment_event(action,reason,actor,assignment_id,work_item_id,attempt_id)
 VALUES(p_action,p_reason,session_user,p_assignment,p_work,p_attempt);
 -- Fixed reason vocabulary and server identifiers only; never payload/tokens.
 RAISE LOG 'cto_assignment action=% reason=% actor=% assignment=% work=% attempt=%',
 p_action,p_reason,session_user,p_assignment,p_work,p_attempt;
END $$;
CREATE FUNCTION _cto_operator() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT session_user='aicc_operator' AND EXISTS(
 SELECT 1 FROM pg_roles WHERE rolname=session_user AND NOT rolsuper)
$$;
CREATE FUNCTION _cto_enabled() RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT enabled FROM cto_control WHERE singleton
$$;
CREATE FUNCTION _cto_digest(p_payload jsonb) RETURNS text
LANGUAGE sql IMMUTABLE SET search_path=pg_catalog,public AS $$
 SELECT encode(sha256(convert_to(p_payload::text,'UTF8')),'hex')
$$;
CREATE FUNCTION _cto_task(p_task text,p_payload jsonb) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog,public AS $$
DECLARE ids text[]; v text;
BEGIN
 IF jsonb_typeof(p_payload) IS DISTINCT FROM 'object'
 OR coalesce(p_payload->>'kind','') NOT IN ('agent_run','simple_task') THEN RETURN NULL; END IF;
 ids:=ARRAY[p_task,p_payload->>'backlog_task_id',p_payload#>>'{delivery_team,task_id}'];
 FOREACH v IN ARRAY ids LOOP
  IF v IS NOT NULL AND (length(btrim(v))=0 OR v<>btrim(v)) THEN RETURN NULL; END IF;
 END LOOP;
 IF (SELECT count(DISTINCT x) FROM unnest(ids) x WHERE x IS NOT NULL)<>1 THEN RETURN NULL; END IF;
 RETURN coalesce(ids[1],ids[2],ids[3]);
END $$;
CREATE FUNCTION cto_assign(p_task text,p_revision bigint,p_principal text,p_payload jsonb,p_reference text)
RETURNS TABLE(ok boolean,reason text,assignment_id text)
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE t backlog_task%ROWTYPE; a cto_assignment%ROWTYPE;
BEGIN
 PERFORM pg_advisory_xact_lock(73536001);
 IF NOT _cto_operator() THEN PERFORM _cto_audit('assign','operator_required');
 RETURN QUERY SELECT false,'operator_required',NULL::text; RETURN; END IF;
 SELECT * INTO t FROM backlog_task WHERE task_id=p_task FOR SHARE;
 IF NOT FOUND OR t.revision IS DISTINCT FROM p_revision OR t.repo IS NULL
 OR _cto_task(p_task,p_payload) IS DISTINCT FROM p_task
 OR coalesce(length(btrim(p_reference)),0)=0
 OR NOT EXISTS(SELECT 1 FROM principal WHERE principal_id=p_principal AND kind='worker_host' AND state='active')
 THEN PERFORM _cto_audit('assign','invalid_assignment');
 RETURN QUERY SELECT false,'invalid_assignment',NULL::text; RETURN; END IF;
 SELECT * INTO a FROM cto_assignment c WHERE c.task_id=p_task AND c.revision=p_revision
 AND c.payload_digest=_cto_digest(p_payload) AND c.revoked_at IS NULL;
 IF FOUND THEN
 IF a.principal_id<>p_principal THEN
  PERFORM _cto_audit('assign','assignment_conflict',a.assignment_id);
  RETURN QUERY SELECT false,'assignment_conflict',a.assignment_id; RETURN;
 END IF;
 RETURN QUERY SELECT a.revoked_at IS NULL,CASE WHEN a.revoked_at IS NULL THEN 'already_assigned' ELSE 'revoked' END,a.assignment_id;
 RETURN; END IF;
 INSERT INTO cto_assignment(assignment_id,task_id,revision,repo,principal_id,payload_digest,reference,issued_by)
 VALUES(_queue_new_id('cta'),p_task,p_revision,t.repo,p_principal,_cto_digest(p_payload),p_reference,session_user)
 RETURNING * INTO a;
 PERFORM _cto_audit('assign','granted',a.assignment_id);
 RETURN QUERY SELECT true,'granted',a.assignment_id;
END $$;
CREATE FUNCTION cto_revoke(p_assignment text,p_reason text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(73536001);
 IF NOT _cto_operator() THEN PERFORM _cto_audit('revoke','operator_required'); RETURN false; END IF;
 IF coalesce(length(btrim(p_reason)),0)=0 THEN RETURN false; END IF;
 UPDATE cto_assignment SET revoked_at=coalesce(revoked_at,clock_timestamp()),revoke_reason=p_reason
 WHERE assignment_id=p_assignment;
 IF NOT FOUND THEN RETURN false; END IF;
 PERFORM _cto_audit('revoke','revoked',p_assignment); RETURN true;
END $$;
CREATE FUNCTION cto_activate(p_reference text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(73536001);
 IF NOT _cto_operator() THEN PERFORM _cto_audit('activate','operator_required'); RETURN false; END IF;
 IF coalesce(length(btrim(p_reference)),0)=0 THEN RETURN false; END IF;
 IF _cto_enabled() THEN RETURN true; END IF;
 -- Snapshot attempt identities, not work items: later retries are not legacy.
 INSERT INTO cto_execution_start(attempt_id,legacy_drain)
 SELECT a.attempt_id,true FROM work_attempt a JOIN work_item i
 ON i.current_attempt_id=a.attempt_id AND i.state='claimed'
 WHERE a.state='active' AND a.visible_until>clock_timestamp()
 ON CONFLICT DO NOTHING;
 UPDATE cto_control SET enabled=true,activated_at=clock_timestamp(),activation_reference=p_reference WHERE singleton;
 PERFORM _cto_audit('activate','granted'); RETURN true;
END $$;
CREATE FUNCTION _cto_assignment_for(p_task text,p_repo text,p_payload jsonb) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE tid text; rev bigint; result text;
BEGIN
 tid:=_cto_task(p_task,p_payload);
 IF tid IS NULL THEN RETURN NULL; END IF;
 SELECT revision INTO rev FROM backlog_task WHERE task_id=tid AND repo=p_repo FOR SHARE;
 IF NOT FOUND THEN RETURN NULL; END IF;
 -- Ambiguous principal assignments require operator correction, never a guess.
 SELECT CASE WHEN count(*)=1 THEN min(a.assignment_id) ELSE NULL END INTO result
 FROM cto_assignment a JOIN principal p USING(principal_id)
 WHERE a.task_id=tid AND a.revision=rev AND a.repo=p_repo
 AND a.payload_digest=_cto_digest(p_payload) AND a.revoked_at IS NULL
 AND p.state='active' AND p.kind='worker_host';
 RETURN result;
END $$;
ALTER FUNCTION queue_enqueue(text,text,jsonb,text,text,integer,integer,integer,integer) RENAME TO _cto_legacy_enqueue;
CREATE FUNCTION queue_enqueue(p_queue text,p_idempotency_key text,p_payload jsonb,
 p_task_id text DEFAULT NULL,p_repository_id text DEFAULT NULL,p_max_attempts integer DEFAULT 3,
 p_priority integer DEFAULT 0,p_delay_seconds integer DEFAULT 0,p_backoff_seconds integer DEFAULT 2)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE aid text; wid text; rev bigint; old work_item%ROWTYPE; bound_work text; refusal text;
BEGIN
 PERFORM pg_advisory_xact_lock(73536001);
 IF _cto_enabled() THEN
  SELECT * INTO old FROM work_item WHERE queue=p_queue AND idempotency_key=p_idempotency_key;
  IF FOUND THEN
   IF old.task_id IS DISTINCT FROM p_task_id OR old.repository_id IS DISTINCT FROM p_repository_id
     OR old.payload IS DISTINCT FROM p_payload THEN
    PERFORM _cto_audit('enqueue','idempotency_conflict',NULL,old.work_item_id); RETURN NULL;
   END IF;
   SELECT assignment_id INTO aid FROM cto_work_binding WHERE work_item_id=old.work_item_id;
   IF FOUND THEN
    -- Replay the exact bound identity, including a legitimate dispatch R→R+1.
    -- No revoked/stale binding can be replaced merely by reusing its key.
    refusal:=_cto_work_reason(old.work_item_id,false);
    IF refusal IS NOT NULL THEN
     PERFORM _cto_audit('enqueue',refusal,aid,old.work_item_id); RETURN NULL;
    END IF;
    RETURN old.work_item_id;
   END IF;
  END IF;
  aid:=_cto_assignment_for(p_task_id,p_repository_id,p_payload);
  IF aid IS NULL THEN PERFORM _cto_audit('enqueue','assignment_required'); RETURN NULL; END IF;
  SELECT work_item_id INTO bound_work FROM cto_work_binding WHERE assignment_id=aid;
  IF FOUND AND bound_work IS DISTINCT FROM old.work_item_id THEN
   -- One operator assignment names one queue identity. Check before INSERT;
   -- the UNIQUE constraint is the race-safe backstop, not exception recovery.
   PERFORM _cto_audit('enqueue','binding_conflict',aid,bound_work); RETURN NULL;
  END IF;
 END IF;
 wid:=_cto_legacy_enqueue(p_queue,p_idempotency_key,p_payload,p_task_id,p_repository_id,
 p_max_attempts,p_priority,p_delay_seconds,p_backoff_seconds);
 IF aid IS NOT NULL THEN
  SELECT revision INTO rev FROM cto_assignment WHERE assignment_id=aid;
  INSERT INTO cto_work_binding VALUES(wid,aid,rev) ON CONFLICT DO NOTHING;
  IF NOT EXISTS(SELECT 1 FROM cto_work_binding WHERE work_item_id=wid AND assignment_id=aid) THEN
   PERFORM _cto_audit('enqueue','binding_conflict',aid,wid); RETURN NULL;
  END IF;
 END IF;
 RETURN wid;
END $$;
ALTER FUNCTION backlog_dispatch(text,text,integer,integer,jsonb,integer) RENAME TO _cto_legacy_dispatch;
CREATE FUNCTION backlog_dispatch(p_task_id text,p_planner text,p_ttl_seconds integer,
 p_wip_limit integer,p_payload jsonb,p_max_attempts integer DEFAULT 3)
RETURNS backlog_dispatch_verdict LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE v backlog_dispatch_verdict; aid text; t backlog_task%ROWTYPE;
BEGIN
 PERFORM pg_advisory_xact_lock(73536001);
 IF _cto_enabled() THEN
  SELECT * INTO t FROM backlog_task WHERE task_id=p_task_id FOR UPDATE;
  aid:=_cto_assignment_for(p_task_id,t.repo,p_payload);
  IF aid IS NULL THEN
   v.ok:=false; v.reason:='cto_assignment_required'; v.revision:=t.revision;
   PERFORM _cto_audit('dispatch','assignment_required'); RETURN v;
  END IF;
 END IF;
 BEGIN
  v:=_cto_legacy_dispatch(p_task_id,p_planner,p_ttl_seconds,p_wip_limit,p_payload,p_max_attempts);
  IF v.ok AND v.work_item_id IS NULL THEN
   RAISE SQLSTATE 'PCT01' USING MESSAGE='CTO enqueue refusal';
  END IF;
 EXCEPTION WHEN SQLSTATE 'PCT01' THEN
  -- Restore any legacy lease/status mutation before persisting the denial.
  v.ok:=false; v.reason:='cto_enqueue_refused'; v.work_item_id:=NULL;
  SELECT revision INTO v.revision FROM backlog_task WHERE task_id=p_task_id;
  PERFORM _cto_audit('dispatch','enqueue_refused'); RETURN v;
 END;
 IF v.ok AND aid IS NOT NULL THEN
  -- Only this atomic transition may carry an authorization from R to R+1.
  UPDATE cto_work_binding SET task_revision=v.revision
  WHERE work_item_id=v.work_item_id AND assignment_id=aid AND task_revision=t.revision;
  IF NOT FOUND THEN RAISE EXCEPTION 'CTO dispatch binding invariant'; END IF;
 END IF;
 RETURN v;
END $$;
CREATE FUNCTION _cto_work_reason(p_work text,p_check_principal boolean DEFAULT true) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE i work_item%ROWTYPE; a cto_assignment%ROWTYPE; b cto_work_binding%ROWTYPE; rev bigint;
BEGIN
 SELECT * INTO i FROM work_item WHERE work_item_id=p_work;
 SELECT * INTO b FROM cto_work_binding WHERE work_item_id=p_work;
 IF NOT FOUND THEN RETURN 'assignment_missing'; END IF;
 SELECT * INTO a FROM cto_assignment WHERE assignment_id=b.assignment_id;
 IF a.revoked_at IS NOT NULL THEN RETURN 'assignment_revoked'; END IF;
 IF p_check_principal AND current_principal() IS DISTINCT FROM a.principal_id THEN RETURN 'foreign_principal'; END IF;
 IF NOT EXISTS(SELECT 1 FROM principal WHERE principal_id=a.principal_id AND state='active' AND kind='worker_host')
 THEN RETURN 'principal_inactive'; END IF;
 SELECT revision INTO rev FROM backlog_task WHERE task_id=a.task_id FOR SHARE;
 IF rev IS DISTINCT FROM b.task_revision THEN RETURN 'stale_revision'; END IF;
 IF _cto_task(i.task_id,i.payload) IS DISTINCT FROM a.task_id OR i.repository_id IS DISTINCT FROM a.repo
 OR _cto_digest(i.payload) IS DISTINCT FROM a.payload_digest THEN RETURN 'binding_mismatch'; END IF;
 RETURN NULL;
END $$;
CREATE FUNCTION cto_authorize_start(p_attempt text,p_token_hash text) RETURNS queue_verdict
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE a work_attempt%ROWTYPE; i work_item%ROWTYPE; v queue_verdict; reason text;
BEGIN
 PERFORM pg_advisory_xact_lock(73536001);
 v.ok:=false;
 SELECT * INTO a FROM work_attempt WHERE attempt_id=p_attempt FOR UPDATE;
 SELECT * INTO i FROM work_item WHERE work_item_id=a.work_item_id;
 IF a.attempt_id IS NULL OR a.claimed_by_role IS DISTINCT FROM session_user
 OR a.claim_token_hash IS DISTINCT FROM p_token_hash OR a.state<>'active'
 OR a.visible_until<=clock_timestamp() OR i.state<>'claimed' OR i.current_attempt_id IS DISTINCT FROM p_attempt
 THEN v.reason:='invalid_claim'; PERFORM _cto_audit('start','invalid_claim',NULL,NULL,a.attempt_id); RETURN v; END IF;
 -- Assignment revocation permits already-started drain; principal revocation
 -- never authorizes another start call, including a legacy drain identity.
 IF _cto_enabled() AND NOT EXISTS(SELECT 1 FROM principal
   WHERE principal_id=current_principal() AND kind='worker_host' AND state='active') THEN
  v.reason:='principal_inactive';
  PERFORM _cto_audit('start','principal_inactive',NULL,i.work_item_id,p_attempt); RETURN v;
 END IF;
 IF _cto_enabled() AND NOT EXISTS(SELECT 1 FROM cto_execution_start WHERE attempt_id=p_attempt) THEN
  reason:=_cto_work_reason(i.work_item_id);
  IF reason IS NOT NULL THEN v.reason:=reason;
   PERFORM _cto_audit('start',reason,NULL,i.work_item_id,p_attempt); RETURN v; END IF;
 END IF;
 INSERT INTO cto_execution_start(attempt_id) VALUES(p_attempt) ON CONFLICT DO NOTHING;
 PERFORM _cto_audit('start','granted',NULL,i.work_item_id,p_attempt);
 v.ok:=true; v.work_item_id:=i.work_item_id; v.attempt_id:=p_attempt;
 v.visible_until:=a.visible_until; RETURN v;
END $$;

ALTER FUNCTION queue_claim(text,text,integer) RENAME TO _cto_legacy_claim;
CREATE OR REPLACE FUNCTION queue_claim(
    p_queue text, p_claim_token_hash text, p_visibility_seconds integer DEFAULT 60
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    it work_item%ROWTYPE;
    v  queue_verdict;
    v_attempt_id text;
    v_no  integer;
    v_hist integer;
    v_used integer;
    v_vis integer;
    deny text;
    first_ready work_item%ROWTYPE;
BEGIN
    PERFORM pg_advisory_xact_lock(73536001);
    IF NOT _cto_enabled() THEN RETURN _cto_legacy_claim(p_queue,p_claim_token_hash,p_visibility_seconds); END IF;
    v.ok := false;
    IF p_claim_token_hash IS NULL OR p_claim_token_hash !~ '^[a-f0-9]{64}$' THEN
        PERFORM _queue_audit(NULL, NULL, 'claim', 'rejected', 'bad_claim_token_hash');
        v.reason := 'bad_claim_token_hash';
        RETURN v;
    END IF;
    v_vis := least(greatest(coalesce(p_visibility_seconds, 60), 1), 3600);

    -- Audit at most one refused head per call, but select from ALL authorized
    -- rows: a long foreign/revoked prefix must never starve its successors.
    SELECT * INTO first_ready FROM work_item
      WHERE queue=p_queue AND state='ready' AND available_at<=now()
      ORDER BY priority DESC,available_at,created_at LIMIT 1;
    IF FOUND THEN
      deny:=_cto_work_reason(first_ready.work_item_id);
      IF deny IS NOT NULL THEN
        PERFORM _cto_audit('claim',deny,NULL,first_ready.work_item_id);
      END IF;
    END IF;
    -- Materialize only this principal's active, current-revision bindings.
    -- Unassigned/foreign/revoked queue prefixes never invoke a per-row guard.
    WITH authorized AS MATERIALIZED (
      SELECT b.work_item_id,a.task_id,a.repo,a.payload_digest
      FROM cto_assignment a
      JOIN backlog_task t ON t.task_id=a.task_id AND t.repo=a.repo
      CROSS JOIN LATERAL (
        SELECT binding.work_item_id FROM cto_work_binding binding
        WHERE binding.assignment_id=a.assignment_id AND binding.task_revision=t.revision
        ORDER BY binding.work_item_id
        OFFSET 0
      ) b
      WHERE a.principal_id=current_principal() AND a.revoked_at IS NULL
    ), selected AS MATERIALIZED (
      SELECT candidate.work_item_id FROM authorized a
      CROSS JOIN LATERAL (
        -- OFFSET 0 preserves the per-authorized-ID lookup under skewed queue
        -- statistics instead of flattening this into a scan of all work.
        SELECT wi.work_item_id,wi.priority,wi.available_at,wi.created_at FROM work_item wi
        WHERE wi.work_item_id=a.work_item_id AND wi.queue=p_queue
        AND wi.state='ready' AND wi.available_at<=now() AND wi.repository_id=a.repo
        AND _cto_task(wi.task_id,wi.payload)=a.task_id AND _cto_digest(wi.payload)=a.payload_digest
        OFFSET 0
      ) candidate
      ORDER BY candidate.priority DESC,candidate.available_at,candidate.created_at LIMIT 1
    )
    SELECT i.* INTO it FROM selected s JOIN work_item i USING(work_item_id)
    FOR UPDATE OF i SKIP LOCKED LIMIT 1;
    IF NOT FOUND THEN v.reason:='no_work'; RETURN v; END IF;
    -- Recheck the one selected identity under its canonical row lock in case
    -- a non-CTO task edit raced the relational snapshot. No attempt is spent.
    deny:=_cto_work_reason(it.work_item_id);
    IF deny IS NOT NULL THEN
      PERFORM _cto_audit('claim',deny,NULL,it.work_item_id);
      v.reason:=deny; RETURN v;
    END IF;

    -- Budget: what this item has actually consumed (refunds included).
    v_used := it.attempt_count;
    IF v_used + 1 > it.max_attempts THEN
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               dead_reason = 'attempt_budget_exhausted', dead_at = now(),
               updated_at = now()
         WHERE work_item_id = it.work_item_id;
        PERFORM _queue_audit(it.work_item_id, NULL, 'claim', 'rejected',
                             'attempt_budget_exhausted',
                             jsonb_build_object('attempt_count', v_used,
                                                'max_attempts', it.max_attempts));
        v.reason := 'attempt_budget_exhausted';
        RETURN v;
    END IF;

    -- Identity: one past whatever the history already holds (0025).
    SELECT COALESCE(MAX(attempt_no), 0) INTO v_hist
      FROM work_attempt
     WHERE work_item_id = it.work_item_id;
    v_no := GREATEST(v_used, v_hist) + 1;

    v_attempt_id := _queue_new_id('wat');
    INSERT INTO work_attempt (
        attempt_id, work_item_id, attempt_no, claimed_by_role, claim_token_hash,
        visibility_seconds, visible_until, state, created_at, updated_at)
    VALUES (v_attempt_id, it.work_item_id, v_no, session_user, p_claim_token_hash,
            v_vis, now() + make_interval(secs => v_vis), 'active', now(), now());

    UPDATE work_item
       SET state = 'claimed', attempt_count = v_used + 1,
           current_attempt_id = v_attempt_id, updated_at = now()
     WHERE work_item_id = it.work_item_id
       AND state = 'ready';
    IF NOT FOUND THEN
        PERFORM _queue_audit(it.work_item_id, v_attempt_id, 'claim', 'rejected',
                             'lost_claim_race');
        v.reason := 'lost_claim_race';
        RETURN v;
    END IF;

    PERFORM _queue_audit(it.work_item_id, v_attempt_id, 'claim', 'granted', NULL,
                         jsonb_build_object('attempt_no', v_no,
                                            'attempt_count', v_used + 1,
                                            'visibility_seconds', v_vis));
    v.ok := true;
    v.work_item_id := it.work_item_id;
    v.attempt_id := v_attempt_id;
    v.attempt_no := v_no;
    v.visible_until := now() + make_interval(secs => v_vis);
    v.payload := it.payload;
    RETURN v;
END
$$;


-- Every existing SQL producer must interpret queue_enqueue's explicit refusal.
-- Preserve originals solely for pre-activation downgrade, never for callers.
ALTER FUNCTION coordinator_enqueue(text,text,text,text,text,jsonb) RENAME TO _cto_legacy_coordinator_enqueue;
CREATE FUNCTION coordinator_enqueue(
    p_name text, p_snapshot_hash text, p_blocker_hash text, p_key text,
    p_queue text, p_payload jsonb
) RETURNS TABLE (work_item_id text, failure text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_id text;
BEGIN
    PERFORM pg_advisory_xact_lock(73536001);
    IF _cto_enabled() THEN
        -- This producer has no canonical repository/task assignment. Do not
        -- invent one or advance its CAS into a queue state without work.
        PERFORM _cto_audit('coordinator_enqueue','assignment_required');
        RETURN QUERY SELECT NULL::text,'cto_assignment_required'::text; RETURN;
    END IF;
    IF NOT coordinator_cas_begin(p_name, p_snapshot_hash, p_blocker_hash, p_key) THEN
        RETURN QUERY SELECT NULL::text, 'cas_begin_refused'::text;
        RETURN;
    END IF;
    BEGIN
        v_id := queue_enqueue(p_queue, p_key, p_payload,
            'VOYN-AICC-COORD-V2', NULL, 1, 100, 0, 2);
    EXCEPTION WHEN OTHERS THEN
        UPDATE coordinator_state SET in_flight_key = NULL, in_flight_at = NULL,
            last_status = 'enqueue_failed',
            cooldown_until = clock_timestamp() + interval '5 minutes',
            detail = detail || jsonb_build_object('enqueue_sqlstate', SQLSTATE),
            updated_at = clock_timestamp()
        WHERE name = p_name AND in_flight_key = p_key;
        RETURN QUERY SELECT NULL::text, ('enqueue_failed:' || SQLSTATE)::text;
        RETURN;
    END;
    IF v_id IS NULL THEN
        UPDATE coordinator_state SET in_flight_key=NULL,in_flight_at=NULL,
            last_status='enqueue_failed',
            cooldown_until=clock_timestamp()+interval '5 minutes',
            detail=detail || jsonb_build_object('enqueue_refusal','null_work_item'),
            updated_at=clock_timestamp()
        WHERE name=p_name AND in_flight_key=p_key;
        PERFORM _cto_audit('coordinator_enqueue','enqueue_refused');
        RETURN QUERY SELECT NULL::text,'enqueue_failed:cto_enqueue_refused'::text; RETURN;
    END IF;
    RETURN QUERY SELECT v_id, NULL::text;
END
$$;

REVOKE ALL ON FUNCTION coordinator_enqueue(text,text,text,text,text,jsonb) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION coordinator_enqueue(text,text,text,text,text,jsonb) TO aicc_app;
DO $foundation$
BEGIN
 IF to_regprocedure('public.delivery_team_dispatch(text,bigint,text,text,jsonb,integer)') IS NOT NULL THEN
  ALTER FUNCTION delivery_team_dispatch(text,bigint,text,text,jsonb,integer) RENAME TO _cto_legacy_delivery_team_dispatch;
  EXECUTE $definition$
CREATE FUNCTION delivery_team_dispatch(p_task_id text,p_revision bigint,p_stage text,
 p_input_digest text,p_payload jsonb,p_max_attempts integer DEFAULT 2)
RETURNS TABLE(ok boolean,reason text,work_item_id text) LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE s delivery_team_stage%ROWTYPE; v_id text; v_meta jsonb; v_count integer;
BEGIN
 PERFORM pg_advisory_xact_lock(73536001);
 PERFORM pg_advisory_xact_lock(73535001);
 PERFORM 1 FROM backlog_task b WHERE b.task_id=p_task_id AND b.revision=p_revision FOR UPDATE;
 IF NOT FOUND THEN RETURN QUERY SELECT false,'stale_revision',NULL::text;RETURN; END IF;
 SELECT * INTO s FROM delivery_team_stage d WHERE d.task_id=p_task_id AND d.revision=p_revision AND d.stage=p_stage FOR UPDATE;
 IF NOT FOUND THEN RETURN QUERY SELECT false,'unknown_stage',NULL::text;RETURN;END IF;
 IF p_input_digest IS DISTINCT FROM s.input_digest OR s.input_digest IS DISTINCT FROM
   _delivery_team_digest(p_task_id,p_revision,s.ordinal) THEN
   RETURN QUERY SELECT false,'stale_digest',NULL::text;RETURN;END IF;
 IF s.status='queued' THEN RETURN QUERY SELECT true,'already_queued',s.work_item_id;RETURN;END IF;
 IF NOT _delivery_team_eligible(p_task_id,p_revision,p_stage) THEN
   RETURN QUERY SELECT false,'not_eligible',NULL::text;RETURN;END IF;
 v_meta:=jsonb_build_object('task_id',p_task_id,'revision',p_revision,'stage',p_stage,'input_digest',p_input_digest);
 IF coalesce(p_payload->>'kind','')<>'agent_run' OR coalesce(p_payload->>'task_type','')<>'review'
    OR p_payload->'delivery_team' IS DISTINCT FROM v_meta OR p_payload ? 'backlog_task_id'
    OR coalesce(p_payload->>'untrusted','true')<>'false'
    OR jsonb_typeof(p_payload->'cascade') IS DISTINCT FROM 'array'
    OR jsonb_array_length(p_payload->'cascade')=0
    OR EXISTS(SELECT 1 FROM jsonb_array_elements(p_payload->'cascade') c
          WHERE c->>'task_type' IS DISTINCT FROM 'review' OR coalesce(c->>'executor','') NOT IN ('claude','codex')) THEN
    RETURN QUERY SELECT false,'unsafe_payload',NULL::text;RETURN;END IF;
 IF s.ordinal IN (8,9,10) AND (p_payload->>'delivery_team_source_sha' IS DISTINCT FROM
    (_delivery_team_context(p_task_id,p_revision,s.ordinal)->>'source_sha')
    OR coalesce(p_payload->>'delivery_team_source_sha','')!~'^[a-f0-9]{40}$') THEN
    RETURN QUERY SELECT false,'source_sha_mismatch',NULL::text;RETURN;END IF;
 -- Serialized global admission: at most two readonly team jobs occupy the
 -- existing execution queue. Terminal retry is bounded by queue max_attempts.
 SELECT count(*) INTO v_count FROM work_item w WHERE w.state IN ('ready','claimed') AND w.payload ? 'delivery_team';
 IF v_count>=2 THEN RETURN QUERY SELECT false,'capacity',NULL::text;RETURN;END IF;
 v_id:=queue_enqueue('execution','delivery-team:'||p_task_id||':'||p_revision||':'||p_stage||':'||p_input_digest||':'||(s.dispatch_count+1),
      p_payload,NULL,(SELECT b.repo FROM backlog_task b WHERE b.task_id=p_task_id),least(greatest(coalesce(p_max_attempts,2),1),2));
 IF v_id IS NULL THEN
  PERFORM _cto_audit('delivery_team_dispatch','enqueue_refused');
  RETURN QUERY SELECT false,'cto_enqueue_refused',NULL::text; RETURN;
 END IF;
 UPDATE delivery_team_stage d SET status='queued',work_item_id=v_id,dispatch_count=d.dispatch_count+1,
   reason=NULL,updated_at=now() WHERE d.task_id=p_task_id AND d.revision=p_revision AND d.stage=p_stage;
 RETURN QUERY SELECT true,'queued',v_id;
END $$;

$definition$;
  REVOKE ALL ON FUNCTION delivery_team_dispatch(text,bigint,text,text,jsonb,integer) FROM PUBLIC;
  GRANT EXECUTE ON FUNCTION delivery_team_dispatch(text,bigint,text,text,jsonb,integer) TO aicc_app;
 END IF;
END $foundation$;

-- Every new function defaults PUBLIC EXECUTE in PostgreSQL: close that default,
-- including renamed legacy entry points, before selectively granting public API.
REVOKE ALL ON TABLE cto_control,cto_assignment,cto_work_binding,cto_execution_start,cto_assignment_event
 FROM PUBLIC,aicc_app,aicc_worker,aicc_operator;
REVOKE ALL ON SEQUENCE cto_assignment_event_event_id_seq FROM PUBLIC,aicc_app,aicc_worker,aicc_operator;
DO $revoke$
DECLARE r record;
BEGIN
 FOR r IN SELECT oid::regprocedure AS name FROM pg_proc
 WHERE pronamespace='public'::regnamespace AND (proname LIKE '_cto_%' OR proname LIKE 'cto_%')
 LOOP EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC,aicc_app,aicc_worker,aicc_operator',r.name); END LOOP;
END $revoke$;
REVOKE ALL ON FUNCTION queue_enqueue(text,text,jsonb,text,text,integer,integer,integer,integer),
 backlog_dispatch(text,text,integer,integer,jsonb,integer),queue_claim(text,text,integer) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION queue_enqueue(text,text,jsonb,text,text,integer,integer,integer,integer),
 backlog_dispatch(text,text,integer,integer,jsonb,integer) TO aicc_app;
GRANT EXECUTE ON FUNCTION queue_claim(text,text,integer),cto_authorize_start(text,text) TO aicc_worker;
GRANT EXECUTE ON FUNCTION cto_assign(text,bigint,text,jsonb,text),cto_revoke(text,text),cto_activate(text) TO aicc_operator;
GRANT SELECT ON cto_control,cto_assignment,cto_work_binding,cto_execution_start,cto_assignment_event TO aicc_app,aicc_operator;
