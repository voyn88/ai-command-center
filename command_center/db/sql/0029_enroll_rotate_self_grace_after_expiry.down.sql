-- 0029 down: the 0003 definitions (expiry is refused outright, role validity
-- equals the ledger expiry) and no grace helper.
CREATE OR REPLACE FUNCTION identity_assert(p_secret text)
    RETURNS identity_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    c principal_credential%ROWTYPE;
    p principal%ROWTYPE;
    v identity_verdict;
    v_hash text := encode(sha256(convert_to(p_secret, 'UTF8')), 'hex');
BEGIN
    v.ok := false;

    SELECT * INTO c FROM principal_credential WHERE secret_hash = v_hash;
    IF NOT FOUND THEN
        -- Nothing to attribute this to; that is what the nullable column is
        -- for. A caller must not turn the distinct reasons below into an oracle
        -- at an external boundary — they are for the audit, not for an API.
        PERFORM _principal_audit(NULL, 'assert', 'rejected', NULL,
                                 'unknown_credential', NULL);
        v.reason := 'unknown_credential';
        RETURN v;
    END IF;

    SELECT * INTO p FROM principal WHERE principal_id = c.principal_id;
    v.credential_id := c.credential_id;
    v.principal_id  := c.principal_id;

    -- Order matters: the identity mismatch is checked BEFORE expiry and
    -- revocation, so "one principal acting as another" is always recorded as
    -- act_as/principal_role_mismatch and never masked by an incidental expiry.
    IF p.db_role IS DISTINCT FROM session_user THEN
        PERFORM _principal_audit(c.principal_id, 'act_as', 'rejected', c.credential_id,
                 'principal_role_mismatch',
                 jsonb_build_object('presented_by', session_user, 'belongs_to', p.db_role));
        v.reason := 'principal_role_mismatch';
        RETURN v;
    END IF;

    IF c.revoked_at IS NOT NULL THEN
        PERFORM _principal_audit(c.principal_id, 'assert', 'rejected', c.credential_id,
                 'credential_revoked', jsonb_build_object('revoke_reason', c.revoke_reason));
        v.reason := 'credential_revoked';
        RETURN v;
    END IF;

    -- Every deadline is compared against the SERVER's now(). No client clock is
    -- written to or read from this table, so skew between hosts cannot lengthen
    -- or shorten a credential.
    IF c.expires_at <= now() THEN
        PERFORM _principal_audit(c.principal_id, 'assert', 'rejected', c.credential_id,
                 'credential_expired', jsonb_build_object('expires_at', c.expires_at));
        v.reason := 'credential_expired';
        RETURN v;
    END IF;

    IF p.state <> 'active' THEN
        PERFORM _principal_audit(c.principal_id, 'assert', 'rejected', c.credential_id,
                 'principal_inactive', jsonb_build_object('state', p.state));
        v.reason := 'principal_inactive';
        RETURN v;
    END IF;

    -- The network the operator declared IN ADVANCE, checked before the address
    -- the credential happened to be used from first. A worker host is refused
    -- outright; operators and the control plane move around, so for them it is
    -- audited and allowed.
    IF p.expected_cidr IS NOT NULL AND inet_client_addr() IS NOT NULL
       AND NOT (inet_client_addr() <<= p.expected_cidr) THEN
        PERFORM _principal_audit(c.principal_id, 'assert', 'rejected', c.credential_id,
                 'addr_mismatch', jsonb_build_object('expected_cidr', p.expected_cidr,
                                                     'seen', inet_client_addr(),
                                                     'check', 'expected_cidr'));
        IF p.kind = 'worker_host' THEN
            v.reason := 'addr_mismatch';
            RETURN v;
        END IF;
    END IF;

    IF c.bound_addr IS NOT NULL AND inet_client_addr() IS NOT NULL
       AND c.bound_addr <> inet_client_addr() THEN
        PERFORM _principal_audit(c.principal_id, 'assert', 'rejected', c.credential_id,
                 'addr_mismatch', jsonb_build_object('bound', c.bound_addr,
                                                     'seen', inet_client_addr(),
                                                     'check', 'bound_addr'));
        IF p.kind = 'worker_host' THEN
            v.reason := 'addr_mismatch';
            RETURN v;
        END IF;
    END IF;

    UPDATE principal_credential
       SET last_used_at = now(),
           bound_addr   = coalesce(bound_addr, inet_client_addr()),
           updated_at   = now()
     WHERE credential_id = c.credential_id;

    v.ok     := true;
    v.reason := NULL;
    RETURN v;
END
$$;

DROP FUNCTION IF EXISTS identity_assert(text, interval);

CREATE OR REPLACE FUNCTION identity_issue_db_credential(
    p_principal_id   text,
    p_secret_hash    text,
    p_scram_verifier text,
    p_ttl            interval
) RETURNS TABLE (issued_credential_id text, issue_refuse_reason text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    p             principal%ROWTYPE;
    v_id          text;
    v_ttl         interval;
    v_caller      text;
    v_caller_tier integer;
    r             record;
BEGIN
    SELECT * INTO p FROM principal WHERE principal_id = p_principal_id FOR UPDATE;
    IF NOT FOUND OR p.state <> 'active' THEN
        PERFORM _principal_audit(p_principal_id, 'issue', 'rejected', NULL,
                                 'principal_inactive', NULL);
        RETURN QUERY SELECT NULL::text, 'principal_inactive'::text;
        RETURN;
    END IF;

    -- Strictly-lower-trust rule: nobody may mint a credential at or above its
    -- own tier. That is what stops a compromised worker issuing itself a
    -- control-plane credential. SELF-issuance is exempt, because a worker and
    -- itself are the same tier and rotation must remain possible: the caller
    -- already proved possession of the current secret and the TTL is still
    -- clamped, so it can change WHICH secret works and nothing else. A caller
    -- with no principal row is the provisioning path, which runs before any
    -- principal exists.
    v_caller := current_principal();
    IF v_caller IS NOT NULL AND v_caller IS DISTINCT FROM p_principal_id THEN
        SELECT trust_tier INTO v_caller_tier FROM principal WHERE principal_id = v_caller;
        IF v_caller_tier >= p.trust_tier THEN
            PERFORM _principal_audit(p_principal_id, 'issue', 'rejected', NULL,
                     'tier_violation',
                     jsonb_build_object('caller', v_caller, 'caller_tier', v_caller_tier,
                                        'target_tier', p.trust_tier));
            RETURN QUERY SELECT NULL::text, 'tier_violation'::text;
            RETURN;
        END IF;
    END IF;

    -- Clamped here rather than trusted from the argument, for the same reason
    -- 0002 recomputes an attempt's visibility deadline: a caller must not be
    -- able to widen its own lifetime. One hour, because revocation cannot reach
    -- a partitioned host — `pg_terminate_backend` needs a connection to
    -- terminate — so the effective revocation latency for such a host is
    -- bounded by this TTL and by nothing else.
    v_ttl := least(coalesce(p_ttl, interval '15 minutes'), interval '1 hour');

    -- PostgreSQL stores exactly ONE verifier per role, so leaving the previous
    -- credential live would make this table disagree with `pg_authid`.
    FOR r IN SELECT credential_id FROM principal_credential
              WHERE principal_id = p_principal_id AND revoked_at IS NULL LOOP
        UPDATE principal_credential
           SET revoked_at = now(), revoke_reason = 'rotated', updated_at = now()
         WHERE credential_id = r.credential_id;
        PERFORM _principal_audit(p_principal_id, 'revoke', 'granted',
                                 r.credential_id, 'rotated', NULL);
    END LOOP;

    v_id := _identity_new_id('cred_');
    INSERT INTO principal_credential (
        credential_id, principal_id, secret_hash,
        issued_at, expires_at, issued_from_addr, created_at, updated_at)
    VALUES (v_id, p_principal_id, p_secret_hash,
            now(), now() + v_ttl, inet_client_addr(), now(), now());

    PERFORM identity_set_role_secret(p.db_role, p_scram_verifier, now() + v_ttl);

    PERFORM _principal_audit(p_principal_id, 'issue', 'granted', v_id, NULL,
                             jsonb_build_object('ttl', v_ttl::text));
    RETURN QUERY SELECT v_id, NULL::text;
END
$$;

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

DROP FUNCTION IF EXISTS enroll_self_grace();
