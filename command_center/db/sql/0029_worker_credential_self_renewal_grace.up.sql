-- 0029: an expired WORKER credential may still renew itself for a bounded
-- grace (VOYN-W0-AICC-CREDENTIAL-ROTATION-DEADLOCKS-AFTER-EXPIRY, remediation).
--
-- The worker rotator authenticates to the database with the very credential
-- it is renewing. Live 2026-09-15 three rotations were refused for a lane
-- readiness reason unrelated to the credential, the credential's one-hour
-- `expires_at` passed at 05:22 UTC, and from then on the rotator could not
-- recover on its own: `identity_current_credential` -- which the rotator asks
-- BEFORE it rotates -- refused it 164 times with `credential_expired`, the
-- lanes' pooled sessions died at 06:17, and the host stayed dead until an
-- operator widened the ledger row and the role by hand at 10:33.
--
-- What changes, and what does not:
--   * `identity_assert(p_secret, p_expired_grace)` -- the 0003 assert with one
--     added branch: an EXPIRED credential of a `worker_host` principal passes
--     when the caller names a grace, `expires_at + grace` has not passed
--     (exclusive), and the credential is already BOUND to an address -- one
--     that was never used has no address to be restricted to and is refused.
--     The grace is clamped to `enroll_self_grace()` (1 hour, the TTL itself),
--     so no caller can widen it. Every other guard -- role match, revocation,
--     principal state, expected CIDR, bound address -- applies to the graced
--     pass exactly as to a live one, and for a worker host the address guards
--     REFUSE, so an expired worker secret can only ever RENEW from the address
--     the credential is bound to. Operator and control-plane credentials get
--     no grace: for them expiry is expiry, as in 0003, because their address
--     guards only audit. A graced pass is audited as
--     `assert/granted/expired_grace`, never as a denial, and the VERDICT's
--     `reason` stays NULL on success: on the verdict, `reason IS NOT NULL`
--     still means refused. (0003's audit rows keep their vocabulary,
--     including the `rejected` row it writes for a tolerated operator address
--     mismatch; that is a 0003 trait this file restores unchanged.) The
--     one-argument `identity_assert` keeps its 0003 effect (grace 0).
--   * `identity_current_credential(p_secret, p_for_renewal)` -- the 0013 query
--     the rotator runs first, with the grace applied when asked for renewal,
--     plus `renewable_until`: the server-authoritative instant after which
--     `enroll_rotate_self` will refuse this credential. That is the deadline
--     the rotator bounds its own attempt by; lanes still stop being able to
--     WORK at `current_expires_at`.
--   * `identity_issue_db_credential` -- a WORKER role may LOG IN until the
--     ledger expiry plus the grace. 0003 set role validity and ledger expiry to
--     the same instant, which made any ledger-side grace unreachable over a
--     fresh connection (the pooled sessions were the only thing keeping the
--     lanes up). Every other kind keeps role validity equal to the ledger
--     expiry. Roles of live worker credentials minted before this migration
--     are widened the same way -- only ever widened, one row per role, from
--     the latest credential -- so the fix reaches a fleet at its next
--     rotation; the down narrows them back to the ledger.
--   * `enroll_rotate_self` -- asks with the grace and records `expired_grace`
--     on its `rotate/granted` event. THAT row is the "host renewed on an
--     expired secret" signal: it is written only when a rotation happened.
--     The `assert/granted/expired_grace` row is written by any graced pass,
--     including the read-only renewal query, and says only that the secret
--     was presented after expiry.
--
-- The property, stated honestly. The grace extends the WORKER ROLE's password
-- validity by `grace` past the ledger expiry. Within that window the secret
-- can still log in and do whatever the worker role can do without a ledger
-- verdict: `queue_claim` and the other queue functions do not call
-- `identity_assert`, and the role has direct INSERT/UPDATE on run, completion
-- and report rows (see `command_center.db.roles`). What it cannot do: pass
-- `identity_assert` (any identity-gated call), or RENEW from any address but
-- the bound one, or renew at all once unbound, revoked, suspended or past
-- `expires_at + grace`. So the exposure delta of a stolen worker secret is:
-- the same capabilities it had during its TTL, for one more hour, from any
-- address pg_hba admits for the role (on the control host that is loopback
-- only, i.e. through a worker's own SSH tunnel: the secret alone opens
-- nothing). Revocation still curtails it at once:
-- `identity_revoke_principal` disables the role, and a rotation replaces the
-- verifier so the superseded secret cannot log in whatever the validity
-- says. That hour is the price of self-recovery; narrowing it further means a
-- renewal-only login role per worker (a follow-up, not this migration).
-- A graced renewal is NOT consumed: a host that stalls again may renew under
-- grace again. The bound is time (each credential's `expires_at + grace`),
-- not a count of recoveries; a stall longer than TTL + grace still needs an
-- operator, and that is the stall an operator should be paged for. This
-- deliberately claims no "one hop": once any secret, graced or live, has
-- rotated, its successor is an ordinary live credential -- exactly what a
-- stolen LIVE secret already yields today. What the grace recovers is a fleet
-- whose lanes are still ready on established sessions (PostgreSQL checks
-- validity at authentication only); lanes that have already lost their
-- sessions cannot become ready on an expired secret, and rotation still
-- requires ready lanes.
-- One knob. Equal to the credential TTL: enough to absorb a missed rotation
-- tick plus the retry and circuit cadence, and no wider than the exposure the
-- TTL already accepts.
CREATE FUNCTION enroll_self_grace() RETURNS interval
    LANGUAGE sql IMMUTABLE AS $$ SELECT interval '1 hour' $$;

CREATE FUNCTION identity_assert(p_secret text, p_expired_grace interval)
    RETURNS identity_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    c principal_credential%ROWTYPE;
    p principal%ROWTYPE;
    v identity_verdict;
    v_hash   text := encode(sha256(convert_to(p_secret, 'UTF8')), 'hex');
    -- Clamped here, not trusted from the argument: a caller must not be able
    -- to widen the window past the one knob.
    v_grace  interval := least(coalesce(p_expired_grace, interval '0'), enroll_self_grace());
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

    -- Revocation is checked BEFORE expiry, so a revoked credential can never
    -- reach the graced branch below: revoked is revoked, expired or not.
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
        -- Expired. A WORKER credential whose caller names a grace still passes
        -- while `expires_at + grace` has not elapsed, PROVIDED it is already
        -- bound to an address: the bound-address guard below is what confines
        -- a graced renewal to the host that earned it, and a credential that
        -- was never used has nothing to be confined to. Every condition here
        -- is written fail-closed (a NULL kind is not a worker host). The
        -- guards that follow (principal state, expected CIDR, bound address)
        -- still apply, and for a worker host the address guards refuse.
        -- Every other kind, and every caller that names no grace, is refused
        -- here as in 0003.
        IF v_grace <= interval '0'
           OR p.kind IS DISTINCT FROM 'worker_host'
           OR c.bound_addr IS NULL
           OR c.expires_at + v_grace <= now() THEN
            PERFORM _principal_audit(c.principal_id, 'assert', 'rejected', c.credential_id,
                     'credential_expired', jsonb_build_object('expires_at', c.expires_at,
                                                              'grace', v_grace::text,
                                                              'bound', c.bound_addr IS NOT NULL));
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

    -- A graced pass is a decision worth a row of its own: it is the signal
    -- that a host is renewing on an expired secret.
    IF v_graced THEN
        PERFORM _principal_audit(c.principal_id, 'assert', 'granted', c.credential_id,
                 'expired_grace', jsonb_build_object('expires_at', c.expires_at,
                                                     'grace', v_grace::text));
    END IF;
    v.ok     := true;
    v.reason := NULL;
    RETURN v;
END
$$;

-- The unchanged contract for everyone else: expiry is expiry.
CREATE OR REPLACE FUNCTION identity_assert(p_secret text)
    RETURNS identity_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    RETURN identity_assert(p_secret, interval '0');
END
$$;

-- The rotator's first question, answered for an expired worker credential too
-- when it asks for renewal. `renewable_until` is the first instant at which
-- `enroll_rotate_self` refuses the credential (exclusive): expiry plus the
-- grace for a worker host, the expiry itself for every other kind. Asking is
-- a read gated by proof of possession -- it mints nothing -- and like the 0013
-- query it goes through `identity_assert`, so it records the use
-- (`last_used_at`) exactly as the one-argument form does. `server_now` is
-- `clock_timestamp()` as in 0013: at or after the transaction `now()` the
-- deadlines were judged against, so a remaining lifetime computed from it is
-- never longer than the one the ledger will honour.
CREATE FUNCTION identity_current_credential(p_secret text, p_for_renewal boolean)
    RETURNS TABLE (
        current_expires_at timestamptz,
        server_now timestamptz,
        refuse_reason text,
        renewable_until timestamptz
    )
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    v identity_verdict;
    v_expires timestamptz;
    v_renewable timestamptz;
    v_now timestamptz;
BEGIN
    v := identity_assert(p_secret,
                         CASE WHEN p_for_renewal THEN enroll_self_grace() ELSE interval '0' END);
    v_now := clock_timestamp();
    IF NOT v.ok THEN
        RETURN QUERY SELECT NULL::timestamptz, v_now, v.reason, NULL::timestamptz;
        RETURN;
    END IF;

    SELECT c.expires_at,
           c.expires_at + CASE WHEN p.kind IS NOT DISTINCT FROM 'worker_host'
                                THEN enroll_self_grace() ELSE interval '0' END
      INTO v_expires, v_renewable
      FROM principal_credential c
      JOIN principal p ON p.principal_id = c.principal_id
     WHERE c.credential_id = v.credential_id
       AND c.principal_id = v.principal_id
       AND c.revoked_at IS NULL;
    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::timestamptz, v_now,
                            'credential_not_current'::text, NULL::timestamptz;
        RETURN;
    END IF;
    RETURN QUERY SELECT v_expires, v_now, NULL::text, v_renewable;
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
    v_valid_until timestamptz;
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
        -- Fail closed: a caller that has an id but no resolvable tier is not
        -- the provisioning path (that one has no id at all) and gets no
        -- credential.
        IF v_caller_tier IS NULL OR v_caller_tier >= p.trust_tier THEN
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
    -- bounded by this TTL. For a worker host the role stays able to log in
    -- for a further `enroll_self_grace()` past it (0029, see the header for
    -- exactly what that window allows); the renewal itself is refused from
    -- any address but the bound one. `enroll_rotate_self` asks for the full
    -- hour on purpose: the rotator's protocol budget
    -- (`SELF_CREDENTIAL_TTL_SECONDS` in `command_center.ops.credential_rotation`)
    -- is sized to a fixed one-hour credential.
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

    -- Role validity: the ledger expiry, plus the self-renewal grace for the one
    -- kind that rotates itself with the credential being replaced. Widening it
    -- for every kind would widen the login surface of operator and
    -- control-plane roles for a window nothing of theirs can use.
    v_valid_until := now() + v_ttl
                   + CASE WHEN p.kind IS NOT DISTINCT FROM 'worker_host'
                          THEN enroll_self_grace() ELSE interval '0' END;
    PERFORM identity_set_role_secret(p.db_role, p_scram_verifier, v_valid_until);

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
    v        identity_verdict;
    v_graced boolean;
    v_cred   text;
    v_why    text;
    v_e      timestamptz;
BEGIN
    -- Proof of possession IS the authorisation, and it runs the full gate, so a
    -- revoked, suspended or out-of-CIDR credential cannot rotate itself back to
    -- life; an EXPIRED worker credential may, inside the grace (0029). Note
    -- that this consumes the verdict rather than raising on it: raising here
    -- would abort the transaction containing the denial audit
    -- `identity_assert()` just wrote.
    v := identity_assert(p_current_secret, enroll_self_grace());
    IF NOT v.ok THEN
        RETURN QUERY SELECT NULL::timestamptz, v.reason;
        RETURN;
    END IF;
    -- Whether the pass above was graced, read from the same row against the
    -- same transaction `now()` the assert used, so the two cannot disagree.
    SELECT c.expires_at <= now() INTO v_graced
      FROM principal_credential c WHERE c.credential_id = v.credential_id;
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
                                'expired_grace', coalesce(v_graced, false)));
    RETURN QUERY SELECT v_e, NULL::text;
END
$$;

-- Grants mirror 0013: the rotator (worker role) may ask about its own proved
-- credential and nothing else; the graced assert and the knob are reached only
-- through the SECURITY DEFINER functions above.
REVOKE ALL ON FUNCTION enroll_self_grace() FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_assert(text, interval) FROM PUBLIC;
REVOKE ALL ON FUNCTION identity_current_credential(text, boolean) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION identity_current_credential(text, boolean) TO aicc_worker;

-- A live worker credential minted before this migration gets the same renewal
-- window as one minted after it, so the fix reaches a fleet at its NEXT
-- rotation rather than one rotation later. Three rules keep this from ever
-- doing what the incident did: exactly one row per role -- the LATEST
-- unrevoked credential, whatever older rows the ledger still holds; only
-- active worker hosts; and only ever WIDENING -- a role whose validity is
-- already later (an operator's hand extension, `NULL` = never expires) is
-- left alone, and a stale ledger row can never pull a role into the past.
DO $$
DECLARE
    r record;
    v_new timestamptz;
BEGIN
    FOR r IN SELECT DISTINCT ON (p.db_role) p.db_role, c.expires_at, g.rolvaliduntil
               FROM principal_credential c
               JOIN principal p ON p.principal_id = c.principal_id
               JOIN pg_roles g ON g.rolname = p.db_role
              WHERE c.revoked_at IS NULL
                AND p.kind = 'worker_host'
                AND p.state = 'active'
              ORDER BY p.db_role, c.expires_at DESC
    LOOP
        v_new := r.expires_at + enroll_self_grace();
        IF r.rolvaliduntil IS NOT NULL AND r.rolvaliduntil < v_new THEN
            EXECUTE format('ALTER ROLE %I VALID UNTIL %L', r.db_role, v_new::text);
        END IF;
    END LOOP;
END
$$;
