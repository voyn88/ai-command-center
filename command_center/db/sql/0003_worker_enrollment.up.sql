-- Worker host enrolment: how an execution host is admitted (VOYN-W0-AICC-SRV-03).
--
-- WHAT THIS CLOSES. 0002 made a claim belong to the PostgreSQL role the server
-- itself authenticated, and `render_worker_host_role()` gives each execution
-- host a LOGIN role of its own. Both stop at the same wall: the host's *first*
-- secret is still hand-delivered, so "one role per host" is an operator
-- convention rather than a protocol, and nothing records who decided a host may
-- exist, when, or that it happened exactly once.
--
-- THE BOOTSTRAP PROBLEM, STATED HONESTLY. Trust cannot be created from nothing.
-- A host that has never been enrolled has, by construction, no credential;
-- something must be the first secret, and here a human pastes one short-lived
-- token, once, per host. This migration does not claim to have eliminated that.
-- It claims to have replaced an unbounded, shared, silent secret with a
-- bounded, single-use, pre-bound, LOUD one. Four properties, not a rename:
--
--   1. SINGLE-USE. Redemption is a state transition under a row lock, and
--      `CHECK (use_count BETWEEN 0 AND 1)` makes "used twice" unrepresentable
--      rather than merely refused by a code path. That is what converts theft
--      from silent to loud: if a thief redeems first, the legitimate host's
--      redemption FAILS, and that failure is the alarm. Under a shared password
--      a thief's success changes nothing observable.
--   2. SHORT-LIVED. Minutes, clamped here from the server's clock, never from
--      the caller's argument — the same reason 0002 recomputes an attempt's
--      deadline instead of trusting one.
--   3. NOT A DATABASE LOGIN. A ticket cannot connect to PostgreSQL at all. It
--      is redeemable through exactly one SECURITY DEFINER function and can
--      cause exactly one thing to happen. The shared password IS a login
--      carrying the whole `aicc_worker` grant set.
--   4. PRE-BOUND. The ticket names the principal it may produce before it is
--      handed out, so a stolen ticket cannot enrol a host of the thief's
--      choosing; it can only impersonate the one host already approved.
--
-- WHAT THE HOST KEEPS TO ITSELF. The host generates its own 256-bit secret
-- locally and sends two derivatives: `sha256(secret)`, which `identity_assert()`
-- matches, and a CLIENT-COMPUTED SCRAM-SHA-256 verifier, which PostgreSQL
-- authenticates against. Neither the control plane nor the database ever sees
-- the plaintext, so a compromised control plane can mint NEW credentials — it
-- always could — but cannot LEARN an existing host's secret.
--
-- REFUSALS ARE RETURN VALUES, NOT EXCEPTIONS, and this is the one rule in the
-- file that was measured rather than reasoned. A denial written to
-- `principal_event` and then followed by `RAISE` leaves NO row: the exception
-- aborts the transaction that contained the audit. The single-use refusal IS
-- the theft alarm, and an alarm that rolls itself back is not an alarm. So
-- every refusal path below returns a reason. The trade-off, stated: an
-- exception cannot be ignored and a return value can — which is safe here only
-- because every refusal also returns NULL for the role, the credential and the
-- expiry, so a caller that ignores the reason has nothing to hand the host and
-- fails closed anyway.
--
-- Conventions follow 0001 and 0002: singular snake_case tables, `timestamptz`
-- (never ISO text), `jsonb` for `*_json`, `bigint GENERATED ALWAYS AS IDENTITY`
-- plus a per-parent `seq` for event tables, explicit `created_at`/`updated_at`
-- with no DEFAULT, `idx_<table>_<column>` indexes after the tables. CHECK
-- constraints are used, as in 0002 and unlike 0001: a ticket with a state
-- nobody defined is not a data-quality nuisance, it is an authorisation
-- decision made against an undefined value.


-- ---------------------------------------------------------------------------
-- principal
-- ---------------------------------------------------------------------------
-- The identity an action is attributed to. Three kinds, and every one of them
-- connects to PostgreSQL, so `db_role` is NOT NULL: this column is the entire
-- binding between "who PostgreSQL says you are" and "who the application says
-- you are". `identity_assert()` compares it against `session_user`, which no
-- client statement can change — that is why a stolen secret cannot be replayed
-- from another principal's connection.
CREATE TABLE principal (
    -- Stable and human-meaningful: 'control-plane', 'worker:srv01a',
    -- 'operator:release-manager'.
    principal_id  text PRIMARY KEY,

    -- 'operator'      — a human, acting through psql or an operator tool.
    -- 'control_plane' — the API and dispatcher process (`aicc_app`).
    -- 'worker_host'   — an execution host. Least trusted: it runs agent
    --                   processes against untrusted repository content.
    kind          text        NOT NULL,

    db_role       text        NOT NULL UNIQUE,
    display_name  text        NOT NULL,

    -- Canonical hostname of a worker host.
    host          text,

    -- The network an operator declared this host will connect from, in
    -- advance. Enforced for worker hosts by `identity_assert()`. Spoofable at
    -- the network layer, so it is a signal that raises the cost of using a
    -- stolen secret elsewhere and never the only control.
    expected_cidr inet,

    -- Self-referencing: the first principals are bootstrapped with NULL, and
    -- everything afterwards names the principal that admitted it.
    enrolled_by   text REFERENCES principal(principal_id),

    -- 'active' | 'suspended' | 'retired'. Re-read on every `identity_assert()`,
    -- so suspension bites on the next statement of an already-open connection.
    --
    -- This column IS the block-list. No `blocked_host` table is introduced: a
    -- second place recording "this host may not come back" is a second place
    -- that can disagree with the first.
    state         text        NOT NULL,

    -- The act-as rule as a number rather than a table of string pairs: a
    -- principal may only admit or issue for a STRICTLY higher tier value (i.e.
    -- strictly less trusted). Derived from `kind` by the CHECK below rather
    -- than supplied, because a worker row with `trust_tier = 0` would be an
    -- escalation written as a typo.
    trust_tier    integer     NOT NULL,

    metadata_json jsonb,
    created_at    timestamptz NOT NULL,
    updated_at    timestamptz NOT NULL,

    CONSTRAINT principal_kind_valid
        CHECK (kind IN ('operator', 'control_plane', 'worker_host')),
    CONSTRAINT principal_state_valid
        CHECK (state IN ('active', 'suspended', 'retired')),
    CONSTRAINT principal_tier_matches_kind
        CHECK (trust_tier = CASE kind WHEN 'operator'      THEN 0
                                      WHEN 'control_plane' THEN 1
                                      ELSE 2 END),
    CONSTRAINT principal_worker_has_host
        CHECK (kind <> 'worker_host' OR host IS NOT NULL)
);


-- ---------------------------------------------------------------------------
-- principal_credential
-- ---------------------------------------------------------------------------
-- One live database secret per host. Deliberately narrow: this slice issues
-- exactly one kind of credential, so there is no `kind` column to branch on and
-- no unreachable branch pretending otherwise. Scoped session tokens are a
-- separate identity concern and are not invented here in advance of a caller.
CREATE TABLE principal_credential (
    credential_id    text PRIMARY KEY,
    principal_id     text        NOT NULL REFERENCES principal(principal_id),

    -- SHA-256 hex of a 256-bit secret generated by the HOST; the preimage never
    -- reaches the database. PostgreSQL separately holds a SCRAM-SHA-256
    -- verifier in `pg_authid`, computed client-side and passed as a verifier
    -- string, so the plaintext appears in no statement, no `log_statement`
    -- output and no `pg_stat_activity.query`.
    --
    -- SHA-256 rather than a slow KDF, on purpose: these are 256-bit CSPRNG
    -- values with no dictionary to attack, and `identity_assert()` runs per
    -- privileged statement. A 600k-iteration KDF on that path would be a
    -- self-inflicted denial of service with no security gain over a
    -- high-entropy preimage.
    secret_hash      text        NOT NULL,

    issued_at        timestamptz NOT NULL,
    -- Always set. There is no non-expiring credential in this model; the
    -- issuing function clamps the TTL to a policy ceiling.
    expires_at       timestamptz NOT NULL,
    last_used_at     timestamptz,
    issued_from_addr inet,
    -- Address observed on FIRST use. Later use from elsewhere is audited, and
    -- for worker hosts refused.
    bound_addr       inet,

    revoked_at       timestamptz,
    -- 'incident' | 'expired' | 'rotated' | 'principal_retired'
    revoke_reason    text,
    revoked_by       text REFERENCES principal(principal_id),

    created_at       timestamptz NOT NULL,
    updated_at       timestamptz NOT NULL,

    CONSTRAINT principal_credential_revocation_complete
        CHECK ((revoked_at IS NULL) = (revoke_reason IS NULL)),
    CONSTRAINT principal_credential_ttl_positive
        CHECK (expires_at > issued_at)
);

-- A secret must resolve to at most one credential. Without this a duplicated
-- hash would make `identity_assert()` ambiguous, and "ambiguous" could only be
-- resolved by picking one — that is, by guessing.
CREATE UNIQUE INDEX idx_principal_credential_secret_hash
    ON principal_credential(secret_hash);
CREATE INDEX idx_principal_credential_principal_id
    ON principal_credential(principal_id);
CREATE INDEX idx_principal_credential_expires_at
    ON principal_credential(expires_at);


-- ---------------------------------------------------------------------------
-- principal_event
-- ---------------------------------------------------------------------------
-- Append-only audit of every identity decision, INCLUDING refusals — those are
-- the security-relevant half. Same shape as `work_event` and `run_event`.
--
-- `principal_id` is nullable, and `seq` with it: a presented secret matching no
-- credential has no principal to attribute the denial to, and inventing a
-- sentinel principal would put a fake row in the identity table to satisfy a
-- foreign key. Gap-free per-principal `seq` therefore holds for every known
-- principal; unattributable denials are ordered by `id` and `created_at`.
CREATE TABLE principal_event (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    principal_id  text REFERENCES principal(principal_id),
    seq           integer,

    -- 'bootstrap' | 'enroll_worker' | 'enroll_mint' | 'enroll' | 'enroll_revoke'
    -- | 'issue' | 'rotate' | 'revoke' | 'assert' | 'act_as'
    event_type    text        NOT NULL,
    outcome       text        NOT NULL,
    -- NOT a foreign key: the audit must outlive a credential purge.
    credential_id text,

    -- The authenticated PostgreSQL identity that made the call, taken from
    -- `session_user`, so a caller cannot write someone else's name here.
    actor_db_role text,
    client_addr   inet,

    reason        text,
    metadata_json jsonb,
    created_at    timestamptz NOT NULL,

    CONSTRAINT principal_event_outcome_valid
        CHECK (outcome IN ('granted', 'rejected')),
    CONSTRAINT principal_event_seq_matches_principal
        CHECK ((principal_id IS NULL) = (seq IS NULL))
);

CREATE UNIQUE INDEX idx_principal_event_principal_seq
    ON principal_event(principal_id, seq);
CREATE INDEX idx_principal_event_created_at ON principal_event(created_at);


-- ---------------------------------------------------------------------------
-- enrollment_ticket
-- ---------------------------------------------------------------------------
-- Not a credential: a one-time capability to OBTAIN one.
CREATE TABLE enrollment_ticket (
    ticket_id              text PRIMARY KEY,

    -- SHA-256 hex of a 256-bit secret generated by the minter, exactly as for
    -- `principal_credential`. The preimage never reaches the database.
    ticket_hash            text        NOT NULL,

    -- PRE-BINDING. The ticket names its target before it is handed out.
    intended_principal_id  text        NOT NULL,
    intended_host          text        NOT NULL,

    -- Written onto `principal.expected_cidr` at redemption.
    expected_cidr          inet,

    -- 'enroll'    — the principal must NOT exist yet.
    -- 're_enroll' — it must exist: a rebuilt host, a hardware change, or a host
    --               an operator is readmitting after revocation.
    purpose                text        NOT NULL,

    -- 'issued' | 'redeemed' | 'revoked' | 'expired'
    state                  text        NOT NULL,

    issued_by              text        NOT NULL REFERENCES principal(principal_id),
    issued_at              timestamptz NOT NULL,
    expires_at             timestamptz NOT NULL,

    redeemed_at            timestamptz,
    redeemed_from_addr     inet,
    -- NOT a foreign key, same rule as `principal_event.credential_id`.
    redeemed_credential_id text,

    -- Belt to the state machine's braces: the CHECK below makes a second use
    -- unrepresentable rather than merely refused by a code path.
    use_count              integer     NOT NULL,

    revoked_at             timestamptz,
    revoke_reason          text,

    metadata_json          jsonb,
    created_at             timestamptz NOT NULL,
    updated_at             timestamptz NOT NULL,

    CONSTRAINT enrollment_ticket_purpose_valid
        CHECK (purpose IN ('enroll', 're_enroll')),
    CONSTRAINT enrollment_ticket_state_valid
        CHECK (state IN ('issued', 'redeemed', 'revoked', 'expired')),
    CONSTRAINT enrollment_ticket_single_use
        CHECK (use_count BETWEEN 0 AND 1),
    CONSTRAINT enrollment_ticket_redemption_complete
        CHECK ((state = 'redeemed') = (redeemed_at IS NOT NULL)),
    CONSTRAINT enrollment_ticket_redeemed_is_used
        CHECK (state <> 'redeemed' OR use_count = 1),
    CONSTRAINT enrollment_ticket_ttl_positive
        CHECK (expires_at > issued_at)
);

CREATE UNIQUE INDEX idx_enrollment_ticket_hash ON enrollment_ticket(ticket_hash);
CREATE INDEX idx_enrollment_ticket_intended
    ON enrollment_ticket(intended_principal_id);
CREATE INDEX idx_enrollment_ticket_expires_at ON enrollment_ticket(expires_at);


-- ---------------------------------------------------------------------------
-- worker_host_fingerprint
-- ---------------------------------------------------------------------------
-- EVIDENCE, NOT AUTHENTICATION. Every field in the descriptor is a string an
-- unattested host typed about itself. Recording it buys exactly one thing —
-- CHANGE DETECTION — and the table is named and granted so that nobody later
-- mistakes it for an authenticator. There is no TPM quote, no Secure Enclave
-- attestation and no cloud instance identity document behind any of it; a host
-- that lies about its machine id at first enrolment is believed.
--
-- History rather than a column on `principal`, for one specific reason: the
-- clone signal is "this fingerprint has been seen under a DIFFERENT principal",
-- which needs an indexed equality lookup. The rejected alternative was scanning
-- `principal_event.metadata_json` — a jsonb path scan over an ever-growing
-- audit table, promoted to a security decision.
CREATE TABLE worker_host_fingerprint (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    principal_id       text        NOT NULL REFERENCES principal(principal_id),
    seq                integer     NOT NULL,

    fingerprint_hash   text        NOT NULL,
    descriptor_json    jsonb       NOT NULL,

    -- NULL on the first enrolment; afterwards what changed and why it was
    -- accepted: 'rebuild' | 'fingerprint_changed' | 'readmitted'.
    change_reason      text,

    observed_at        timestamptz NOT NULL,
    observed_from_addr inet,
    created_at         timestamptz NOT NULL,

    CONSTRAINT worker_host_fingerprint_seq_positive CHECK (seq >= 1)
);

CREATE UNIQUE INDEX idx_worker_host_fingerprint_principal_seq
    ON worker_host_fingerprint(principal_id, seq);
-- The clone lookup. NOT unique: a rebuilt host legitimately re-presents the
-- same fingerprint under the SAME principal, and new hardware legitimately
-- presents a new one. The refusable case is the same fingerprint under a
-- DIFFERENT principal, which is a predicate, not a constraint.
CREATE INDEX idx_worker_host_fingerprint_hash
    ON worker_host_fingerprint(fingerprint_hash);


-- ---------------------------------------------------------------------------
-- Redacted views
-- ---------------------------------------------------------------------------
-- `principal_credential` and `enrollment_ticket` are granted to NO role, and
-- these views — which omit `secret_hash` and `ticket_hash` — are the only read
-- paths. "The control plane cannot read a pending ticket's secret" is then a
-- property of the grant graph rather than of a WHERE clause someone remembered
-- to write. Same treatment `work_attempt` gets in 0002 for `claim_token_hash`.
CREATE VIEW principal_credential_public AS
    SELECT credential_id, principal_id, issued_at, expires_at, last_used_at,
           revoked_at, revoke_reason, revoked_by, created_at, updated_at
      FROM principal_credential;

CREATE VIEW enrollment_ticket_public AS
    SELECT ticket_id, intended_principal_id, intended_host, expected_cidr,
           purpose, state, issued_by, issued_at, expires_at, redeemed_at,
           redeemed_from_addr, redeemed_credential_id, use_count,
           revoked_at, revoke_reason, metadata_json, created_at, updated_at
      FROM enrollment_ticket;


-- ---------------------------------------------------------------------------
-- current_principal()
-- ---------------------------------------------------------------------------
-- `session_user`, NOT `current_user`: inside a SECURITY DEFINER function
-- `current_user` is the function OWNER, so using it here would make every
-- caller look like the migrator — the exact inversion of the control. And
-- `session_user` is unaffected by SET ROLE, so a caller cannot widen or narrow
-- its own identity mid-session.
CREATE FUNCTION current_principal() RETURNS text
    LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT principal_id FROM principal
     WHERE db_role = session_user AND state = 'active'
$$;

CREATE FUNCTION _identity_new_id(p_prefix text) RETURNS text
    LANGUAGE sql VOLATILE SET search_path = pg_catalog, public AS $$
    -- These are identifiers and carry no entropy requirement, so
    -- `gen_random_uuid()` is right and core: no pgcrypto dependency, which the
    -- rest of the schema does not have either. Credential material is generated
    -- by the holder and never by this function.
    SELECT p_prefix || replace(gen_random_uuid()::text, '-', '')
$$;


-- ---------------------------------------------------------------------------
-- _principal_audit(): the one place events are written
-- ---------------------------------------------------------------------------
-- Takes the principal row lock before computing max(seq)+1, exactly as 0002's
-- `_queue_audit` does, so the sequence is collision-free without a retry loop.
--
-- It also `RAISE LOG`s refusals, which answers an otherwise silent hole: a
-- denial audited inside a transaction the CALLER then rolls back disappears
-- with it. A log line is not transactional and survives. It is a diagnostic,
-- not a substitute — a server log line is not a queryable security record,
-- which is why the refusal paths return instead of raising.
CREATE FUNCTION _principal_audit(
    p_principal_id  text,
    p_event_type    text,
    p_outcome       text,
    p_credential_id text,
    p_reason        text,
    p_metadata      jsonb
) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    v_seq integer;
BEGIN
    IF p_principal_id IS NOT NULL THEN
        PERFORM 1 FROM principal WHERE principal_id = p_principal_id FOR UPDATE;
        SELECT coalesce(max(seq), 0) + 1 INTO v_seq
          FROM principal_event WHERE principal_id = p_principal_id;
    END IF;

    INSERT INTO principal_event (
        principal_id, seq, event_type, outcome, credential_id,
        actor_db_role, client_addr, reason, metadata_json, created_at)
    VALUES (
        p_principal_id, v_seq, p_event_type, p_outcome, p_credential_id,
        session_user, inet_client_addr(), p_reason, p_metadata, now());

    IF p_outcome = 'rejected' THEN
        RAISE LOG 'aicc identity denial: principal=% event=% reason=% db_role=%',
            coalesce(p_principal_id, '<unknown>'), p_event_type,
            coalesce(p_reason, '<none>'), session_user;
    END IF;
END
$$;


-- ---------------------------------------------------------------------------
-- identity_assert(): the statement-level gate
-- ---------------------------------------------------------------------------
-- Every privileged operation calls this with the presented secret, on every
-- statement, with no caching. That is what makes revocation take effect on an
-- already-open connection without having to terminate it.
--
-- It RETURNS a verdict rather than raising, so the caller can commit the denial
-- audit. There is deliberately NO raising wrapper: a wrapper that turns a
-- returned verdict into an exception destroys the audit that verdict just
-- wrote, which is the whole defect this file is built around.
CREATE TYPE identity_verdict AS (
    ok            boolean,
    reason        text,
    credential_id text,
    principal_id  text
);

CREATE FUNCTION identity_assert(p_secret text)
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


-- ---------------------------------------------------------------------------
-- Role DDL — the three functions that touch pg_authid
-- ---------------------------------------------------------------------------
-- These are the only functions in the schema that perform role DDL. They run as
-- their owner, `aicc_migrator`, which `render_bootstrap()` gives CREATEROLE and
-- ADMIN on `aicc_worker` for exactly this reason and no other.
--
-- STATED PLAINLY, because it is a real weakening and not a detail: this makes
-- the schema owner also the credential minter. The two SHOULD be separate roles
-- so that neither compromise alone is sufficient, and the reason they are not
-- here is mechanical rather than aesthetic — `render_table_grants()` opens with
-- `REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM PUBLIC`, executed as the
-- migrator, and PostgreSQL refuses that statement outright once any function in
-- the schema is owned by someone else (measured: `permission denied for
-- function`). Splitting the owner therefore needs the grant renderer to learn
-- about second owners first. Filed as VOYN-W0-AICC-SRV-03d.
CREATE FUNCTION identity_create_worker_role(p_role text) RETURNS void
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    -- NOLOGIN on creation: enrolment and credential issuance are separate
    -- steps, so a host that has been enrolled but not yet issued a credential
    -- cannot connect. Membership of `aicc_worker` is where its privileges come
    -- from; the per-host role carries none of its own, exactly as
    -- `render_worker_host_role()` already does for hand-provisioned hosts.
    EXECUTE format('CREATE ROLE %I NOLOGIN IN ROLE aicc_worker', p_role);
END
$$;

CREATE FUNCTION identity_set_role_secret(
    p_role        text,
    p_verifier    text,
    p_valid_until timestamptz
) RETURNS void
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    -- `p_verifier` is a SCRAM-SHA-256 verifier computed by the CLIENT, so the
    -- plaintext password never appears in a statement, in `log_statement`
    -- output, or in `pg_stat_activity.query`.
    EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L VALID UNTIL %L',
                   p_role, p_verifier, p_valid_until::text);
END
$$;

CREATE FUNCTION identity_disable_role(p_role text) RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_killed integer;
BEGIN
    IF p_role IS NULL THEN RETURN 0; END IF;
    -- Both halves are needed and neither is sufficient. NOLOGIN and
    -- VALID UNTIL '-infinity' stop FUTURE authentication, which PostgreSQL
    -- evaluates at authentication time only; `pg_terminate_backend` stops
    -- CURRENT sessions, without which a leaked password that is already
    -- connected keeps its connection indefinitely.
    EXECUTE format('ALTER ROLE %I NOLOGIN VALID UNTIL %L', p_role, '-infinity');
    -- Blunt on purpose: every backend of the role dies. Narrowing by
    -- `application_name` is NOT available as a control, because it is
    -- client-settable — it may be used for diagnostics, never for a targeting
    -- or authorisation decision.
    SELECT count(*) INTO v_killed FROM (
        SELECT pg_terminate_backend(pid) FROM pg_stat_activity
         WHERE usename = p_role AND pid <> pg_backend_pid()) t;
    RETURN v_killed;
END
$$;


-- ---------------------------------------------------------------------------
-- Revocation — one entry point
-- ---------------------------------------------------------------------------
CREATE FUNCTION identity_revoke_credential(p_credential_id text, p_reason text)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE c principal_credential%ROWTYPE; p principal%ROWTYPE;
BEGIN
    SELECT * INTO c FROM principal_credential
      WHERE credential_id = p_credential_id FOR UPDATE;
    IF NOT FOUND OR c.revoked_at IS NOT NULL THEN
        RETURN false;   -- idempotent: revoking twice is not an error
    END IF;

    UPDATE principal_credential
       SET revoked_at = now(), revoke_reason = p_reason,
           revoked_by = current_principal(), updated_at = now()
     WHERE credential_id = p_credential_id;

    SELECT * INTO p FROM principal WHERE principal_id = c.principal_id;
    PERFORM _principal_audit(c.principal_id, 'revoke', 'granted', c.credential_id,
                             p_reason, NULL);
    -- Worker hosts only. The operator's and the control plane's roles are
    -- provisioned outside this protocol and are shared with everything else
    -- those components do, so disabling one because a credential lapsed would
    -- turn a routine expiry into an outage of the control plane itself.
    IF p.kind = 'worker_host' THEN
        PERFORM identity_disable_role(p.db_role);
    END IF;
    RETURN true;
END
$$;

-- Revoke everything a principal holds. The incident lever, and the ONLY way a
-- host is revoked: SRV-03 adds no second host-revocation path, because a second
-- one is a second implementation to get right and a second one to audit.
CREATE FUNCTION identity_revoke_principal(p_principal_id text, p_reason text)
    RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_count integer := 0; r record;
BEGIN
    UPDATE principal SET state = 'suspended', updated_at = now()
     WHERE principal_id = p_principal_id AND state = 'active';
    FOR r IN SELECT credential_id FROM principal_credential
              WHERE principal_id = p_principal_id AND revoked_at IS NULL LOOP
        IF identity_revoke_credential(r.credential_id, p_reason) THEN
            v_count := v_count + 1;
        END IF;
    END LOOP;
    RETURN v_count;
END
$$;

-- Expiry is already enforced on every assert; this exists so an expired
-- credential is also CLOSED OUT — the row records when and why it died, and the
-- role is actually disabled rather than merely refused at the next login.
CREATE FUNCTION identity_sweep_expired() RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_count integer := 0; r record;
BEGIN
    FOR r IN SELECT credential_id FROM principal_credential
              WHERE revoked_at IS NULL AND expires_at <= now() LOOP
        IF identity_revoke_credential(r.credential_id, 'expired') THEN
            v_count := v_count + 1;
        END IF;
    END LOOP;
    RETURN v_count;
END
$$;


-- ---------------------------------------------------------------------------
-- Bootstrap: the first principals
-- ---------------------------------------------------------------------------
-- Granted to NO role. The operator and the control plane cannot be enrolled by
-- the enrolment protocol — something has to be first — so creating them is an
-- owner operation performed once at provisioning, in the same place the roles
-- themselves are created. Recorded rather than silently inserted: `bootstrap`
-- is its own audit event type, so "who existed before the protocol" is a
-- question the audit answers.
CREATE FUNCTION identity_bootstrap_principal(
    p_principal_id text,
    p_kind         text,
    p_db_role      text,
    p_display_name text,
    p_host         text DEFAULT NULL
) RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    INSERT INTO principal (principal_id, kind, db_role, display_name, host,
                           enrolled_by, state, trust_tier, created_at, updated_at)
    VALUES (p_principal_id, p_kind, p_db_role, p_display_name, p_host,
            current_principal(), 'active',
            CASE p_kind WHEN 'operator' THEN 0 WHEN 'control_plane' THEN 1 ELSE 2 END,
            now(), now());
    PERFORM _principal_audit(p_principal_id, 'bootstrap', 'granted', NULL, NULL,
                             jsonb_build_object('db_role', p_db_role, 'kind', p_kind));
    RETURN p_principal_id;
END
$$;


-- ---------------------------------------------------------------------------
-- Enrolment mechanism: a principal, a role, a credential
-- ---------------------------------------------------------------------------
-- Deliberately not reachable from anywhere except `enroll_redeem_ticket()`:
-- these are granted to no role, so "anything that can call this can conjure a
-- worker" is closed by the grant graph rather than by convention.
CREATE FUNCTION identity_enroll_worker(
    p_principal_id  text,
    p_display_name  text,
    p_host          text,
    p_expected_cidr inet DEFAULT NULL
) RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_role text;
BEGIN
    -- One LOGIN role per worker HOST — not per session, which would mean role
    -- DDL on every run, and not one shared role for the fleet, which is today's
    -- reality and means compromise of any host is compromise of all of them.
    v_role := 'aicc_w_' || regexp_replace(lower(p_principal_id), '[^a-z0-9]+', '_', 'g');
    PERFORM identity_create_worker_role(v_role);
    INSERT INTO principal (principal_id, kind, db_role, display_name, host,
                           expected_cidr, enrolled_by, state, trust_tier,
                           created_at, updated_at)
    VALUES (p_principal_id, 'worker_host', v_role, p_display_name, p_host,
            p_expected_cidr, current_principal(), 'active', 2, now(), now());
    PERFORM _principal_audit(p_principal_id, 'enroll_worker', 'granted', NULL, NULL,
                             jsonb_build_object('db_role', v_role, 'host', p_host));
    RETURN v_role;
END
$$;

-- The caller passes only derivatives: `p_secret_hash` is SHA-256 of the
-- plaintext (what `identity_assert()` matches) and `p_scram_verifier` is a
-- client-computed SCRAM-SHA-256 verifier (what PostgreSQL authenticates).
CREATE FUNCTION identity_issue_db_credential(
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


-- ===========================================================================
-- The enrolment protocol
-- ===========================================================================

-- Canonical fingerprint over a FIXED key set. Extra keys in the descriptor are
-- recorded but excluded from the hash, so adding a diagnostic field later does
-- not make every enrolled host look like a new machine.
CREATE FUNCTION _enroll_fingerprint_hash(p_descriptor jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, public AS $$
    SELECT encode(sha256(convert_to(
        jsonb_build_object(
            'machine_id', coalesce(p_descriptor ->> 'machine_id', ''),
            'os',         coalesce(p_descriptor ->> 'os', ''),
            'arch',       coalesce(p_descriptor ->> 'arch', ''),
            'hostname',   coalesce(p_descriptor ->> 'hostname', '')
        )::text, 'UTF8')), 'hex')
$$;


-- ---------------------------------------------------------------------------
-- enroll_mint_ticket() — where the admission policy lives
-- ---------------------------------------------------------------------------
--   * A ticket for a principal that ALREADY EXISTS is a re-enrolment, and if
--     that principal is not `active` — suspended or retired by an incident —
--     only an OPERATOR may mint it. This is what stops a revoked host coming
--     back, and it is why no separate block-list table is needed.
--   * A ticket for a NEW principal may be minted by the control plane, so that
--     bringing up a host does not require a human at 3am. The bound is
--     deliberate: a compromised control plane can enrol shadow hosts of its
--     own, but it cannot resurrect a host an operator retired, cannot learn any
--     existing host's secret, and every mint is appended to an audit it can
--     neither delete nor forge.
--   * A worker can never mint. Enrolment is not a peer-to-peer gossip protocol;
--     an enrolled host must not be able to admit others.
CREATE FUNCTION enroll_mint_ticket(
    p_intended_principal_id text,
    p_intended_host         text,
    p_ticket_hash           text,
    p_expected_cidr         inet     DEFAULT NULL,
    p_ttl                   interval DEFAULT NULL,
    p_purpose               text     DEFAULT 'enroll'
) RETURNS TABLE (minted_ticket_id text, refuse_reason text)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    v_caller      text;
    v_caller_tier integer;
    v_ttl         interval;
    v_id          text;
    p             principal%ROWTYPE;
BEGIN
    v_caller := current_principal();
    IF v_caller IS NULL THEN
        PERFORM _principal_audit(NULL, 'enroll_mint', 'rejected', NULL,
                                 'no_principal', NULL);
        RETURN QUERY SELECT NULL::text, 'no_principal'::text;
        RETURN;
    END IF;
    SELECT trust_tier INTO v_caller_tier FROM principal WHERE principal_id = v_caller;

    IF v_caller_tier > 1 THEN
        PERFORM _principal_audit(v_caller, 'enroll_mint', 'rejected', NULL,
                 'tier_violation', jsonb_build_object('intended', p_intended_principal_id));
        RETURN QUERY SELECT NULL::text, 'tier_violation'::text;
        RETURN;
    END IF;

    SELECT * INTO p FROM principal WHERE principal_id = p_intended_principal_id;

    IF FOUND THEN
        IF p_purpose <> 're_enroll' THEN
            PERFORM _principal_audit(v_caller, 'enroll_mint', 'rejected', NULL,
                     'principal_exists',
                     jsonb_build_object('intended', p_intended_principal_id));
            RETURN QUERY SELECT NULL::text, 'principal_exists'::text;
            RETURN;
        END IF;
        -- THE READMISSION GATE.
        IF p.state <> 'active' AND v_caller_tier > 0 THEN
            PERFORM _principal_audit(v_caller, 'enroll_mint', 'rejected', NULL,
                     'readmission_requires_operator',
                     jsonb_build_object('intended', p_intended_principal_id,
                                        'principal_state', p.state));
            RETURN QUERY SELECT NULL::text, 'readmission_requires_operator'::text;
            RETURN;
        END IF;
    ELSE
        IF p_purpose <> 'enroll' THEN
            PERFORM _principal_audit(v_caller, 'enroll_mint', 'rejected', NULL,
                     'unknown_principal',
                     jsonb_build_object('intended', p_intended_principal_id));
            RETURN QUERY SELECT NULL::text, 'unknown_principal'::text;
            RETURN;
        END IF;
    END IF;

    -- Minutes, because the whole security argument for a ticket is that its
    -- exposure is brief. Clamped from the server's clock.
    v_ttl := least(coalesce(p_ttl, interval '10 minutes'), interval '15 minutes');

    v_id := _identity_new_id('etk_');
    INSERT INTO enrollment_ticket (
        ticket_id, ticket_hash, intended_principal_id, intended_host,
        expected_cidr, purpose, state, issued_by, issued_at, expires_at,
        use_count, created_at, updated_at)
    VALUES (v_id, p_ticket_hash, p_intended_principal_id, p_intended_host,
            p_expected_cidr, p_purpose, 'issued', v_caller, now(), now() + v_ttl,
            0, now(), now());

    PERFORM _principal_audit(v_caller, 'enroll_mint', 'granted', NULL, NULL,
             jsonb_build_object('ticket_id', v_id, 'intended', p_intended_principal_id,
                                'purpose', p_purpose, 'ttl', v_ttl::text));
    RETURN QUERY SELECT v_id, NULL::text;
END
$$;


-- ---------------------------------------------------------------------------
-- enroll_redeem_ticket() — the whole protocol, in one transaction
-- ---------------------------------------------------------------------------
-- WHO CALLS IT. Not the enrolling host: by definition it has no database
-- credential yet, which is the entire problem. It calls the control plane's
-- enrolment endpoint, which calls this. That endpoint is the one HTTP surface
-- that is SELF-AUTHENTICATING — its only input is a ticket, and the ticket is
-- its own proof. The ticket secret does pass through the control plane in
-- flight; that gains an attacker who already owns the control plane nothing,
-- because such an attacker can mint tickets outright.
--
-- ATOMICITY. `FOR UPDATE` on the ticket row is the serialisation point, the
-- same shape 0002 uses for a claim. Every redemption of one ticket — winning,
-- losing or refused — queues on that lock, so the state test is made against a
-- state nobody can concurrently change. Two hosts presenting the same ticket
-- produce ONE enrolment and ONE loud refusal, never two credentials and never a
-- silent share.
CREATE FUNCTION enroll_redeem_ticket(
    p_ticket_secret  text,
    p_secret_hash    text,      -- sha256 of the host's OWN new secret
    p_scram_verifier text,      -- client-computed; plaintext never travels
    p_descriptor     jsonb
) RETURNS TABLE (
    -- Prefixed on purpose. Bare `principal_id` / `db_role` / `credential_id` /
    -- `expires_at` as OUT parameters shadow the identically-named columns of
    -- `principal`, `principal_credential` and `enrollment_ticket` throughout the
    -- body, and PL/pgSQL resolves that as an ambiguity error at RUNTIME rather
    -- than at creation — the function would install cleanly and fail on first
    -- use.
    enrolled_principal_id  text,
    enrolled_db_role       text,
    enrolled_credential_id text,
    credential_expires_at  timestamptz,
    refuse_reason          text
)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    t          enrollment_ticket%ROWTYPE;
    p          principal%ROWTYPE;
    v_hash     text := encode(sha256(convert_to(p_ticket_secret, 'UTF8')), 'hex');
    v_fp       text := _enroll_fingerprint_hash(p_descriptor);
    v_role     text;
    v_cred     text;
    v_issue    text;
    v_conflict text;
    v_seq      integer;
    v_reason   text;
    v_expires  timestamptz;
    v_found    boolean;
BEGIN
    SELECT * INTO t FROM enrollment_ticket WHERE ticket_hash = v_hash FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _principal_audit(NULL, 'enroll', 'rejected', NULL,
                                 'unknown_ticket', NULL);
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text,
                            NULL::timestamptz, 'unknown_ticket'::text;
        RETURN;
    END IF;

    IF t.state <> 'issued' OR t.use_count <> 0 THEN
        -- THE THEFT ALARM. If this fires for a legitimate host, someone else
        -- already redeemed its ticket. It is a RETURNED refusal precisely so
        -- that this audit row commits: the alarm has to outlive the call.
        PERFORM _principal_audit(t.issued_by, 'enroll', 'rejected', NULL,
                 'ticket_' || t.state,
                 jsonb_build_object('ticket_id', t.ticket_id,
                                    'intended', t.intended_principal_id,
                                    'use_count', t.use_count));
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text,
                            NULL::timestamptz, ('ticket_' || t.state)::text;
        RETURN;
    END IF;

    IF t.expires_at <= now() THEN
        -- This close-out UPDATE is the other thing a RAISE here would discard.
        UPDATE enrollment_ticket SET state = 'expired', updated_at = now()
         WHERE ticket_id = t.ticket_id;
        PERFORM _principal_audit(t.issued_by, 'enroll', 'rejected', NULL,
                 'ticket_expired', jsonb_build_object('ticket_id', t.ticket_id,
                                                      'expires_at', t.expires_at));
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text,
                            NULL::timestamptz, 'ticket_expired'::text;
        RETURN;
    END IF;

    -- CLONE SIGNAL: the same machine fingerprint already belongs to a different
    -- principal. Refused rather than warned — a clone getting its own second
    -- identity is how one compromised image quietly becomes a fleet. What this
    -- does NOT catch is a clone that keeps the ORIGINAL's secret and never
    -- enrols at all; that is indistinguishable from the original by
    -- construction, and only per-host key material in a TPM would close it.
    SELECT f.principal_id INTO v_conflict
      FROM worker_host_fingerprint f
     WHERE f.fingerprint_hash = v_fp
       AND f.principal_id <> t.intended_principal_id
     LIMIT 1;
    IF v_conflict IS NOT NULL THEN
        PERFORM _principal_audit(t.issued_by, 'enroll', 'rejected', NULL,
                 'fingerprint_conflict',
                 jsonb_build_object('ticket_id', t.ticket_id,
                                    'intended', t.intended_principal_id,
                                    'already_bound_to', v_conflict));
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text,
                            NULL::timestamptz, 'fingerprint_conflict'::text;
        RETURN;
    END IF;

    SELECT * INTO p FROM principal
      WHERE principal_id = t.intended_principal_id FOR UPDATE;
    v_found := FOUND;

    IF t.purpose = 'enroll' THEN
        IF v_found THEN
            -- Lost a race, or the ticket outlived its purpose.
            PERFORM _principal_audit(t.issued_by, 'enroll', 'rejected', NULL,
                     'principal_exists', jsonb_build_object('ticket_id', t.ticket_id));
            RETURN QUERY SELECT NULL::text, NULL::text, NULL::text,
                                NULL::timestamptz, 'principal_exists'::text;
            RETURN;
        END IF;
        v_role := identity_enroll_worker(t.intended_principal_id,
                                         t.intended_host, t.intended_host,
                                         t.expected_cidr);
        v_reason := NULL;
    ELSE
        IF NOT v_found THEN
            PERFORM _principal_audit(t.issued_by, 'enroll', 'rejected', NULL,
                     'unknown_principal', jsonb_build_object('ticket_id', t.ticket_id));
            RETURN QUERY SELECT NULL::text, NULL::text, NULL::text,
                                NULL::timestamptz, 'unknown_principal'::text;
            RETURN;
        END IF;
        v_role := p.db_role;
        -- Readmission is reachable ONLY because `enroll_mint_ticket()` already
        -- required an operator to mint a `re_enroll` ticket for a non-active
        -- principal. There is no other route to this reactivation.
        IF p.state <> 'active' THEN
            v_reason := 'readmitted';
        ELSIF EXISTS (SELECT 1 FROM worker_host_fingerprint f
                       WHERE f.principal_id = p.principal_id
                         AND f.fingerprint_hash = v_fp) THEN
            v_reason := 'rebuild';             -- same machine, new secret
        ELSE
            -- New hardware under an old name. ACCEPTED and recorded, not
            -- refused: refusing it would break every legitimate rebuild, and
            -- the descriptor is unattested anyway, so refusing on it would be
            -- security theatre with an availability cost.
            v_reason := 'fingerprint_changed';
        END IF;
        UPDATE principal
           SET state = 'active',
               expected_cidr = coalesce(t.expected_cidr, expected_cidr),
               host = t.intended_host,
               updated_at = now()
         WHERE principal.principal_id = t.intended_principal_id;
    END IF;

    SELECT issued_credential_id, issue_refuse_reason INTO v_cred, v_issue
      FROM identity_issue_db_credential(t.intended_principal_id, p_secret_hash,
                                        p_scram_verifier, interval '1 hour');
    IF v_cred IS NULL THEN
        -- Cannot happen through this path today — the principal was just
        -- created or reactivated above, and issuance to a lower tier is
        -- allowed. Surfaced rather than swallowed so that a future change to
        -- either rule fails loudly and with the reason, instead of leaving a
        -- ticket consumed and a host with no credential.
        RETURN QUERY SELECT NULL::text, NULL::text, NULL::text,
                            NULL::timestamptz, v_issue;
        RETURN;
    END IF;
    SELECT c.expires_at INTO v_expires
      FROM principal_credential c WHERE c.credential_id = v_cred;

    SELECT coalesce(max(f.seq), 0) + 1 INTO v_seq
      FROM worker_host_fingerprint f WHERE f.principal_id = t.intended_principal_id;
    INSERT INTO worker_host_fingerprint (
        principal_id, seq, fingerprint_hash, descriptor_json, change_reason,
        observed_at, observed_from_addr, created_at)
    VALUES (t.intended_principal_id, v_seq, v_fp, p_descriptor, v_reason,
            now(), inet_client_addr(), now());

    UPDATE enrollment_ticket
       SET state = 'redeemed', use_count = 1, redeemed_at = now(),
           redeemed_from_addr = inet_client_addr(),
           redeemed_credential_id = v_cred, updated_at = now()
     WHERE ticket_id = t.ticket_id;

    PERFORM _principal_audit(t.intended_principal_id, 'enroll', 'granted', v_cred,
             NULL, jsonb_build_object('ticket_id', t.ticket_id, 'purpose', t.purpose,
                                      'db_role', v_role, 'fingerprint', v_fp,
                                      'change_reason', v_reason,
                                      'fingerprint_seq', v_seq));

    RETURN QUERY SELECT t.intended_principal_id, v_role, v_cred, v_expires, NULL::text;
END
$$;


-- ---------------------------------------------------------------------------
-- enroll_rotate_self() — rotation with no availability gap
-- ---------------------------------------------------------------------------
-- PostgreSQL stores exactly ONE verifier per role, so two live passwords for
-- one host are not representable and "overlap the old and the new secret" is
-- not available. What IS available follows from a fact about PostgreSQL: it
-- checks the password at AUTHENTICATION time only, so changing it does not
-- disturb an established session. So the HOST drives its own rotation, and the
-- ordering is the whole argument:
--
--   1. the host generates the new secret locally — from this instant it knows
--      BOTH secrets;
--   2. it calls this on its own already-open, already-authenticated connection,
--      proving possession of the CURRENT secret;
--   3. the verifier changes. Existing connections are unaffected; every new
--      connection uses the new secret, which the host has held since step 1.
--
-- There is no instant at which `pg_authid` holds a value the host does not
-- know, and no instant at which the host holds zero working connections.
--
-- Residual, stated: an attacker holding a stolen secret can rotate it and evict
-- the legitimate host. That is a denial of service and an eviction, NOT an
-- escalation — scope and TTL are unchanged — and it is loud, because the
-- evicted host's return needs an operator-minted ticket.
CREATE FUNCTION enroll_rotate_self(
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


-- ---------------------------------------------------------------------------
-- enroll_revoke_ticket() / enroll_sweep_expired()
-- ---------------------------------------------------------------------------
-- Revoking a HOST is deliberately not here: that is `identity_revoke_principal()`,
-- the single revocation entry point. These two cover only the object this
-- migration introduces — a ticket in flight.
CREATE FUNCTION enroll_revoke_ticket(p_ticket_id text, p_reason text)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t enrollment_ticket%ROWTYPE;
BEGIN
    SELECT * INTO t FROM enrollment_ticket WHERE ticket_id = p_ticket_id FOR UPDATE;
    IF NOT FOUND OR t.state <> 'issued' THEN
        RETURN false;   -- idempotent, like identity_revoke_credential
    END IF;
    UPDATE enrollment_ticket
       SET state = 'revoked', revoked_at = now(), revoke_reason = p_reason,
           updated_at = now()
     WHERE ticket_id = p_ticket_id;
    PERFORM _principal_audit(current_principal(), 'enroll_revoke', 'granted', NULL,
             p_reason, jsonb_build_object('ticket_id', p_ticket_id));
    RETURN true;
END
$$;

CREATE FUNCTION enroll_sweep_expired() RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_count integer;
BEGIN
    -- Expiry is already enforced on every redemption; this only closes the row
    -- out so an operator can see dead tickets, exactly as `identity_sweep_expired()`
    -- does for credentials. It is hygiene, not correctness.
    WITH swept AS (
        UPDATE enrollment_ticket SET state = 'expired', updated_at = now()
         WHERE state = 'issued' AND expires_at <= now()
        RETURNING 1)
    SELECT count(*) INTO v_count FROM swept;
    RETURN v_count;
END
$$;
