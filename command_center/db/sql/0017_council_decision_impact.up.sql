-- 0017_council_decision_impact (VOYN-MIN-WOW-1)
--
-- The Decision P&L pillar of the client-facing proof package needs a place to
-- record a decision's estimated financial/time impact. `council_decision` has
-- no update path -- a decision is written exactly once and never edited -- so
-- the column must exist at insert time rather than be patched on afterward.
--
-- Nullable and write-once, same shape as `run.finalized_at` (0004): existing
-- rows get NULL, which is honest -- those decisions never estimated an
-- impact, and backfilling one would manufacture an estimate nobody made.

ALTER TABLE council_decision ADD COLUMN impact_json jsonb;
