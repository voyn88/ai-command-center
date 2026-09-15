-- 0029: an expired, unrevoked credential may still renew ITSELF, once, for a
-- short grace (VOYN-W0-AICC-CREDENTIAL-ROTATION-DEADLOCKS-AFTER-EXPIRY).
--
-- The worker rotator authenticates to `enroll_rotate_self` with the very
-- credential it is renewing. Once that credential's `expires_at` passed --
-- live 2026-09-15 05:22 UTC, after three rotations were refused for a
-- readiness reason unrelated to the credential -- every later attempt failed
-- `credential_expired`, and the host could never recover on its own: 164
-- refusals, the lanes dead from 06:17 until an operator extended the row.
--
-- Three pieces, one knob (`enroll_self_grace()`, 2 x the 1-hour TTL):
--   * `identity_issue_db_credential` sets the ROLE's login validity to the
--     ledger expiry plus the grace, so a fresh connection with an expired
--     credential still reaches the database at all (0003 set both to the
--     same instant, which made any SQL-side grace unreachable in production
--     -- the pooled connections were the only thing keeping the lanes up).
--   * `identity_assert(p_secret, p_expired_grace)` is the 0003 assert with
--     one changed predicate: an expired credential passes only when the
--     caller names a grace, the grace has not elapsed, and the credential was
--     not itself issued under grace (one hop, no chaining). Role match,
--     revocation, principal state, expected CIDR and bound address apply to
--     the graced path unchanged. A graced pass is audited as
--     `assert/granted/expired_grace`, never as a denial. The one-argument
--     `identity_assert` becomes a wrapper with grace 0: everywhere else,
--     expiry means what it meant.
--   * `enroll_rotate_self` asks with the grace and records `expired_grace`
--     on its `rotate/granted` event.
CREATE FUNCTION enroll_self_grace() RETURNS interval
    LANGUAGE sql IMMUTABLE AS $$ SELECT interval '2 hours' $$;

CREATE FUNCTION identity_assert(p_secret text, p_expired_grace interval)
    RETURNS identity_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    c principal_credential%ROWTYPE;
    p principal%ROWTYPE;
    v identity_verdict;
    v_hash text := encode(sha256(convert_to(p_secret, 'UTF8')), 'hex');
    v_graced boolean := false;
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
        -- Expired. A caller that names a grace (only `enroll_rotate_self`,
        -- with `enroll_self_grace()`) may still pass -- once: a credential
        -- that was itself issued under grace gets no second hop, so a
        -- stolen expired secret cannot chain successors indefinitely. Every
        -- other guard below (role, revocation, principal state, CIDR, bound
        -- address) still applies to the graced path exactly as to a live one.
        IF p_expired_grace <= interval '0'
           OR c.expires_at + p_expired_grace <= now()
           OR EXISTS (SELECT 1 FROM principal_event e
                       WHERE e.credential_id = c.credential_id
                         AND e.event_type = 'rotate' AND e.outcome = 'granted'
                         AND (e.metadata_json ->> 'expired_grace') = 'true') THEN
            PERFORM _principal_audit(c.principal_id, 'assert', 'rejected', c.credential_id,
                     'credential_expired', jsonb_build_object('expires_at', c.expires_at));
            v.reason := 'credential_expired';
            RETURN v;
        END IF;
        v_graced := true;
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

    IF v_graced THEN
        PERFORM _principal_audit(c.principal_id, 'assert', 'granted', c.credential_id,
                 'expired_grace', jsonb_build_object('expires_at', c.expires_at,
                                                     'grace', p_expired_grace::text));
    END IF;
    v.ok     := true;
    v.reason := CASE WHEN v_graced THEN 'expired_grace' ELSE NULL END;
    RETURN v;
END
$$;

-- The unchanged contract: expiry is expiry.
CREATE OR REPLACE FUNCTION identity_assert(p_secret text)
    RETURNS identity_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    RETURN identity_assert(p_secret, interval '0');
END
$$;

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

    -- The role may still LOG IN for the self-renewal grace after the ledger
    -- expiry; the ledger (identity_assert) is what refuses work in between.
    PERFORM identity_set_role_secret(p.db_role, p_scram_verifier, now() + v_ttl + enroll_self_grace());

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
    v := identity_assert(p_current_secret, enroll_self_grace());
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
             jsonb_build_object('replaced', v.credential_id,
                                'expired_grace', coalesce(v.reason = 'expired_grace', false)));
    RETURN QUERY SELECT v_e, NULL::text;
END
$$;
