-- Downgrade of 0004_run_finalized_at (VOYN-W0-AICC-SRV-09-FINALIZED-AT).
--
-- Reversibility is asserted rather than asserted-about: the migration set's
-- round trip runs up -> down -> up -> down and requires the schema afterwards
-- to be byte-identical to the pre-0003 one. A dropped column that leaves its
-- index behind, or an index dropped without its column, would leave the
-- database holding half of a marker nothing writes — which reads exactly like
-- a finalization gate that is switched on when it is not.
--
-- Order matters only in the sense that dropping the column would take the
-- partial index with it; naming the index first keeps the downgrade readable
-- as the exact inverse of the upgrade rather than relying on that cascade.

DROP INDEX IF EXISTS idx_run_unfinalized;

ALTER TABLE run DROP COLUMN IF EXISTS finalized_at;
