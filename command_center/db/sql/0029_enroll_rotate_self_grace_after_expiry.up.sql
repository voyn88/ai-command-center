-- 0029: an expired, unrevoked credential may still renew ITSELF for a grace
-- window (VOYN-W0-AICC-CREDENTIAL-ROTATION-DEADLOCKS-AFTER-EXPIRY).
--
-- The worker rotator authenticates to `enroll_rotate_self` with the very
-- credential it is renewing. Once that credential's `expires_at` passed --
-- live 2026-09-15 05:22 UTC, after three rotations were refused for a
-- readiness reason unrelated to the credential -- every later attempt failed
-- `credential_expired` and the host could never recover on its own: 164
-- refusals, the lanes dead from 06:17 until an operator extended the row by
-- hand at 10:33.
--
-- Expiry still means what it meant for WORK: `identity_assert` keeps refusing
-- an expired credential everywhere else, so a stale secret buys nothing but
-- the right to replace itself. Within `_ENROLL_SELF_GRACE` after expiry, and
-- only if the credential is not revoked and its principal is active,
-- `enroll_rotate_self` issues the successor exactly as it would have a minute
-- before expiry; the grant is audited as `rotate` with `expired_grace`
-- detail so the ledger shows it happened. Past the grace window the answer
-- is `credential_expired` as before; a revoked credential is refused as
-- before (`identity_assert` answers first and its refusal stands unless it is
-- exactly `credential_expired`).
CREATE OR REPLACE FUNCTION enroll_rotate_self(
    p_current_secret     text,
    p_new_secret_hash    text,
    p_new_scram_verifier text
) RETURNS TABLE (new_expires_at timestamptz, refuse_reason text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    v       identity_verdict;
    c       principal_credential%ROWTYPE;
    p       principal%ROWTYPE;
    v_cred  text;
    v_why   text;
    v_e     timestamptz;
    v_grace constant interval := interval '24 hours';
    v_graced boolean := false;
BEGIN
    v := identity_assert(p_current_secret);
    IF NOT v.ok AND v.reason = 'credential_expired' THEN
        SELECT * INTO c FROM principal_credential
         WHERE secret_hash = encode(sha256(convert_to(p_current_secret, 'UTF8')), 'hex');
        IF FOUND AND c.revoked_at IS NULL AND c.expires_at > now() - v_grace THEN
            SELECT * INTO p FROM principal WHERE principal_id = c.principal_id;
            IF FOUND AND p.state = 'active' THEN
                v.ok := true;
                v.reason := NULL;
                v.credential_id := c.credential_id;
                v.principal_id := c.principal_id;
                v_graced := true;
            END IF;
        END IF;
    END IF;
    IF NOT v.ok THEN
        RETURN QUERY SELECT NULL::timestamptz, v.reason;
        RETURN;
    END IF;

    SELECT issued_credential_id, issue_refuse_reason INTO v_cred, v_why
      FROM identity_issue_db_credential(v.principal_id, p_new_secret_hash,
                                        p_new_scram_verifier, interval '1 hour');
    IF v_cred IS NULL THEN
        RETURN QUERY SELECT NULL::timestamptz, v_why;
        RETURN;
    END IF;
    SELECT x.expires_at INTO v_e
      FROM principal_credential x WHERE x.credential_id = v_cred;
    PERFORM _principal_audit(v.principal_id, 'rotate', 'granted', v_cred, NULL,
             jsonb_build_object('replaced', v.credential_id,
                                'expired_grace', v_graced));
    RETURN QUERY SELECT v_e, NULL::text;
END
$$;
