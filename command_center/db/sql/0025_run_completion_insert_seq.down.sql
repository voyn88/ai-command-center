-- Downgrade of 0025_run_completion_insert_seq (VOYN-W0-AICC-INSERT-SEQ).
--
-- Reversibility is asserted rather than asserted-about: the migration set's
-- round trip runs up -> down -> up -> down and requires the schema afterwards
-- to be byte-identical to the pre-0025 one — see
-- `tests/db/test_insert_seq_migration.py`.
--
-- Two things an identity column leaves behind that a plain one does not, and
-- both are handled by `DROP COLUMN` rather than by hand: the sequence it owns
-- (dropped with the column, because the column owns it) and the identity
-- attribute itself. Naming the sequence here would be the mistake — its name
-- is PostgreSQL's to choose, and a hand-written `DROP SEQUENCE` that guessed
-- right today would leave a stray object the next upgrade cannot recreate the
-- moment the naming changed.
--
-- The indexes are named first anyway. Dropping the columns would take them
-- along; writing them out keeps this file readable as the exact inverse of the
-- upgrade instead of relying on that cascade — the same rule 0004 follows.

DROP INDEX IF EXISTS idx_completion_task_insert_seq;

DROP INDEX IF EXISTS idx_run_task_insert_seq;

DROP INDEX IF EXISTS idx_completion_insert_seq;

DROP INDEX IF EXISTS idx_run_insert_seq;

ALTER TABLE completion DROP COLUMN IF EXISTS insert_seq;

ALTER TABLE run DROP COLUMN IF EXISTS insert_seq;
