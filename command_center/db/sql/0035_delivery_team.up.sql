-- Native delivery-team control protocol. Business roles are stage labels, not
-- database principals. Only the app may invoke control functions; workers can
-- complete ordinary fenced work_item attempts, never edit this ledger.
CREATE TABLE delivery_team_stage (
    task_id text NOT NULL REFERENCES backlog_task(task_id),
    revision bigint NOT NULL CHECK (revision >= 1),
    stage text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal BETWEEN 1 AND 12),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','queued','passed','blocked')),
    input_digest text NOT NULL CHECK (input_digest ~ '^[a-f0-9]{64}$'),
    work_item_id text REFERENCES work_item(work_item_id),
    dispatch_count integer NOT NULL DEFAULT 0 CHECK (dispatch_count BETWEEN 0 AND 2),
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, revision, stage),
    UNIQUE (task_id, revision, ordinal),
    CHECK (stage = (ARRAY['ba','business_technologist','architect','qa_plan','security','ux','cto',
                         'independent_reviewer','devops','production_qa','auditor','cto_rca'])[ordinal]),
    CHECK (status <> 'queued' OR work_item_id IS NOT NULL)
);
CREATE TABLE delivery_team_evidence (
    evidence_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id text NOT NULL,
    revision bigint NOT NULL,
    stage text NOT NULL,
    work_item_id text NOT NULL UNIQUE REFERENCES work_item(work_item_id),
    result_id text REFERENCES work_result(result_id),
    verdict text NOT NULL CHECK (verdict IN ('pass','blocked','changes_requested','invalid','stale','dead')),
    payload jsonb NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (task_id, revision, stage) REFERENCES delivery_team_stage(task_id, revision, stage)
);
CREATE TABLE delivery_team_release_receipt (
    receipt_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id text NOT NULL REFERENCES backlog_task(task_id),
    revision bigint NOT NULL CHECK (revision >= 1),
    sha text NOT NULL CHECK (sha ~ '^[a-f0-9]{40}$'),
    environment text NOT NULL CHECK (environment = 'production'),
    criteria jsonb NOT NULL CHECK (jsonb_typeof(criteria) = 'array' AND jsonb_array_length(criteria)>0),
    reference text NOT NULL CHECK (length(btrim(reference))>0),
    verified_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (task_id, revision, sha, environment)
);
-- Separate operator evidence for an actual criterion check, never health alone.
CREATE TABLE delivery_team_acceptance_check (
    receipt_id bigint NOT NULL REFERENCES delivery_team_release_receipt(receipt_id),
    criterion_id text NOT NULL CHECK (length(btrim(criterion_id))>0),
    check_id text NOT NULL CHECK (length(btrim(check_id))>0),
    reference text NOT NULL CHECK (reference ~ '^https://[^[:space:]]+#sha256=[a-f0-9]{64}$'),
    observed_at timestamptz NOT NULL,
    verified_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (receipt_id, criterion_id)
);
CREATE TABLE delivery_team_incident (
    task_id text NOT NULL REFERENCES backlog_task(task_id),
    revision bigint NOT NULL CHECK (revision>=1),
    incident_key text NOT NULL CHECK (incident_key ~ '^[a-f0-9]{64}$'),
    repo text NOT NULL,
    run_ref text NOT NULL,
    sha text NOT NULL CHECK (sha ~ '^[a-f0-9]{40}$'),
    signature text NOT NULL,
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    recorded_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id,incident_key),
    UNIQUE (task_id,repo,run_ref,sha,signature)
);
CREATE TABLE delivery_team_corrective_assignment (
    parent_task_id text NOT NULL REFERENCES backlog_task(task_id),
    incident_key text NOT NULL CHECK (incident_key ~ '^[a-f0-9]{64}$'),
    task_id text NOT NULL REFERENCES backlog_task(task_id),
    assignment jsonb NOT NULL,
    reference text NOT NULL,
    recorded_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (parent_task_id,incident_key,task_id)
);
CREATE TABLE delivery_team_corrective_check (
    parent_task_id text NOT NULL,
    incident_key text NOT NULL,
    task_id text NOT NULL,
    corrective_revision bigint NOT NULL CHECK (corrective_revision>=1),
    sha text NOT NULL CHECK (sha ~ '^[a-f0-9]{40}$'),
    criterion_id text NOT NULL,
    check_id text NOT NULL,
    reference text NOT NULL,
    observed_at timestamptz NOT NULL,
    recorded_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (parent_task_id,incident_key,task_id,corrective_revision,sha),
    FOREIGN KEY (parent_task_id,incident_key,task_id)
      REFERENCES delivery_team_corrective_assignment(parent_task_id,incident_key,task_id)
);
CREATE INDEX idx_delivery_team_pending ON delivery_team_stage(status, task_id, revision);
CREATE FUNCTION _delivery_team_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'delivery team evidence is append-only'; END $$;
CREATE TRIGGER delivery_team_evidence_append_only BEFORE UPDATE OR DELETE ON delivery_team_evidence
    FOR EACH ROW EXECUTE FUNCTION _delivery_team_append_only();
CREATE TRIGGER delivery_team_receipt_append_only BEFORE UPDATE OR DELETE ON delivery_team_release_receipt
    FOR EACH ROW EXECUTE FUNCTION _delivery_team_append_only();

CREATE TRIGGER delivery_team_acceptance_append_only BEFORE UPDATE OR DELETE ON delivery_team_acceptance_check
    FOR EACH ROW EXECUTE FUNCTION _delivery_team_append_only();
CREATE TRIGGER delivery_team_assignment_append_only BEFORE UPDATE OR DELETE ON delivery_team_corrective_assignment
    FOR EACH ROW EXECUTE FUNCTION _delivery_team_append_only();
CREATE TRIGGER delivery_team_corrective_check_append_only BEFORE UPDATE OR DELETE ON delivery_team_corrective_check
    FOR EACH ROW EXECUTE FUNCTION _delivery_team_append_only();
CREATE TRIGGER delivery_team_incident_append_only BEFORE UPDATE OR DELETE ON delivery_team_incident
    FOR EACH ROW EXECUTE FUNCTION _delivery_team_append_only();

CREATE FUNCTION delivery_team_record_incident(p_task_id text,p_revision bigint,p_incident_key text,
 p_repo text,p_run_ref text,p_sha text,p_signature text,p_evidence jsonb)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE existing delivery_team_incident%ROWTYPE; v_key text; v_part text; v_identity text:='';
BEGIN
 PERFORM pg_advisory_xact_lock(73535001);
 PERFORM 1 FROM backlog_task b WHERE b.task_id=p_task_id AND b.revision=p_revision AND b.repo=p_repo FOR UPDATE;
 IF NOT FOUND OR p_incident_key IS NULL OR p_incident_key!~'^[a-f0-9]{64}$'
    OR coalesce(length(btrim(p_repo)),0)=0 OR coalesce(length(btrim(p_run_ref)),0)=0
    OR coalesce(length(btrim(p_signature)),0)=0 OR p_sha IS NULL OR p_sha!~'^[a-f0-9]{40}$'
    OR jsonb_typeof(p_evidence) IS DISTINCT FROM 'object' THEN RETURN false;END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(p_evidence))<>4
    OR coalesce(p_evidence->>'kind','') NOT IN ('ci_failure','review_rejection','merge_failure','deployment_failure')
    OR EXISTS(SELECT 1 FROM unnest(ARRAY['kind','reference','observed_at','detail']) k
      WHERE jsonb_typeof(p_evidence->k) IS DISTINCT FROM 'string' OR coalesce(length(btrim(p_evidence->>k)),0)=0)
    OR (p_evidence->>'observed_at')!~'(Z|[+-][0-9]{2}:[0-9]{2})$' THEN RETURN false;END IF;
 BEGIN PERFORM (p_evidence->>'observed_at')::timestamptz;
 EXCEPTION WHEN invalid_datetime_format OR datetime_field_overflow THEN RETURN false;END;
 -- UTF-8 byte-length-prefixed identity, shared with Python incident_key.
 FOREACH v_part IN ARRAY ARRAY[p_repo,p_run_ref,p_sha,p_signature] LOOP
  v_identity:=v_identity||octet_length(convert_to(v_part,'UTF8'))::text||':'||v_part;
 END LOOP;
 v_key:=encode(sha256(convert_to(v_identity,'UTF8')),'hex');
 IF p_incident_key<>v_key THEN RETURN false;END IF;
 SELECT * INTO existing FROM delivery_team_incident i WHERE i.task_id=p_task_id
   AND i.incident_key=p_incident_key;
 IF FOUND THEN RETURN existing.repo=p_repo AND existing.run_ref=p_run_ref AND existing.sha=p_sha
   AND existing.signature=p_signature AND (existing.evidence-'observed_at')=(p_evidence-'observed_at');END IF;
 INSERT INTO delivery_team_incident(task_id,revision,incident_key,repo,run_ref,sha,signature,evidence,recorded_by)
 VALUES(p_task_id,p_revision,p_incident_key,p_repo,p_run_ref,p_sha,p_signature,p_evidence,session_user);
 RETURN true;
END $$;

CREATE FUNCTION _delivery_team_incidents(p_task_id text,p_revision bigint)
RETURNS jsonb LANGUAGE sql STABLE SET search_path=pg_catalog,public AS $$
 SELECT coalesce(jsonb_agg(incident ORDER BY incident->>'incident_key'),'[]'::jsonb) FROM (
   SELECT jsonb_build_object('incident_key',encode(sha256(convert_to(jsonb_build_array(
      'delivery_team_evidence',e.task_id,e.work_item_id)::text,'UTF8')),'hex'),
      'stage',e.stage,'verdict',e.verdict,'work_item_id',e.work_item_id,
      'recorded_revision',e.revision,'evidence',e.payload) AS incident
   FROM delivery_team_evidence e WHERE e.task_id=p_task_id
      AND e.stage NOT IN ('auditor','cto_rca') AND e.verdict IN ('blocked','changes_requested','invalid','dead')
   UNION ALL
   SELECT jsonb_build_object('incident_key',i.incident_key,'repo',i.repo,'run_ref',i.run_ref,
       'sha',i.sha,'signature',i.signature,'evidence',i.evidence,'recorded_revision',i.revision)
   FROM delivery_team_incident i WHERE i.task_id=p_task_id
 ) incidents
$$;

-- The digest is computed inside PostgreSQL from the current task and all
-- inputs affecting THIS stage. A caller cannot supply a replacement digest.
CREATE FUNCTION _delivery_team_context(p_task_id text, p_revision bigint, p_ordinal integer)
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
CREATE FUNCTION _delivery_team_digest(p_task_id text,p_revision bigint,p_ordinal integer)
RETURNS text LANGUAGE sql STABLE SET search_path = pg_catalog, public AS $$
 SELECT encode(sha256(convert_to(jsonb_build_object('ordinal',p_ordinal,'context',
   _delivery_team_context(p_task_id,p_revision,p_ordinal) #- '{task,revision}' #- '{task,status}')::text,'UTF8')),'hex')
$$;

CREATE FUNCTION delivery_team_sync(p_limit integer DEFAULT 100,p_task_ids jsonb DEFAULT NULL)
RETURNS TABLE(task_id text,action text) LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE t record; n integer; v_stage text; v_digest text; v_changed integer; prior delivery_team_stage%ROWTYPE;
BEGIN
 PERFORM pg_advisory_xact_lock(73535001);
 IF p_task_ids IS NOT NULL AND (jsonb_typeof(p_task_ids) IS DISTINCT FROM 'array'
   OR EXISTS(SELECT 1 FROM jsonb_array_elements(p_task_ids) x WHERE jsonb_typeof(x)<>'string')) THEN RETURN;END IF;
 FOR t IN SELECT b.task_id,b.revision FROM backlog_task b WHERE b.kind='task' AND (p_task_ids IS NULL OR p_task_ids ? b.task_id)
    ORDER BY NOT EXISTS(SELECT 1 FROM delivery_team_stage s WHERE s.task_id=b.task_id AND s.revision=b.revision) DESC,
      EXISTS(SELECT 1 FROM delivery_team_stage dirty WHERE dirty.task_id=b.task_id AND dirty.revision=b.revision
        AND dirty.input_digest<>_delivery_team_digest(dirty.task_id,dirty.revision,dirty.ordinal)) DESC,
      b.updated_at DESC,b.task_id LIMIT least(greatest(coalesce(p_limit,100),1),1000)
    FOR UPDATE OF b
 LOOP
  FOR n IN 1..12 LOOP
   v_stage := (ARRAY['ba','business_technologist','architect','qa_plan','security','ux','cto',
                'independent_reviewer','devops','production_qa','auditor','cto_rca'])[n];
   v_digest := _delivery_team_digest(t.task_id,t.revision,n);
   INSERT INTO delivery_team_stage(task_id,revision,stage,ordinal,input_digest)
    VALUES(t.task_id,t.revision,v_stage,n,v_digest)
    ON CONFLICT ON CONSTRAINT delivery_team_stage_pkey DO UPDATE SET
      input_digest=EXCLUDED.input_digest,status='pending',work_item_id=NULL,
      dispatch_count=0,reason='input_changed',updated_at=now()
    WHERE delivery_team_stage.input_digest<>EXCLUDED.input_digest;
   GET DIAGNOSTICS v_changed = ROW_COUNT;
   IF v_changed>0 THEN
    -- Backlog revisions also change on dispatch/status updates. Preserve an
    -- unchanged requirement approval (or refusal), never infer a new one.
    SELECT * INTO prior FROM delivery_team_stage old WHERE old.task_id=t.task_id
      AND old.revision<t.revision AND old.stage=v_stage AND old.input_digest=v_digest
      AND old.status IN ('passed','blocked') ORDER BY old.revision DESC LIMIT 1;
    IF FOUND THEN
     UPDATE delivery_team_stage d SET status=prior.status,work_item_id=prior.work_item_id,
       dispatch_count=prior.dispatch_count,reason=prior.reason
       WHERE d.task_id=t.task_id AND d.revision=t.revision AND d.stage=v_stage;
    END IF;
    task_id:=t.task_id;action:='synced:'||v_stage;RETURN NEXT;
   END IF;
  END LOOP;
 END LOOP;
END $$;

CREATE FUNCTION _delivery_team_eligible(p_task_id text,p_revision bigint,p_stage text)
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
      ((SELECT e.value~'^[a-f0-9]{40}$' FROM backlog_evidence e WHERE e.task_id=s.task_id AND e.kind='sha' ORDER BY e.evidence_id DESC LIMIT 1)
       AND EXISTS(SELECT 1 FROM backlog_evidence e WHERE e.task_id=s.task_id AND e.kind='pr')))
   AND (s.ordinal<>10 OR EXISTS(SELECT 1 FROM delivery_team_release_receipt r
        WHERE r.task_id=s.task_id AND r.revision=s.revision AND r.environment='production'
          AND r.sha=(SELECT e.value FROM backlog_evidence e WHERE e.task_id=s.task_id
                     AND e.kind='sha' ORDER BY e.evidence_id DESC LIMIT 1)))
   FROM delivery_team_stage s JOIN backlog_task t ON t.task_id=s.task_id AND t.revision=s.revision
   WHERE s.task_id=p_task_id AND s.revision=p_revision AND s.stage=p_stage),false)
$$;
CREATE FUNCTION delivery_team_candidates(p_limit integer DEFAULT 10,p_task_ids jsonb DEFAULT NULL)
RETURNS SETOF jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
 SELECT jsonb_build_object('task_id',s.task_id,'revision',s.revision,'stage',s.stage,
   'input_digest',s.input_digest,'repo',t.repo,'title',t.title,'body',t.body,
   'context',_delivery_team_context(s.task_id,s.revision,s.ordinal))
 FROM delivery_team_stage s JOIN backlog_task t ON t.task_id=s.task_id AND t.revision=s.revision
 WHERE _delivery_team_eligible(s.task_id,s.revision,s.stage)
   AND (p_task_ids IS NULL OR (jsonb_typeof(p_task_ids)='array' AND p_task_ids ? s.task_id))
 ORDER BY coalesce(t.priority,'P9'),s.created_at,s.task_id,s.ordinal
 LIMIT least(greatest(coalesce(p_limit,10),1),100)
$$;
CREATE FUNCTION delivery_team_dispatch(p_task_id text,p_revision bigint,p_stage text,
 p_input_digest text,p_payload jsonb,p_max_attempts integer DEFAULT 2)
RETURNS TABLE(ok boolean,reason text,work_item_id text) LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE s delivery_team_stage%ROWTYPE; v_id text; v_meta jsonb; v_count integer;
BEGIN
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
 UPDATE delivery_team_stage d SET status='queued',work_item_id=v_id,dispatch_count=d.dispatch_count+1,
   reason=NULL,updated_at=now() WHERE d.task_id=p_task_id AND d.revision=p_revision AND d.stage=p_stage;
 RETURN QUERY SELECT true,'queued',v_id;
END $$;

CREATE FUNCTION delivery_team_stage_current(p_task_id text,p_revision bigint,p_stage text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
 SELECT EXISTS(SELECT 1 FROM delivery_team_stage s JOIN backlog_task t
   ON t.task_id=s.task_id AND t.revision=s.revision WHERE s.task_id=p_task_id
   AND s.revision=p_revision AND s.stage=p_stage
   AND s.input_digest=_delivery_team_digest(s.task_id,s.revision,s.ordinal))
$$;
CREATE FUNCTION delivery_team_ready(p_task_id text,p_revision bigint)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
 SELECT count(*)=7 AND coalesce(bool_and(s.status='passed' AND
   delivery_team_stage_current(s.task_id,s.revision,s.stage)),false)
 FROM delivery_team_stage s WHERE s.task_id=p_task_id AND s.revision=p_revision AND s.ordinal<=7
$$;

-- Release measurement is a separate control-plane act. A model saying
-- verified=true cannot mint this receipt. The caller must supply a measured
-- production reference, exact latest code SHA and the complete BA criteria.
CREATE FUNCTION delivery_team_record_release(p_task_id text,p_revision bigint,p_sha text,
 p_environment text,p_criteria jsonb,p_reference text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_criteria jsonb; v_sha text;
BEGIN
 PERFORM pg_advisory_xact_lock(73535001);
 PERFORM 1 FROM backlog_task t WHERE t.task_id=p_task_id AND t.revision=p_revision FOR UPDATE;
 IF NOT FOUND OR p_environment IS DISTINCT FROM 'production' OR p_sha IS NULL
    OR p_sha!~'^[a-f0-9]{40}$' OR coalesce(length(btrim(p_reference)),0)=0
    OR jsonb_typeof(p_criteria) IS DISTINCT FROM 'array' THEN RETURN false;END IF;
 SELECT e.value INTO v_sha FROM backlog_evidence e WHERE e.task_id=p_task_id AND e.kind='sha'
   ORDER BY e.evidence_id DESC LIMIT 1;
 IF p_sha IS DISTINCT FROM v_sha THEN RETURN false;END IF;
 SELECT jsonb_agg(c->'id' ORDER BY c->>'id') INTO v_criteria FROM delivery_team_stage s
  JOIN delivery_team_evidence e ON e.work_item_id=s.work_item_id,
  LATERAL jsonb_array_elements(e.payload->'criteria') c
  WHERE s.task_id=p_task_id AND s.revision=p_revision AND s.stage='ba' AND s.status='passed'
    AND delivery_team_stage_current(s.task_id,s.revision,s.stage);
 IF v_criteria IS NULL OR jsonb_array_length(v_criteria)=0
    OR p_criteria @> v_criteria IS NOT TRUE OR v_criteria @> p_criteria IS NOT TRUE
    OR jsonb_array_length(p_criteria)<>jsonb_array_length(v_criteria) THEN RETURN false;END IF;
 -- Re-observation of the same immutable release must not replace its first
 -- receipt or invalidate a queued/completed QA digest with a new timestamp.
 INSERT INTO delivery_team_release_receipt(task_id,revision,sha,environment,criteria,reference,verified_by)
 VALUES(p_task_id,p_revision,p_sha,p_environment,p_criteria,p_reference,session_user)
 ON CONFLICT (task_id,revision,sha,environment) DO NOTHING;
 RETURN true;
END $$;

CREATE FUNCTION delivery_team_release_scope(p_task_id text,p_revision bigint)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
 SELECT jsonb_build_object('repo',t.repo,'source_sha',code.value,
     'criteria',(SELECT jsonb_agg(c->'id' ORDER BY c->>'id') FROM jsonb_array_elements(e.payload->'criteria') c))
 FROM backlog_task t JOIN delivery_team_stage s ON s.task_id=t.task_id AND s.revision=t.revision AND s.stage='ba'
 JOIN delivery_team_evidence e ON e.work_item_id=s.work_item_id
 CROSS JOIN LATERAL (SELECT b.value FROM backlog_evidence b WHERE b.task_id=t.task_id AND b.kind='sha'
    ORDER BY b.evidence_id DESC LIMIT 1) code
 WHERE t.task_id=p_task_id AND t.revision=p_revision AND s.status='passed'
   AND delivery_team_stage_current(s.task_id,s.revision,s.stage) AND code.value~'^[a-f0-9]{40}$'
   AND jsonb_typeof(e.payload->'criteria')='array' AND jsonb_array_length(e.payload->'criteria')>0
$$;

-- Only the operator/test runner authority may attest a successful criterion
-- check. The caller verifies and retains the artifact; model output is not input
-- to this function. A repeat must preserve the exact original observation.
CREATE FUNCTION delivery_team_record_acceptance(p_task_id text,p_revision bigint,p_sha text,
 p_criterion_id text,p_check_id text,p_reference text,p_observed_at timestamptz)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE r delivery_team_release_receipt%ROWTYPE; a delivery_team_acceptance_check%ROWTYPE; v_scope jsonb;
BEGIN
 PERFORM pg_advisory_xact_lock(73535001);
 PERFORM 1 FROM backlog_task b WHERE b.task_id=p_task_id AND b.revision=p_revision FOR UPDATE;
 IF NOT FOUND THEN RETURN false;END IF;
 v_scope:=delivery_team_release_scope(p_task_id,p_revision);
 IF v_scope IS NULL OR v_scope->>'source_sha' IS DISTINCT FROM p_sha
    OR coalesce(length(btrim(p_check_id)),0)=0
    OR coalesce(p_reference,'')!~'^https://[^[:space:]]+#sha256=[a-f0-9]{64}$'
    OR p_observed_at IS NULL OR p_observed_at>clock_timestamp() THEN RETURN false;END IF;
 SELECT * INTO r FROM delivery_team_release_receipt rr WHERE rr.task_id=p_task_id
   AND rr.revision=p_revision AND rr.sha=p_sha AND rr.environment='production';
 IF NOT FOUND OR NOT (r.criteria ? p_criterion_id) OR NOT (v_scope->'criteria' ? p_criterion_id)
    OR p_reference=r.reference OR p_observed_at<r.recorded_at THEN RETURN false;END IF;
 SELECT * INTO a FROM delivery_team_acceptance_check aa
   WHERE aa.receipt_id=r.receipt_id AND aa.criterion_id=p_criterion_id;
 IF FOUND THEN RETURN a.check_id=p_check_id AND a.reference=p_reference AND a.observed_at=p_observed_at;END IF;
 INSERT INTO delivery_team_acceptance_check(receipt_id,criterion_id,check_id,reference,observed_at,verified_by)
 VALUES(r.receipt_id,p_criterion_id,p_check_id,p_reference,p_observed_at,session_user);
 RETURN true;
END $$;

-- Operator-confirmed assignment is distinct from an auditor's proposal.
CREATE FUNCTION delivery_team_assign_correction(p_parent text,p_revision bigint,p_incident text,
 p_action jsonb,p_reference text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE v_existing jsonb;
BEGIN
 PERFORM pg_advisory_xact_lock(73535001);
 PERFORM 1 FROM backlog_task b WHERE b.task_id=p_parent AND b.revision=p_revision FOR UPDATE;
 IF NOT FOUND OR jsonb_typeof(p_action) IS DISTINCT FROM 'object'
   OR coalesce(p_reference,'')!~'^https://[^[:space:]]+$' THEN RETURN false;END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(p_action))<>4 OR EXISTS(
   SELECT 1 FROM unnest(ARRAY['task_id','owner','action','effectiveness_criterion_id']) k
   WHERE jsonb_typeof(p_action->k) IS DISTINCT FROM 'string' OR coalesce(length(btrim(p_action->>k)),0)=0)
   OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(_delivery_team_incidents(p_parent,p_revision)) i
     WHERE i->>'incident_key'=p_incident)
   OR NOT EXISTS(SELECT 1 FROM backlog_task_remediation m JOIN backlog_task b ON b.task_id=m.task_id
     WHERE m.parent_task_id=p_parent AND m.task_id=p_action->>'task_id' AND b.kind='task') THEN RETURN false;END IF;
 SELECT a.assignment INTO v_existing FROM delivery_team_corrective_assignment a
   WHERE a.parent_task_id=p_parent AND a.incident_key=p_incident AND a.task_id=p_action->>'task_id';
 IF FOUND THEN RETURN v_existing=p_action;END IF;
 INSERT INTO delivery_team_corrective_assignment(parent_task_id,incident_key,task_id,assignment,reference,recorded_by)
 VALUES(p_parent,p_incident,p_action->>'task_id',p_action,p_reference,session_user);
 RETURN true;
END $$;

-- The trusted check producer attests effectiveness only after checking actual
-- retained evidence. DONE or a canonical CI link alone is insufficient.
CREATE FUNCTION delivery_team_record_corrective_check(p_parent text,p_revision bigint,p_incident text,
 p_task text,p_task_revision bigint,p_sha text,p_criterion text,p_check text,p_reference text,p_observed_at timestamptz)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,public AS $$
DECLARE existing delivery_team_corrective_check%ROWTYPE; v_sha text; a delivery_team_corrective_assignment%ROWTYPE;
BEGIN
 PERFORM pg_advisory_xact_lock(73535001);
 PERFORM 1 FROM backlog_task b WHERE b.task_id=p_parent AND b.revision=p_revision FOR UPDATE;
 IF NOT FOUND OR coalesce(p_reference,'')!~'^https://[^[:space:]]+#sha256=[a-f0-9]{64}$'
   OR coalesce(length(btrim(p_check)),0)=0 OR p_observed_at IS NULL OR p_observed_at>clock_timestamp()
   OR NOT EXISTS(SELECT 1 FROM backlog_task b WHERE b.task_id=p_task AND b.revision=p_task_revision AND b.status='DONE') THEN RETURN false;END IF;
 SELECT * INTO a FROM delivery_team_corrective_assignment aa
   WHERE aa.parent_task_id=p_parent AND aa.incident_key=p_incident AND aa.task_id=p_task;
 IF NOT FOUND OR a.assignment->>'effectiveness_criterion_id' IS DISTINCT FROM p_criterion
   OR p_observed_at<a.recorded_at THEN RETURN false;END IF;
 SELECT e.value INTO v_sha FROM backlog_evidence e WHERE e.task_id=p_task AND e.kind='sha' ORDER BY e.evidence_id DESC LIMIT 1;
 IF p_sha IS DISTINCT FROM v_sha OR coalesce(p_sha,'')!~'^[a-f0-9]{40}$' THEN RETURN false;END IF;
 SELECT * INTO existing FROM delivery_team_corrective_check c WHERE c.parent_task_id=p_parent
   AND c.incident_key=p_incident AND c.task_id=p_task AND c.corrective_revision=p_task_revision AND c.sha=p_sha;
 IF FOUND THEN RETURN existing.reference=p_reference AND existing.criterion_id=p_criterion
   AND existing.check_id=p_check AND existing.observed_at=p_observed_at;END IF;
 INSERT INTO delivery_team_corrective_check(parent_task_id,incident_key,task_id,corrective_revision,sha,criterion_id,check_id,reference,observed_at,recorded_by)
 VALUES(p_parent,p_incident,p_task,p_task_revision,p_sha,p_criterion,p_check,p_reference,p_observed_at,session_user);
 RETURN true;
END $$;

CREATE FUNCTION _delivery_team_validate_rca(p_task_id text,p_revision bigint,p_stage text,p_result jsonb)
RETURNS text LANGUAGE plpgsql STABLE SET search_path=pg_catalog,public AS $$
DECLARE item jsonb; act jsonb; v_incidents jsonb; v_audit jsonb; v_seen text[]:=ARRAY[]::text[];
        v_keys text[]:=ARRAY[]::text[]; v_sha text; v_state text;
BEGIN
 IF jsonb_typeof(p_result->'rca') IS DISTINCT FROM 'array' THEN RETURN 'missing_rca';END IF;
 IF jsonb_array_length(p_result->'rca')>100 OR
   (p_result->>'verdict'='pass' AND jsonb_array_length(p_result->'rca')=0) THEN RETURN 'invalid_rca_count';END IF;
 v_incidents:=_delivery_team_incidents(p_task_id,p_revision);
 FOR item IN SELECT value FROM jsonb_array_elements(p_result->'rca') LOOP
  IF jsonb_typeof(item) IS DISTINCT FROM 'object' THEN RETURN 'invalid_rca';END IF;
  IF (SELECT count(*) FROM jsonb_object_keys(item))<>6
    OR jsonb_typeof(item->'incident_key') IS DISTINCT FROM 'string'
    OR coalesce(item->>'incident_key','')!~'^[a-f0-9]{64}$'
    OR EXISTS(SELECT 1 FROM unnest(ARRAY['root_cause','missed_control']) k WHERE
      jsonb_typeof(item->k) IS DISTINCT FROM 'string' OR coalesce(length(btrim(item->>k)),0)=0)
    OR jsonb_typeof(item->'facts') IS DISTINCT FROM 'array'
    OR jsonb_typeof(item->'hypotheses') IS DISTINCT FROM 'array'
    OR jsonb_typeof(item->'actions') IS DISTINCT FROM 'array' THEN RETURN 'invalid_rca';END IF;
  IF jsonb_array_length(item->'facts')=0 OR jsonb_array_length(item->'actions') NOT BETWEEN 1 AND 100
    OR EXISTS(SELECT 1 FROM jsonb_array_elements((item->'facts')||(item->'hypotheses')) v
      WHERE jsonb_typeof(v) IS DISTINCT FROM 'string' OR length(btrim(v#>>'{}'))=0)
    THEN RETURN 'invalid_rca';END IF;
  IF item->>'incident_key'=ANY(v_keys) THEN RETURN 'duplicate_rca_incident';END IF;
  v_keys:=array_append(v_keys,item->>'incident_key');
  IF NOT EXISTS(SELECT 1 FROM jsonb_array_elements(v_incidents) i
    WHERE i->>'incident_key'=item->>'incident_key') THEN RETURN 'foreign_rca_incident';END IF;
  v_seen:=ARRAY[]::text[];
  FOR act IN SELECT value FROM jsonb_array_elements(item->'actions') LOOP
   IF jsonb_typeof(act) IS DISTINCT FROM 'object' THEN RETURN 'invalid_corrective_action';END IF;
   IF (SELECT count(*) FROM jsonb_object_keys(act))<>4
     OR EXISTS(SELECT 1 FROM unnest(ARRAY['task_id','owner','action','effectiveness_criterion_id']) k WHERE
       jsonb_typeof(act->k) IS DISTINCT FROM 'string' OR coalesce(length(btrim(act->>k)),0)=0)
     OR (act->>'task_id')!~'^VOYN-[A-Za-z0-9][A-Za-z0-9._-]*$'
     OR act->>'task_id'=p_task_id
     OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p_result->'criteria') c
       WHERE c->>'id'=act->>'effectiveness_criterion_id') THEN RETURN 'invalid_corrective_action';END IF;
   IF act->>'task_id'=ANY(v_seen) THEN RETURN 'duplicate_corrective_task';END IF;
   v_seen:=array_append(v_seen,act->>'task_id');
   SELECT b.status INTO v_state FROM backlog_task b WHERE b.task_id=act->>'task_id' AND b.kind='task';
   IF NOT FOUND THEN RETURN 'missing_corrective_task';END IF;
   IF NOT EXISTS(SELECT 1 FROM backlog_task_remediation m WHERE m.task_id=act->>'task_id'
     AND m.parent_task_id=p_task_id) THEN RETURN 'unlinked_corrective_task';END IF;
   IF NOT EXISTS(SELECT 1 FROM delivery_team_corrective_assignment a WHERE a.parent_task_id=p_task_id
     AND a.incident_key=item->>'incident_key' AND a.task_id=act->>'task_id'
     AND a.assignment=act) THEN RETURN 'unassigned_corrective_action';END IF;
   IF p_stage='cto_rca' AND p_result->>'verdict'='pass' THEN
    IF v_state<>'DONE' THEN RETURN 'corrective_task_not_done';END IF;
    SELECT b.value INTO v_sha FROM backlog_evidence b WHERE b.task_id=act->>'task_id' AND b.kind='sha'
      ORDER BY b.evidence_id DESC LIMIT 1;
    IF v_sha IS NULL OR v_sha!~'^[a-f0-9]{40}$' OR NOT EXISTS(
      SELECT 1 FROM jsonb_array_elements(p_result->'evidence') proof JOIN delivery_team_corrective_check ci
        ON ci.parent_task_id=p_task_id AND ci.incident_key=item->>'incident_key'
        AND ci.task_id=act->>'task_id' AND ci.sha=v_sha AND ci.reference=proof->>'reference'
        AND ci.corrective_revision=(SELECT b.revision FROM backlog_task b WHERE b.task_id=act->>'task_id')
        AND ci.criterion_id=act->>'effectiveness_criterion_id'
      WHERE proof->>'criterion_id'=act->>'effectiveness_criterion_id' AND proof->>'sha'=v_sha
        AND proof->'verified'='true'::jsonb) THEN RETURN 'unproven_corrective_effectiveness';END IF;
   END IF;
  END LOOP;
 END LOOP;
 IF p_result->>'verdict'='pass' AND cardinality(v_keys)<>jsonb_array_length(v_incidents)
   THEN RETURN 'incomplete_incident_coverage';END IF;
 IF p_stage='cto_rca' AND p_result->>'verdict'='pass' THEN
  SELECT e.payload->'rca' INTO v_audit FROM delivery_team_stage s
   JOIN delivery_team_evidence e ON e.work_item_id=s.work_item_id
   WHERE s.task_id=p_task_id AND s.revision=p_revision AND s.stage='auditor' AND s.status='passed'
     AND delivery_team_stage_current(s.task_id,s.revision,s.stage);
  IF v_audit IS NULL OR p_result->'rca' IS DISTINCT FROM v_audit THEN RETURN 'audit_contract_mismatch';END IF;
 END IF;
 RETURN NULL;
END $$;

CREATE FUNCTION _delivery_team_validate(p_task_id text,p_revision bigint,p_stage text,p_digest text,p_result jsonb)
RETURNS text LANGUAGE plpgsql STABLE SET search_path = pg_catalog, public AS $$
DECLARE v_criterion jsonb; v_evidence jsonb; v_sha text; r delivery_team_release_receipt%ROWTYPE; v_ids jsonb; v_canonical jsonb; v_rca_reason text;
BEGIN
 IF jsonb_typeof(p_result) IS DISTINCT FROM 'object' THEN RETURN 'result_not_object';END IF;
 IF (SELECT count(*) FROM jsonb_object_keys(p_result))<>(CASE WHEN p_stage IN ('auditor','cto_rca') THEN 11 ELSE 10 END)
   OR p_result->>'schema' IS DISTINCT FROM 'delivery-team-review/v1'
   OR p_result->>'task_id' IS DISTINCT FROM p_task_id
   OR p_result->>'revision' IS DISTINCT FROM p_revision::text
   OR jsonb_typeof(p_result->'revision') IS DISTINCT FROM 'number'
   OR p_result->>'stage' IS DISTINCT FROM p_stage
   OR p_result->>'input_digest' IS DISTINCT FROM p_digest
   OR coalesce(p_result->>'verdict','') NOT IN ('pass','blocked','changes_requested')
   OR jsonb_typeof(p_result->'summary') IS DISTINCT FROM 'string'
   OR coalesce(length(btrim(p_result->>'summary')),0)=0
   OR jsonb_typeof(p_result->'criteria') IS DISTINCT FROM 'array'
   OR jsonb_typeof(p_result->'evidence') IS DISTINCT FROM 'array'
   OR jsonb_typeof(p_result->'blockers') IS DISTINCT FROM 'array' THEN RETURN 'invalid_schema';END IF;
 IF jsonb_array_length(p_result->'criteria')=0 THEN RETURN 'missing_criteria';END IF;
 FOR v_criterion IN SELECT value FROM jsonb_array_elements(p_result->'criteria') LOOP
  IF jsonb_typeof(v_criterion) IS DISTINCT FROM 'object' OR jsonb_typeof(v_criterion->'id') IS DISTINCT FROM 'string'
    OR jsonb_typeof(v_criterion->'description') IS DISTINCT FROM 'string'
    OR coalesce(length(btrim(v_criterion->>'id')),0)=0 OR coalesce(length(btrim(v_criterion->>'description')),0)=0
    THEN RETURN 'invalid_criterion';END IF;
 END LOOP;
 IF (SELECT count(DISTINCT value->>'id') FROM jsonb_array_elements(p_result->'criteria'))<>
    jsonb_array_length(p_result->'criteria') THEN RETURN 'duplicate_criteria';END IF;
 FOR v_criterion IN SELECT value FROM jsonb_array_elements(p_result->'blockers') LOOP
  IF jsonb_typeof(v_criterion) IS DISTINCT FROM 'string' OR length(btrim(v_criterion#>>'{}'))=0 THEN RETURN 'invalid_blocker';END IF;
 END LOOP;
 IF (p_result->>'verdict'='pass') IS DISTINCT FROM (jsonb_array_length(p_result->'blockers')=0)
    THEN RETURN 'verdict_blockers_mismatch';END IF;
 FOR v_evidence IN SELECT value FROM jsonb_array_elements(p_result->'evidence') LOOP
  IF jsonb_typeof(v_evidence) IS DISTINCT FROM 'object' OR jsonb_typeof(v_evidence->'reference') IS DISTINCT FROM 'string'
    OR coalesce(length(btrim(v_evidence->>'reference')),0)=0
    OR jsonb_typeof(v_evidence->'verified') IS DISTINCT FROM 'boolean'
    OR NOT (v_evidence ? 'sha') OR (v_evidence->'sha'<>'null'::jsonb AND
       (jsonb_typeof(v_evidence->'sha') IS DISTINCT FROM 'string' OR coalesce(v_evidence->>'sha','')!~'^[a-f0-9]{40}$'))
    OR NOT EXISTS(SELECT 1 FROM jsonb_array_elements(p_result->'criteria') c WHERE c->>'id'=v_evidence->>'criterion_id')
    THEN RETURN 'invalid_evidence';END IF;
 END LOOP;
 IF p_stage IN ('auditor','cto_rca') THEN
  v_rca_reason:=_delivery_team_validate_rca(p_task_id,p_revision,p_stage,p_result);
  IF v_rca_reason IS NOT NULL THEN RETURN v_rca_reason;END IF;
 END IF;
 IF p_result->>'verdict'='pass' THEN
  -- Every claimed criterion needs linked, nonempty, positively verified
  -- evidence even for pre-code BA/technologist/QA requirements.
  IF EXISTS(SELECT 1 FROM jsonb_array_elements(p_result->'criteria') c WHERE NOT EXISTS(
      SELECT 1 FROM jsonb_array_elements(p_result->'evidence') e
      WHERE e->>'criterion_id'=c->>'id' AND e->'verified'='true'::jsonb))
    THEN RETURN 'unproven_criterion';END IF;
  IF p_stage IN ('independent_reviewer','devops','production_qa') THEN
   SELECT e.value INTO v_sha FROM backlog_evidence e WHERE e.task_id=p_task_id AND e.kind='sha'
     ORDER BY e.evidence_id DESC LIMIT 1;
   IF v_sha IS NULL OR EXISTS(SELECT 1 FROM jsonb_array_elements(p_result->'evidence') e
       WHERE e->>'sha' IS DISTINCT FROM v_sha OR e->'verified' IS DISTINCT FROM 'true'::jsonb)
     THEN RETURN 'code_sha_mismatch';END IF;
  END IF;
  IF p_stage='production_qa' THEN
   SELECT * INTO r FROM delivery_team_release_receipt rr WHERE rr.task_id=p_task_id
     AND rr.revision=p_revision ORDER BY rr.receipt_id DESC LIMIT 1;
   IF NOT FOUND OR r.sha IS DISTINCT FROM v_sha THEN RETURN 'missing_trusted_receipt';END IF;
   SELECT e.payload->'criteria' INTO v_canonical FROM delivery_team_stage b
     JOIN delivery_team_evidence e ON e.work_item_id=b.work_item_id
     WHERE b.task_id=p_task_id AND b.revision=p_revision AND b.stage='ba' AND b.status='passed';
   IF v_canonical IS NULL OR p_result->'criteria' @> v_canonical IS NOT TRUE
      OR v_canonical @> (p_result->'criteria') IS NOT TRUE
      OR jsonb_array_length(v_canonical)<>jsonb_array_length(p_result->'criteria')
      THEN RETURN 'criterion_contract_mismatch';END IF;
   SELECT jsonb_agg(c->'id') INTO v_ids FROM jsonb_array_elements(p_result->'criteria') c;
   IF v_ids @> r.criteria IS NOT TRUE OR r.criteria @> v_ids IS NOT TRUE
      OR jsonb_array_length(v_ids)<>jsonb_array_length(r.criteria)
      OR EXISTS(SELECT 1 FROM jsonb_array_elements(p_result->'evidence') e
        WHERE e->>'environment' IS DISTINCT FROM r.environment)
      THEN RETURN 'production_receipt_mismatch';END IF;
   IF EXISTS(SELECT 1 FROM jsonb_array_elements(p_result->'criteria') c WHERE NOT EXISTS(
      SELECT 1 FROM delivery_team_acceptance_check a
       JOIN LATERAL jsonb_array_elements(p_result->'evidence') proof ON true
      WHERE a.receipt_id=r.receipt_id AND a.criterion_id=c->>'id'
        AND proof->>'criterion_id'=a.criterion_id AND proof->>'reference'=a.reference
        AND proof->>'sha'=r.sha AND proof->'verified'='true'::jsonb
        AND proof->>'environment'=r.environment)) THEN RETURN 'unproven_production_criteria';END IF;
  END IF;
 END IF;
 RETURN NULL;
END $$;

CREATE FUNCTION delivery_team_ingest(p_limit integer DEFAULT 100)
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
CREATE FUNCTION delivery_team_accepted(p_task_id text,p_revision bigint)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
 SELECT EXISTS(SELECT 1 FROM delivery_team_stage s JOIN delivery_team_evidence e ON e.work_item_id=s.work_item_id
   WHERE s.task_id=p_task_id AND s.revision=p_revision AND s.stage='production_qa' AND s.status='passed'
     AND delivery_team_stage_current(s.task_id,s.revision,s.stage)
     AND _delivery_team_validate(p_task_id,p_revision,'production_qa',s.input_digest,e.payload) IS NULL)
$$;
-- A corrective retry is explicit, bounded, and requires the exact current
-- input. It cannot silently reset a passed stage or infinite-loop a refusal.
CREATE FUNCTION delivery_team_retry(p_task_id text,p_revision bigint,p_stage text,p_input_digest text)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
 PERFORM pg_advisory_xact_lock(73535001);
 UPDATE delivery_team_stage s SET status='pending',work_item_id=NULL,reason='explicit_retry',updated_at=now()
  WHERE s.task_id=p_task_id AND s.revision=p_revision AND s.stage=p_stage AND s.status='blocked'
    AND s.dispatch_count<2 AND s.input_digest=p_input_digest
    AND delivery_team_stage_current(s.task_id,s.revision,s.stage);
 RETURN FOUND;
END $$;
-- Opt-in is enrollment by sync: un-enrolled legacy tasks preserve existing
-- policy; enrolled tasks cannot bypass the gate by calling backlog_dispatch
-- directly. Readonly reviews carry neither queue task_id nor backlog_task_id.
CREATE FUNCTION _delivery_team_implementation_gate() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public AS $$
DECLARE v_task text;v_revision bigint;
BEGIN
 v_task:=coalesce(NEW.task_id,nullif(NEW.payload->>'backlog_task_id',''));
 IF v_task IS NULL THEN RETURN NEW;END IF;
 IF coalesce(NEW.payload->>'task_type','') NOT IN ('implementation','remediation')
    AND NOT EXISTS(SELECT 1 FROM jsonb_array_elements(CASE
      WHEN jsonb_typeof(NEW.payload->'cascade')='array' THEN NEW.payload->'cascade' ELSE '[]'::jsonb END) c
      WHERE c->>'task_type' IN ('implementation','remediation')) THEN RETURN NEW;END IF;
 IF EXISTS(SELECT 1 FROM delivery_team_stage s WHERE s.task_id=v_task) THEN
  SELECT t.revision INTO v_revision FROM backlog_task t WHERE t.task_id=v_task;
  IF NOT delivery_team_ready(v_task,v_revision) THEN
   RAISE EXCEPTION 'delivery_team_requirements_not_ready: %',v_task USING ERRCODE='check_violation';
  END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER delivery_team_implementation_gate BEFORE INSERT ON work_item
 FOR EACH ROW EXECUTE FUNCTION _delivery_team_implementation_gate();

-- No implicit PUBLIC EXECUTE window, even before grant reconciliation.
REVOKE ALL ON FUNCTION _delivery_team_append_only() FROM PUBLIC;
REVOKE ALL ON FUNCTION _delivery_team_context(text,bigint,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION _delivery_team_digest(text,bigint,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION _delivery_team_eligible(text,bigint,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION _delivery_team_validate(text,bigint,text,text,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION _delivery_team_implementation_gate() FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_sync(integer,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_candidates(integer,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_dispatch(text,bigint,text,text,jsonb,integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_stage_current(text,bigint,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_ready(text,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_record_release(text,bigint,text,text,jsonb,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_ingest(integer) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_accepted(text,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_retry(text,bigint,text,text) FROM PUBLIC;

REVOKE ALL ON FUNCTION delivery_team_record_incident(text,bigint,text,text,text,text,text,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION _delivery_team_incidents(text,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_release_scope(text,bigint) FROM PUBLIC;
REVOKE ALL ON FUNCTION _delivery_team_validate_rca(text,bigint,text,jsonb) FROM PUBLIC;

REVOKE ALL ON FUNCTION delivery_team_record_acceptance(text,bigint,text,text,text,text,timestamptz) FROM PUBLIC;

REVOKE ALL ON FUNCTION delivery_team_assign_correction(text,bigint,text,jsonb,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION delivery_team_record_corrective_check(text,bigint,text,text,bigint,text,text,text,text,timestamptz) FROM PUBLIC;
