-- 0016_run_finalization_claim
--
-- PostgreSQL target for the host-local finalization fence.  The current
-- SQLite authority does not dual-write this row: PID/start identity only have
-- meaning on the execution host and a best-effort mirror cannot preserve its
-- compare-and-swap semantics.  The target exists so a future authority
-- cutover has an explicit native schema; cutover requires a zero-open-claim
-- drain and a PostgreSQL-native claim implementation.

CREATE TABLE run_finalization_claim (
    run_id text PRIMARY KEY REFERENCES run(id) ON DELETE CASCADE,
    owner_token text NOT NULL CHECK (owner_token <> ''),
    owner_pid bigint NOT NULL CHECK (owner_pid > 0),
    owner_identity text NOT NULL CHECK (owner_identity <> ''),
    claimed_at timestamptz NOT NULL,
    completed_at timestamptz,
    CHECK (completed_at IS NULL OR completed_at >= claimed_at)
);

CREATE INDEX idx_run_finalization_claim_open
    ON run_finalization_claim (completed_at)
    WHERE completed_at IS NULL;

REVOKE ALL ON TABLE run_finalization_claim FROM PUBLIC;
