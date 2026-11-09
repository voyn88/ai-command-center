-- Downgrade is safe only before activation; do not turn rollback into bypass.
DO $$ BEGIN
 PERFORM pg_advisory_xact_lock(73536001);
 IF (SELECT enabled FROM cto_control WHERE singleton) THEN
  RAISE EXCEPTION 'activated CTO authorization requires a reviewed forward migration';
 END IF;
END $$;
DROP FUNCTION queue_claim(text,text,integer);
DROP FUNCTION backlog_dispatch(text,text,integer,integer,jsonb,integer);
DROP FUNCTION queue_enqueue(text,text,jsonb,text,text,integer,integer,integer,integer);
ALTER FUNCTION _cto_legacy_claim(text,text,integer) RENAME TO queue_claim;
ALTER FUNCTION _cto_legacy_dispatch(text,text,integer,integer,jsonb,integer) RENAME TO backlog_dispatch;
ALTER FUNCTION _cto_legacy_enqueue(text,text,jsonb,text,text,integer,integer,integer,integer) RENAME TO queue_enqueue;
DROP FUNCTION cto_authorize_start(text,text);
DROP FUNCTION _cto_work_reason(text,boolean);
DROP FUNCTION _cto_assignment_for(text,text,jsonb);
DROP FUNCTION cto_activate(text);
DROP FUNCTION cto_revoke(text,text);
DROP FUNCTION cto_assign(text,bigint,text,jsonb,text);
DROP FUNCTION _cto_task(text,jsonb);
DROP FUNCTION _cto_digest(jsonb);
DROP FUNCTION _cto_enabled();
DROP FUNCTION _cto_operator();
DROP FUNCTION _cto_audit(text,text,text,text,text);
DROP TABLE cto_assignment_event,cto_execution_start,cto_work_binding,cto_assignment,cto_control;
GRANT EXECUTE ON FUNCTION queue_claim(text,text,integer) TO aicc_worker;
GRANT EXECUTE ON FUNCTION queue_enqueue(text,text,jsonb,text,text,integer,integer,integer,integer),
 backlog_dispatch(text,text,integer,integer,jsonb,integer) TO aicc_app;
