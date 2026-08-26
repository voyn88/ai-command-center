-- Revert BO-S1 structured backlog store.
DROP FUNCTION IF EXISTS backlog_lease_release(text, text);
DROP FUNCTION IF EXISTS backlog_lease_heartbeat(text, text, integer);
DROP FUNCTION IF EXISTS backlog_lease_acquire(text, text, integer);
DROP FUNCTION IF EXISTS backlog_add_dependency(text, text);
DROP FUNCTION IF EXISTS backlog_record_evidence(text, text, text);
DROP FUNCTION IF EXISTS backlog_transition(text, text, bigint);
DROP FUNCTION IF EXISTS backlog_upsert_task(text, text, text, text, text, text, text, text);
DROP TYPE IF EXISTS backlog_lease_verdict;
DROP TYPE IF EXISTS backlog_dependency_verdict;
DROP TYPE IF EXISTS backlog_verdict;
DROP FUNCTION IF EXISTS _backlog_audit(text, text, text, text, jsonb);
DROP TABLE IF EXISTS backlog_event;
DROP TABLE IF EXISTS backlog_evidence;
DROP TABLE IF EXISTS backlog_writer_lease;
DROP TABLE IF EXISTS backlog_dependency;
DROP TABLE IF EXISTS backlog_task;
