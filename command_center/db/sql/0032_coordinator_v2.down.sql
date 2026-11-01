DROP FUNCTION IF EXISTS coordinator_enqueue(text, text, text, text, text, jsonb);
DROP FUNCTION IF EXISTS coordinator_cas_clear_inflight(text, text, text, timestamptz);
DROP FUNCTION IF EXISTS coordinator_cas_finish(text, text, text, jsonb, text, jsonb, integer, timestamptz);
DROP FUNCTION IF EXISTS coordinator_cas_begin(text, text, text, text, timestamptz);
DROP FUNCTION IF EXISTS coordinator_state_read(text);
DROP TABLE IF EXISTS coordinator_state;
