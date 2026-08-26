-- Downgrade of 0002_queue_claim (VOYN-W0-AICC-SRV-04b).
--
-- Reversibility matters here for a specific reason: this migration introduces a
-- candidate authority (`work_item`) alongside an existing mirror
-- (`queue_entry`). If the downgrade left any part of the new authority behind,
-- the database would hold two partial claims on the same question — which is
-- the duplicate-authority failure the project rules forbid outright. So the
-- test is not "does it drop the tables" but "is the schema afterwards
-- byte-identical to the pre-0002 schema".
-- `test_up_down_up_down_leaves_the_schema_byte_identical` asserts exactly that,
-- over up -> down -> up -> down.
--
-- 0002 never altered `queue_entry`, so there is nothing to restore on it.

DROP FUNCTION IF EXISTS queue_redrive(text, integer);
DROP FUNCTION IF EXISTS queue_reap();
DROP FUNCTION IF EXISTS queue_fail(text, text, text, boolean);
DROP FUNCTION IF EXISTS queue_complete(text, text, jsonb);
DROP FUNCTION IF EXISTS queue_heartbeat(text, text);
DROP FUNCTION IF EXISTS _queue_owns(text, text);
DROP FUNCTION IF EXISTS queue_claim(text, text, integer);
DROP FUNCTION IF EXISTS queue_enqueue(text, text, jsonb, text, text, integer, integer, integer, integer);

DROP VIEW IF EXISTS work_item_public;
DROP VIEW IF EXISTS work_dlq;
DROP VIEW IF EXISTS work_attempt_public;

DROP TRIGGER IF EXISTS trg_work_attempt_claimant_is_derived ON work_attempt;
DROP FUNCTION IF EXISTS work_attempt_claimant_is_derived();

DROP FUNCTION IF EXISTS _queue_backoff(integer, integer);
DROP FUNCTION IF EXISTS _queue_audit(text, text, text, text, text, jsonb);
DROP FUNCTION IF EXISTS _queue_new_id(text);

-- Order matters: work_item and work_attempt reference each other.
ALTER TABLE IF EXISTS work_item  DROP CONSTRAINT IF EXISTS work_item_current_attempt_fk;
ALTER TABLE IF EXISTS work_item  DROP CONSTRAINT IF EXISTS work_item_result_fk;
ALTER TABLE IF EXISTS work_attempt DROP CONSTRAINT IF EXISTS work_attempt_result_fk;

DROP TABLE IF EXISTS work_event;
DROP TABLE IF EXISTS work_result;
DROP TABLE IF EXISTS work_attempt;
DROP TABLE IF EXISTS work_item;

DROP TYPE IF EXISTS queue_verdict;
