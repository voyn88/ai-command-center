-- VOYN-W0-AICC-DELIVERY-TEAM: CTO corrective-assignment policy, due dates and
-- risk-based optional roles. Separate from 0035 so its reviewed bytes stay
-- unchanged.
--
-- Authority. An auditor's corrective actions are proposals. The CTO policy in
-- this migration -- not a model -- admits them as authoritative assignments,
-- and only when the whole batch is canonical: the corrective task is an
-- existing remediation child of the parent, the incident is authoritative, the
-- owner is a team role from a closed set, the parent is not P0 and no prior
-- assignment for the same (parent, incident, task) differs. Anything else
-- remains an operator decision through delivery_team_assign_correction, whose
-- rows are never overridden. Effectiveness still requires an independently
-- admitted corrective check before CTO-RCA closure.
--
-- Due dates. Every assignment, operator or policy, receives due_at from the
-- priority SLA table unless the policy row for it is missing (unknown, never
-- invented). The SLA values are a recorded team policy, changed by migration.
--
-- Optional roles. A passed architect result may declare security and/or UX
-- not applicable with a reason. The waiver is derived on every read from the
-- current architect evidence (never stored), is refused for P0/P1 tasks, and
-- never overrides a stage that already ran: only a pending stage is waived.

CREATE TABLE delivery_team_due_policy (
    priority text PRIMARY KEY CHECK (priority ~ '^P[0-9]$' OR priority = 'default'),
    sla interval NOT NULL CHECK (sla > interval '0')
);
INSERT INTO delivery_team_due_policy(priority, sla) VALUES
    ('P0', interval '1 day'), ('P1', interval '2 days'),
    ('P2', interval '5 days'), ('default', interval '10 days');
REVOKE ALL ON delivery_team_due_policy FROM PUBLIC;

-- Nullable additions; the append-only row trigger does not fire on DDL.
ALTER TABLE delivery_team_corrective_assignment
    ADD COLUMN due_at timestamptz,
    ADD COLUMN source_work_item_id text REFERENCES work_item(work_item_id);

CREATE FUNCTION _delivery_team_assignment_due() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public AS $$
DECLARE v_priority text; v_sla interval;
BEGIN
 IF NEW.due_at IS NULL THEN
  SELECT b.priority INTO v_priority FROM backlog_task b WHERE b.task_id=NEW.parent_task_id;
  SELECT coalesce((SELECT p.sla FROM delivery_team_due_policy p WHERE p.priority=v_priority),
                  (SELECT p.sla FROM delivery_team_due_policy p WHERE p.priority='default')) INTO v_sla;
  NEW.due_at := coalesce(NEW.recorded_at, clock_timestamp()) + v_sla;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER delivery_team_assignment_due BEFORE INSERT ON delivery_team_corrective_assignment
    FOR EACH ROW EXECUTE FUNCTION _delivery_team_assignment_due();

-- Closed set of accountable team roles. The auditor is excluded so the
-- process audit stays independent of the corrective work it judges.
CREATE FUNCTION _delivery_team_owner_roles() RETURNS text[] LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public AS $$
 SELECT ARRAY['ba','business_technologist','architect','qa','security','ux','developer',
              'independent_reviewer','devops','integrator','cto']::text[]
$$;

CREATE FUNCTION _delivery_team_cto_assign(p_task_id text,p_revision bigint,p_result jsonb,p_work_item_id text)
RETURNS text LANGUAGE plpgsql VOLATILE SET search_path = pg_catalog, public AS $$
DECLARE item jsonb; act jsonb; v_existing jsonb; v_priority text; v_incidents jsonb;
BEGIN
 SELECT b.priority INTO v_priority FROM backlog_task b WHERE b.task_id=p_task_id AND b.revision=p_revision;
 IF NOT FOUND THEN RETURN 'stale_revision';END IF;
 IF v_priority='P0' THEN RETURN 'operator_confirmation_required';END IF;
 IF jsonb_typeof(p_result->'rca') IS DISTINCT FROM 'array' OR jsonb_array_length(p_result->'rca')=0
   THEN RETURN 'missing_rca';END IF;
 v_incidents:=_delivery_team_incidents(p_task_id,p_revision);
 FOR item IN SELECT value FROM jsonb_array_elements(p_result->'rca') LOOP
  IF jsonb_typeof(item) IS DISTINCT FROM 'object' OR jsonb_typeof(item->'actions') IS DISTINCT FROM 'array'
    THEN RETURN 'invalid_rca';END IF;
  IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(v_incidents) i WHERE i->>'incident_key'=item->>'incident_key')
    THEN RETURN 'foreign_rca_incident';END IF;
  FOR act IN SELECT value FROM jsonb_array_elements(item->'actions') LOOP
   IF (CASE WHEN jsonb_typeof(act) IS DISTINCT FROM 'object' THEN true
      ELSE (act-'task_id'-'owner'-'action'-'effectiveness_criterion_id')<>'{}'::jsonb
        OR EXISTS(SELECT 1 FROM unnest(ARRAY['task_id','owner','action','effectiveness_criterion_id']) k
          WHERE jsonb_typeof(act->k) IS DISTINCT FROM 'string' OR length(btrim(act->>k))=0) END)
     THEN RETURN 'invalid_corrective_action';END IF;
   IF NOT (act->>'owner' = ANY(_delivery_team_owner_roles())) THEN RETURN 'owner_not_team_role';END IF;
   IF NOT EXISTS(SELECT 1 FROM backlog_task_remediation m JOIN backlog_task b ON b.task_id=m.task_id
     WHERE m.parent_task_id=p_task_id AND m.task_id=act->>'task_id' AND b.kind='task')
     THEN RETURN 'unlinked_corrective_task';END IF;
   SELECT a.assignment INTO v_existing FROM delivery_team_corrective_assignment a
     WHERE a.parent_task_id=p_task_id AND a.incident_key=item->>'incident_key' AND a.task_id=act->>'task_id';
   IF FOUND THEN
    IF v_existing IS DISTINCT FROM act THEN RETURN 'assignment_conflict';END IF;
   ELSE
    INSERT INTO delivery_team_corrective_assignment(parent_task_id,incident_key,task_id,assignment,
        reference,recorded_by,source_work_item_id)
    VALUES(p_task_id,item->>'incident_key',act->>'task_id',act,
        'journal://delivery-team/'||p_work_item_id,'policy:delivery-team-cto/v1',p_work_item_id);
   END IF;
  END LOOP;
 END LOOP;
 RETURN NULL;
END $$;

CREATE FUNCTION _delivery_team_role_waived(p_task_id text,p_revision bigint,p_stage text)
RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
 SELECT coalesce((SELECT p_stage IN ('security','ux') AND coalesce(t.priority,'P9') NOT IN ('P0','P1')
   AND s.status='pending'
   AND EXISTS(SELECT 1 FROM delivery_team_stage a JOIN delivery_team_evidence e ON e.work_item_id=a.work_item_id
     WHERE a.task_id=t.task_id AND a.revision=t.revision AND a.stage='architect'
       AND a.status='passed' AND e.verdict='pass'
       AND a.input_digest=_delivery_team_digest(a.task_id,a.revision,a.ordinal)
       AND e.payload #> ARRAY['role_applicability',p_stage,'required'] = 'false'::jsonb)
  FROM backlog_task t JOIN delivery_team_stage s
    ON s.task_id=t.task_id AND s.revision=t.revision AND s.stage=p_stage
  WHERE t.task_id=p_task_id AND t.revision=p_revision),false)
$$;
CREATE FUNCTION delivery_team_stage_waived(p_task_id text,p_revision bigint,p_stage text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
 SELECT _delivery_team_role_waived(p_task_id,p_revision,p_stage)
$$;

-- The architect may add role_applicability; the 0035 validator still checks
-- everything else exactly, on the result without that key.
ALTER FUNCTION _delivery_team_validate(text,bigint,text,text,jsonb) RENAME TO _delivery_team_validate_v1;
CREATE FUNCTION _delivery_team_validate(p_task_id text,p_revision bigint,p_stage text,p_digest text,p_result jsonb)
RETURNS text LANGUAGE plpgsql STABLE SET search_path = pg_catalog, public AS $$
DECLARE v_app jsonb;
BEGIN
 IF p_stage='architect' AND jsonb_typeof(p_result)='object' AND p_result ? 'role_applicability' THEN
  v_app:=p_result->'role_applicability';
  IF jsonb_typeof(v_app) IS DISTINCT FROM 'object' OR v_app='{}'::jsonb OR EXISTS(
     SELECT 1 FROM jsonb_each(v_app) e WHERE e.key NOT IN ('security','ux')
       OR CASE WHEN jsonb_typeof(e.value) IS DISTINCT FROM 'object' THEN true
          ELSE (e.value-'required'-'reason')<>'{}'::jsonb
            OR jsonb_typeof(e.value->'required') IS DISTINCT FROM 'boolean'
            OR jsonb_typeof(e.value->'reason') IS DISTINCT FROM 'string'
            OR length(btrim(e.value->>'reason'))=0 END)
   THEN RETURN 'invalid_role_applicability';END IF;
  RETURN _delivery_team_validate_v1(p_task_id,p_revision,p_stage,p_digest,p_result-'role_applicability');
 END IF;
 RETURN _delivery_team_validate_v1(p_task_id,p_revision,p_stage,p_digest,p_result);
END $$;

-- Policy rows are outputs of the auditor result they came from, so they are
-- excluded from the auditor's own input digest (ordinal 11); CTO-RCA sees all.
CREATE OR REPLACE FUNCTION _delivery_team_context(p_task_id text, p_revision bigint, p_ordinal integer)
RETURNS jsonb LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
 SELECT jsonb_build_object(
   'task', jsonb_build_object('task_id',t.task_id,'revision',t.revision,'title',t.title,
        'body',t.body,'repo',t.repo,'status',t.status,'kind',t.kind),
   'dependencies',coalesce((SELECT jsonb_agg(jsonb_build_object('task_id',d.depends_on_task_id,
       'revision',b.revision,'status',b.status) ORDER BY d.depends_on_task_id)
       FROM backlog_dependency d JOIN backlog_task b ON b.task_id=d.depends_on_task_id
       WHERE d.task_id=t.task_id),'[]'::jsonb),
   'prior_reviews',coalesce((SELECT jsonb_agg(e.payload - 'revision' - 'input_digest' ORDER BY s.ordinal)
       FROM delivery_team_stage s JOIN delivery_team_evidence e ON e.work_item_id=s.work_item_id
       WHERE s.task_id=t.task_id AND s.revision=t.revision AND s.status='passed'
         AND s.ordinal<p_ordinal AND s.ordinal<=CASE WHEN p_ordinal>10 THEN 0 ELSE 10 END),'[]'::jsonb),
   'source_sha',CASE WHEN p_ordinal IN (8,9,10) THEN (SELECT e.value FROM backlog_evidence e
       WHERE e.task_id=t.task_id AND e.kind='sha' ORDER BY e.evidence_id DESC LIMIT 1) ELSE NULL END,
   'code_evidence', CASE WHEN p_ordinal>=8 THEN coalesce((SELECT jsonb_agg(
       jsonb_build_object('kind',e.kind,'value',e.value) ORDER BY e.kind,e.value)
       FROM backlog_evidence e WHERE e.task_id=t.task_id AND e.kind IN ('pr','sha','ci')),'[]'::jsonb) ELSE '[]'::jsonb END,
   'release_receipt',CASE WHEN p_ordinal=10 THEN (SELECT to_jsonb(r) FROM delivery_team_release_receipt r
       WHERE r.task_id=t.task_id AND r.revision=t.revision ORDER BY r.receipt_id DESC LIMIT 1) ELSE NULL END,
   'acceptance_checks',CASE WHEN p_ordinal=10 THEN coalesce((SELECT jsonb_agg(to_jsonb(a) ORDER BY a.criterion_id)
       FROM delivery_team_acceptance_check a JOIN delivery_team_release_receipt r USING(receipt_id)
       WHERE r.task_id=t.task_id AND r.revision=t.revision
         AND r.receipt_id=(SELECT max(rr.receipt_id) FROM delivery_team_release_receipt rr
           WHERE rr.task_id=t.task_id AND rr.revision=t.revision)),'[]'::jsonb) ELSE '[]'::jsonb END,
   'incidents',CASE WHEN p_ordinal>=11 THEN _delivery_team_incidents(t.task_id,t.revision) ELSE '[]'::jsonb END,
   'corrective_tasks',CASE WHEN p_ordinal>=11 THEN coalesce((SELECT jsonb_agg(
       jsonb_build_object('task_id',m.task_id,'pr_url',m.pr_url,'rejected_head_sha',m.rejected_head_sha)
       ORDER BY m.task_id) FROM backlog_task_remediation m WHERE m.parent_task_id=t.task_id),'[]'::jsonb)
       ELSE '[]'::jsonb END,
   'corrective_assignments',CASE WHEN p_ordinal>=11 THEN coalesce((SELECT jsonb_agg(to_jsonb(a) ORDER BY a.incident_key,a.task_id)
       FROM delivery_team_corrective_assignment a WHERE a.parent_task_id=t.task_id
         AND (p_ordinal>11 OR a.source_work_item_id IS NULL)),'[]'::jsonb) ELSE '[]'::jsonb END,
   'corrective_checks',CASE WHEN p_ordinal=12 THEN coalesce((SELECT jsonb_agg(to_jsonb(c) ORDER BY c.incident_key,c.task_id,c.corrective_revision,c.sha)
       FROM delivery_team_corrective_check c WHERE c.parent_task_id=t.task_id),'[]'::jsonb) ELSE '[]'::jsonb END,
   'corrective_state',CASE WHEN p_ordinal=12 THEN coalesce((SELECT jsonb_agg(jsonb_build_object(
       'task_id',a->>'task_id','revision',b.revision,'status',b.status,
       'evidence',coalesce((SELECT jsonb_agg(jsonb_build_object('kind',be.kind,'value',be.value)
         ORDER BY be.evidence_id) FROM backlog_evidence be WHERE be.task_id=b.task_id
         AND be.kind IN ('sha','ci')),'[]'::jsonb)) ORDER BY a->>'task_id')
       FROM delivery_team_stage ds JOIN delivery_team_evidence de ON de.work_item_id=ds.work_item_id
       CROSS JOIN LATERAL jsonb_array_elements(de.payload->'rca') ri
       CROSS JOIN LATERAL jsonb_array_elements(ri->'actions') a
       LEFT JOIN backlog_task b ON b.task_id=a->>'task_id'
       WHERE ds.task_id=t.task_id AND ds.revision=t.revision AND ds.stage='auditor' AND ds.status='passed'),
       '[]'::jsonb) ELSE '[]'::jsonb END,
   'audit',CASE WHEN p_ordinal=12 THEN (SELECT e.payload FROM delivery_team_stage s
       JOIN delivery_team_evidence e ON e.work_item_id=s.work_item_id WHERE s.task_id=t.task_id
       AND s.revision=t.revision AND s.stage='auditor' AND s.status='passed') ELSE NULL END)
 FROM backlog_task t WHERE t.task_id=p_task_id AND t.revision=p_revision
$$;
CREATE OR REPLACE FUNCTION _delivery_team_eligible(p_task_id text,p_revision bigint,p_stage text)
RETURNS boolean LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
 SELECT coalesce((SELECT s.status='pending' AND s.dispatch_count<2
   AND NOT _delivery_team_role_waived(s.task_id,s.revision,s.stage)
   AND s.input_digest=_delivery_team_digest(s.task_id,s.revision,s.ordinal)
   AND CASE
    WHEN s.ordinal<=10 THEN NOT EXISTS(SELECT 1 FROM delivery_team_stage earlier
      WHERE earlier.task_id=s.task_id AND earlier.revision=s.revision
      AND earlier.ordinal<s.ordinal AND ((earlier.status<>'passed'
          AND NOT _delivery_team_role_waived(earlier.task_id,earlier.revision,earlier.stage))
        OR earlier.input_digest<>_delivery_team_digest(earlier.task_id,earlier.revision,earlier.ordinal)))
    WHEN s.ordinal=11 THEN jsonb_array_length(_delivery_team_incidents(s.task_id,s.revision))>0
    ELSE EXISTS(SELECT 1 FROM delivery_team_stage a WHERE a.task_id=s.task_id AND a.revision=s.revision
      AND a.stage='auditor' AND a.status='passed'
      AND a.input_digest=_delivery_team_digest(a.task_id,a.revision,a.ordinal)) END
   AND (s.ordinal NOT IN (8,9,10) OR
      (EXISTS(SELECT 1 FROM backlog_evidence e WHERE e.task_id=s.task_id AND e.kind='sha' AND e.value~'^[a-f0-9]{40}$')
       AND EXISTS(SELECT 1 FROM backlog_evidence e WHERE e.task_id=s.task_id AND e.kind='pr')))
   AND (s.ordinal<>10 OR EXISTS(SELECT 1 FROM delivery_team_release_receipt r
        WHERE r.task_id=s.task_id AND r.revision=s.revision AND r.environment='production'))
   FROM delivery_team_stage s JOIN backlog_task t ON t.task_id=s.task_id AND t.revision=s.revision
   WHERE s.task_id=p_task_id AND s.revision=p_revision AND s.stage=p_stage),false)
$$;
CREATE OR REPLACE FUNCTION delivery_team_ready(p_task_id text,p_revision bigint)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
 SELECT count(*)=7 AND coalesce(bool_and((s.status='passed'
   OR _delivery_team_role_waived(s.task_id,s.revision,s.stage)) AND
   delivery_team_stage_current(s.task_id,s.revision,s.stage)),false)
 FROM delivery_team_stage s WHERE s.task_id=p_task_id AND s.revision=p_revision AND s.ordinal<=7
$$;
CREATE OR REPLACE FUNCTION delivery_team_ingest(p_limit integer DEFAULT 100)
RETURNS TABLE(task_id text,stage text,action text) LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE s record; v_result jsonb; v_reason text; v_verdict text; v_policy text;
BEGIN
 PERFORM pg_advisory_xact_lock(73535001);
 FOR s IN SELECT d.*,w.state AS queue_state,w.result_id,r.payload AS result_payload,
      r.work_item_id AS result_work_item_id
   FROM delivery_team_stage d JOIN work_item w ON w.work_item_id=d.work_item_id
   LEFT JOIN work_result r ON r.result_id=w.result_id
   WHERE d.status='queued' AND w.state IN ('succeeded','dead')
   ORDER BY d.updated_at LIMIT least(greatest(coalesce(p_limit,100),1),1000) FOR UPDATE OF d
 LOOP
  v_result:=coalesce(s.result_payload->'delivery_team_result','{}'::jsonb);v_reason:=NULL;
  IF NOT EXISTS(SELECT 1 FROM backlog_task b WHERE b.task_id=s.task_id AND b.revision=s.revision)
     OR s.input_digest IS DISTINCT FROM _delivery_team_digest(s.task_id,s.revision,s.ordinal) THEN
    v_verdict:='stale';v_reason:='stale_input';
  ELSIF s.queue_state='dead' THEN v_verdict:='dead';v_reason:='queue_exhausted';
  ELSIF s.result_work_item_id IS DISTINCT FROM s.work_item_id THEN v_verdict:='invalid';v_reason:='result_provenance_mismatch';
  ELSIF s.ordinal IN (8,9,10) AND s.result_payload->>'observed_source_sha' IS DISTINCT FROM
    (_delivery_team_context(s.task_id,s.revision,s.ordinal)->>'source_sha') THEN
     v_verdict:='invalid';v_reason:='observed_source_sha_mismatch';
  ELSE
   v_reason:=_delivery_team_validate(s.task_id,s.revision,s.stage,s.input_digest,v_result);
   -- CTO policy: admit the auditor's proposals only as one canonical,
   -- all-or-nothing batch, then revalidate. Any refusal rolls the batch back.
   IF v_reason='unassigned_corrective_action' AND s.stage='auditor' AND v_result->>'verdict'='pass' THEN
    BEGIN
     v_policy:=_delivery_team_cto_assign(s.task_id,s.revision,v_result,s.work_item_id);
     IF v_policy IS NULL THEN
      v_reason:=_delivery_team_validate(s.task_id,s.revision,s.stage,s.input_digest,v_result);
     ELSE
      v_reason:='unassigned_corrective_action:'||v_policy;
     END IF;
     IF v_reason IS NOT NULL THEN RAISE EXCEPTION 'cto policy refused' USING ERRCODE='DT001';END IF;
    EXCEPTION WHEN SQLSTATE 'DT001' THEN NULL;
    END;
   END IF;
   v_verdict:=CASE WHEN v_reason IS NULL THEN v_result->>'verdict' ELSE 'invalid' END;
  END IF;
  INSERT INTO delivery_team_evidence(task_id,revision,stage,work_item_id,result_id,verdict,payload)
   VALUES(s.task_id,s.revision,s.stage,s.work_item_id,s.result_id,v_verdict,v_result)
   ON CONFLICT (work_item_id) DO NOTHING;
  UPDATE delivery_team_stage d SET status=CASE WHEN v_verdict='pass' THEN 'passed' ELSE 'blocked' END,
    reason=coalesce(v_reason,CASE WHEN v_verdict<>'pass' THEN (v_result->'blockers')::text END),updated_at=now()
    WHERE d.task_id=s.task_id AND d.revision=s.revision AND d.stage=s.stage;
  task_id:=s.task_id;stage:=s.stage;action:=coalesce(v_reason,v_verdict);RETURN NEXT;
 END LOOP;
END $$;

-- Overdue escalation, exactly once per assignment, as data: a ledger row and
-- (in the same caller transaction) an OPEN canonical backlog task addressed to
-- the CTO, the same inbox the tick-stall watchdog uses. The claim function
-- re-derives "overdue and unproven" itself; a caller cannot escalate early.
CREATE TABLE delivery_team_overdue_escalation (
    parent_task_id text NOT NULL,
    incident_key text NOT NULL,
    task_id text NOT NULL,
    due_at timestamptz NOT NULL,
    escalation_task_id text NOT NULL UNIQUE CHECK (escalation_task_id ~ '^VOYN-[A-Za-z0-9][A-Za-z0-9._-]*$'),
    recorded_by text NOT NULL,
    escalated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (parent_task_id,incident_key,task_id),
    FOREIGN KEY (parent_task_id,incident_key,task_id)
      REFERENCES delivery_team_corrective_assignment(parent_task_id,incident_key,task_id)
);
CREATE TRIGGER delivery_team_overdue_escalation_append_only BEFORE UPDATE OR DELETE ON delivery_team_overdue_escalation
    FOR EACH ROW EXECUTE FUNCTION _delivery_team_append_only();
REVOKE ALL ON delivery_team_overdue_escalation FROM PUBLIC;

CREATE FUNCTION delivery_team_overdue(p_limit integer DEFAULT 20)
RETURNS TABLE(parent_task_id text,incident_key text,task_id text,owner text,action text,due_at timestamptz,
              parent_priority text,parent_wave text,parent_repo text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
 SELECT a.parent_task_id,a.incident_key,a.task_id,a.assignment->>'owner',a.assignment->>'action',a.due_at,
        p.priority,p.wave,p.repo
 FROM delivery_team_corrective_assignment a JOIN backlog_task p ON p.task_id=a.parent_task_id
 WHERE a.due_at IS NOT NULL AND a.due_at<clock_timestamp()
   AND NOT EXISTS(SELECT 1 FROM delivery_team_corrective_check c WHERE c.parent_task_id=a.parent_task_id
     AND c.incident_key=a.incident_key AND c.task_id=a.task_id)
   AND NOT EXISTS(SELECT 1 FROM delivery_team_overdue_escalation o WHERE o.parent_task_id=a.parent_task_id
     AND o.incident_key=a.incident_key AND o.task_id=a.task_id)
 ORDER BY a.due_at,a.parent_task_id,a.task_id
 LIMIT least(greatest(coalesce(p_limit,20),1),100)
$$;

CREATE FUNCTION delivery_team_claim_overdue(p_parent text,p_incident text,p_task text,p_escalation_task_id text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_due timestamptz;
BEGIN
 PERFORM pg_advisory_xact_lock(73535001);
 SELECT a.due_at INTO v_due FROM delivery_team_corrective_assignment a
  WHERE a.parent_task_id=p_parent AND a.incident_key=p_incident AND a.task_id=p_task;
 IF NOT FOUND OR v_due IS NULL OR v_due>=clock_timestamp()
   OR coalesce(p_escalation_task_id,'')!~'^VOYN-[A-Za-z0-9][A-Za-z0-9._-]*$'
   OR EXISTS(SELECT 1 FROM delivery_team_corrective_check c WHERE c.parent_task_id=p_parent
     AND c.incident_key=p_incident AND c.task_id=p_task) THEN RETURN false;END IF;
 INSERT INTO delivery_team_overdue_escalation(parent_task_id,incident_key,task_id,due_at,escalation_task_id,recorded_by)
  VALUES(p_parent,p_incident,p_task,v_due,p_escalation_task_id,session_user)
  ON CONFLICT DO NOTHING;
 RETURN FOUND;
END $$;

REVOKE ALL ON FUNCTION _delivery_team_assignment_due() FROM PUBLIC;
REVOKE ALL ON FUNCTION _delivery_team_owner_roles() FROM PUBLIC;
REVOKE ALL ON FUNCTION _delivery_team_cto_assign(text,bigint,jsonb,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION _delivery_team_role_waived(text,bigint,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_stage_waived(text,bigint,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION _delivery_team_validate(text,bigint,text,text,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_overdue(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_claim_overdue(text,text,text,text) FROM PUBLIC;
