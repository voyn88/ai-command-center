-- A rotator must know the server-authoritative expiry of the credential it is
-- about to use before it closes worker claim gates. Client wall clocks are not
-- trusted and the worker role deliberately cannot read credential tables, so
-- expose only the caller's proved credential and the database clock used to
-- evaluate it.
CREATE FUNCTION identity_current_credential(p_secret text)
    RETURNS TABLE (
        current_expires_at timestamptz,
        server_now timestamptz,
        refuse_reason text
    )
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    v identity_verdict;
    v_expires timestamptz;
    v_now timestamptz;
BEGIN
    v := identity_assert(p_secret);
    v_now := clock_timestamp();
    IF NOT v.ok THEN
        RETURN QUERY SELECT NULL::timestamptz, v_now, v.reason;
        RETURN;
    END IF;

    SELECT c.expires_at INTO v_expires
      FROM principal_credential c
     WHERE c.credential_id = v.credential_id
       AND c.principal_id = v.principal_id
       AND c.revoked_at IS NULL;
    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::timestamptz, v_now,
                            'credential_not_current'::text;
        RETURN;
    END IF;
    RETURN QUERY SELECT v_expires, v_now, NULL::text;
END
$$;

-- Close PostgreSQL's default PUBLIC EXECUTE window inside the migration; the
-- normal post-migration role reconciliation re-asserts the same final matrix.
REVOKE ALL ON FUNCTION identity_current_credential(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity_current_credential(text) TO aicc_worker;
