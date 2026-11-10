-- Restore the exact 0036 state, only when nothing would be lost or widened:
--  * while enforcement is active a downgrade would either widen it back to
--    fleet-wide 0036 enforcement or (via 0036.down) remove it;
--  * once any switch or scope decision was recorded, dropping
--    cto_control_event would destroy that audit history. A reviewed forward
--    migration must retain it instead. A never-used 37 downgrades cleanly.
DO $$ BEGIN
 PERFORM pg_advisory_xact_lock(73536001);
 IF (SELECT enabled FROM cto_control WHERE singleton) THEN
  RAISE EXCEPTION 'activated CTO authorization requires a reviewed forward migration';
 END IF;
 IF EXISTS(SELECT 1 FROM cto_control_event) OR EXISTS(SELECT 1 FROM cto_scope) THEN
  RAISE EXCEPTION 'recorded CTO switch history requires a reviewed forward migration';
 END IF;
END $$;
DROP FUNCTION queue_redrive(text,integer);
ALTER FUNCTION _cto_legacy_redrive(text,integer) RENAME TO queue_redrive;
GRANT EXECUTE ON FUNCTION queue_redrive(text,integer) TO aicc_app;
DROP FUNCTION cto_scope_remove(text,text);
DROP FUNCTION cto_scope_add(text,text);
DROP FUNCTION cto_deactivate(text);
-- Verbatim 0036 definitions (CREATE OR REPLACE keeps owner and ACL).
CREATE OR REPLACE FUNCTION cto_activate(p_reference text) RETURNS boolean
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
CREATE OR REPLACE FUNCTION queue_enqueue(p_queue text,p_idempotency_key text,p_payload jsonb,
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
CREATE OR REPLACE FUNCTION backlog_dispatch(p_task_id text,p_planner text,p_ttl_seconds integer,
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
CREATE OR REPLACE FUNCTION cto_authorize_start(p_attempt text,p_token_hash text) RETURNS queue_verdict
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
CREATE OR REPLACE FUNCTION coordinator_enqueue(
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
DROP FUNCTION _cto_unbound_scoped_work(text);
DROP FUNCTION _cto_work_in_scope(text);
DROP FUNCTION _cto_in_scope(text,jsonb);
DROP FUNCTION _cto_read_only(jsonb);
DROP FUNCTION _cto_identities(text,jsonb);
DROP TABLE cto_scope;
DROP TABLE cto_control_event;
DROP FUNCTION _cto_control_event_append_only();
