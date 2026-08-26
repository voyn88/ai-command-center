-- Downgrade of 0003_worker_enrollment (VOYN-W0-AICC-SRV-03).
--
-- Reversibility matters here for a reason the queue's downgrade does not have:
-- this migration creates CLUSTER-level objects. A per-host LOGIN role outlives
-- the database it was created for, so a downgrade that dropped only the tables
-- would leave behind exactly the thing the migration exists to control — a
-- login with `aicc_worker`'s grant set and a password nothing now records. The
-- roles are therefore dropped from the record of them, before that record goes.
--
-- `test_up_down_up_down_leaves_no_enrolment_object` asserts both halves over
-- up -> down -> up -> down: no table, view, function or type survives, and no
-- `aicc_w_*` role does either.

-- The role drop reads `principal` while it still exists. `DROP ROLE` fails if
-- the role owns objects or holds grants; a per-host role owns nothing and holds
-- only its membership of `aicc_worker`, which goes with it.
DO $$
DECLARE r record;
BEGIN
    FOR r IN SELECT db_role FROM principal WHERE kind = 'worker_host' LOOP
        EXECUTE format('DROP ROLE IF EXISTS %I', r.db_role);
    END LOOP;
END
$$;

DROP FUNCTION IF EXISTS enroll_sweep_expired();
DROP FUNCTION IF EXISTS enroll_revoke_ticket(text, text);
DROP FUNCTION IF EXISTS enroll_rotate_self(text, text, text);
DROP FUNCTION IF EXISTS enroll_redeem_ticket(text, text, text, jsonb);
DROP FUNCTION IF EXISTS enroll_mint_ticket(text, text, text, inet, interval, text);
DROP FUNCTION IF EXISTS _enroll_fingerprint_hash(jsonb);

DROP FUNCTION IF EXISTS identity_issue_db_credential(text, text, text, interval);
DROP FUNCTION IF EXISTS identity_enroll_worker(text, text, text, inet);
DROP FUNCTION IF EXISTS identity_bootstrap_principal(text, text, text, text, text);
DROP FUNCTION IF EXISTS identity_sweep_expired();
DROP FUNCTION IF EXISTS identity_revoke_principal(text, text);
DROP FUNCTION IF EXISTS identity_revoke_credential(text, text);
DROP FUNCTION IF EXISTS identity_disable_role(text);
DROP FUNCTION IF EXISTS identity_set_role_secret(text, text, timestamptz);
DROP FUNCTION IF EXISTS identity_create_worker_role(text);
DROP FUNCTION IF EXISTS identity_assert(text);
DROP FUNCTION IF EXISTS _principal_audit(text, text, text, text, text, jsonb);
DROP FUNCTION IF EXISTS _identity_new_id(text);
DROP FUNCTION IF EXISTS current_principal();

DROP VIEW IF EXISTS enrollment_ticket_public;
DROP VIEW IF EXISTS principal_credential_public;

DROP TABLE IF EXISTS worker_host_fingerprint;
DROP TABLE IF EXISTS enrollment_ticket;
DROP TABLE IF EXISTS principal_event;
DROP TABLE IF EXISTS principal_credential;
DROP TABLE IF EXISTS principal;

DROP TYPE IF EXISTS identity_verdict;
