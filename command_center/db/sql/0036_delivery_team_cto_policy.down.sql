-- Restores the exact 0035 function bodies. Policy-created assignment rows are
-- kept (append-only evidence); only the 0036 columns are dropped.
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
       FROM delivery_team_corrective_assignment a WHERE a.parent_task_id=t.task_id),'[]'::jsonb) ELSE '[]'::jsonb END,
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
   AND s.input_digest=_delivery_team_digest(s.task_id,s.revision,s.ordinal)
   AND CASE
    WHEN s.ordinal<=10 THEN NOT EXISTS(SELECT 1 FROM delivery_team_stage earlier
      WHERE earlier.task_id=s.task_id AND earlier.revision=s.revision
      AND earlier.ordinal<s.ordinal AND (earlier.status<>'passed'
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
 SELECT count(*)=7 AND coalesce(bool_and(s.status='passed' AND
   delivery_team_stage_current(s.task_id,s.revision,s.stage)),false)
 FROM delivery_team_stage s WHERE s.task_id=p_task_id AND s.revision=p_revision AND s.ordinal<=7
$$;
CREATE OR REPLACE FUNCTION delivery_team_ingest(p_limit integer DEFAULT 100)
RETURNS TABLE(task_id text,stage text,action text) LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE s record; v_result jsonb; v_reason text; v_verdict text;
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
DROP FUNCTION _delivery_team_validate(text,bigint,text,text,jsonb);
ALTER FUNCTION _delivery_team_validate_v1(text,bigint,text,text,jsonb) RENAME TO _delivery_team_validate;
DROP FUNCTION delivery_team_stage_waived(text,bigint,text);
DROP FUNCTION _delivery_team_role_waived(text,bigint,text);
DROP FUNCTION _delivery_team_cto_assign(text,bigint,jsonb,text);
DROP FUNCTION _delivery_team_owner_roles();
DROP TRIGGER delivery_team_assignment_due ON delivery_team_corrective_assignment;
DROP FUNCTION _delivery_team_assignment_due();
ALTER TABLE delivery_team_corrective_assignment DROP COLUMN source_work_item_id, DROP COLUMN due_at;
DROP TABLE delivery_team_due_policy;
