-- 0029 down: the 0003 self-rotation, which refuses an expired credential outright.
CREATE OR REPLACE FUNCTION enroll_rotate_self(
    p_current_secret     text,
    p_new_secret_hash    text,
    p_new_scram_verifier text
) RETURNS TABLE (new_expires_at timestamptz, refuse_reason text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    v      identity_verdict;
    v_cred text;
    v_why  text;
    v_e    timestamptz;
BEGIN
    -- Proof of possession IS the authorisation, and it runs the full gate, so a
    -- revoked, expired, suspended or out-of-CIDR credential cannot rotate
    -- itself back to life. Note that this consumes the verdict rather than
    -- raising on it: raising here would abort the transaction containing the
    -- denial audit `identity_assert()` just wrote.
    v := identity_assert(p_current_secret);
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
    SELECT c.expires_at INTO v_e
      FROM principal_credential c WHERE c.credential_id = v_cred;
    PERFORM _principal_audit(v.principal_id, 'rotate', 'granted', v_cred, NULL,
             jsonb_build_object('replaced', v.credential_id));
    RETURN QUERY SELECT v_e, NULL::text;
END
$$;
