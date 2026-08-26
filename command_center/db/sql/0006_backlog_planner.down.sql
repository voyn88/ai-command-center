-- Revert BO-S2 planner protocol.
DROP FUNCTION IF EXISTS backlog_release_terminal(text);
DROP FUNCTION IF EXISTS backlog_dispatch(text, text, integer, integer, jsonb, integer);
DROP TYPE IF EXISTS backlog_dispatch_verdict;
DROP FUNCTION IF EXISTS _backlog_earliest_wave_candidate(text);
DROP FUNCTION IF EXISTS _backlog_repo_free(text, text);
DROP VIEW IF EXISTS backlog_eligible;
