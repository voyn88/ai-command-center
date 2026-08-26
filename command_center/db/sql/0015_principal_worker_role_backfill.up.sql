-- SRV-02 migration: close the gap between "a PostgreSQL role exists" and "a
-- principal exists" for worker hosts (VOYN-W0-AICC-SRV-02-MIGRATION).
--
-- THE GAP, STATED PLAINLY. 0003 gave every worker host a `principal` row when
-- it is admitted through `identity_enroll_worker()` (ticket redemption). But
-- 0003's own header names a second, older path that produces the identical
-- kind of PostgreSQL role without ever touching `principal`:
-- `render_worker_host_role()` "gives each execution host a LOGIN role of its
-- own" by a bare `CREATE ROLE ... LOGIN IN ROLE aicc_worker`, and
-- `identity_create_worker_role()`'s own comment says it does "exactly as
-- `render_worker_host_role()` already does for hand-provisioned hosts". A role
-- created that way authenticates and claims work under 0002 today -- claim
-- identity is `session_user`, which does not care how the role was made -- but
-- has no `principal` row, no `principal_event` audit trail and no credential
-- lifecycle. It is invisible to `identity_revoke_principal()`, to
-- `identity_sweep_expired()`, and to the join 0002's own header anticipates
-- ("if and when it lands, joins to `work_attempt.claimed_by_role`"): that join
-- silently drops every hand-provisioned host, which is a worse failure than an
-- error, because a dropped row reads as "no such claimant" rather than "not
-- migrated yet".
--
-- EXPAND (this migration). `identity_backfill_worker_role()` lets an operator
-- register the `principal` row for a worker role that already exists, without
-- touching its credential or its membership -- purely additive, and safe to
-- run against a role that already has one (idempotent: returns the existing
-- `principal_id` unchanged rather than erroring or duplicating). Paired with
-- it, `render_worker_host_role()` (`command_center/db/roles.py`) now calls the
-- equivalent insert itself, so every NEW hand-provisioned role gets a
-- `principal` row at creation time and the gap stops growing.
--
-- CONTRACT (deliberately NOT done here; see
-- `docs/operations/SRV-02-PRINCIPAL-BACKFILL.md`). Retiring
-- `render_worker_host_role()` in favour of ticket enrolment as the ONLY way to
-- produce a worker role is a live-fleet change: it must wait until every
-- existing `aicc_worker` member has been reconciled (verifiable with the query
-- in that runbook), and it must happen with the worker fleet drained (SRV-03
-- stop, `ops/aicc_staged_worker_rollout.py`) so no host is mid-connection on a
-- role the reconciliation has not reached yet. That is a reviewed, scheduled
-- maintenance change, not a schema migration, which is exactly why this
-- migration stops at the additive half.
CREATE FUNCTION identity_backfill_worker_role(p_role text, p_host text DEFAULT NULL)
    RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    v_existing text;
BEGIN
    SELECT principal_id INTO v_existing FROM principal WHERE db_role = p_role;
    IF FOUND THEN
        RETURN v_existing;   -- idempotent: already reconciled, not an error
    END IF;

    -- Fails closed on Postgres's own errors for "no such role" (bare
    -- `pg_has_role` on an unknown role raises, it does not return false), and
    -- explicitly on a role this migration has no business registering: only a
    -- member of `aicc_worker` is the shape `render_worker_host_role()` and
    -- `identity_enroll_worker()` both produce.
    IF NOT pg_has_role(p_role, 'aicc_worker', 'member') THEN
        RAISE EXCEPTION 'identity_backfill_worker_role: % is not a member of aicc_worker', p_role
            USING ERRCODE = '28000';
    END IF;

    INSERT INTO principal (principal_id, kind, db_role, display_name, host,
                           enrolled_by, state, trust_tier, created_at, updated_at)
    VALUES (p_role, 'worker_host', p_role, p_role, coalesce(p_host, p_role),
            current_principal(), 'active', 2, now(), now());
    PERFORM _principal_audit(p_role, 'enroll_worker', 'granted', NULL,
                             'legacy_role_backfill',
                             jsonb_build_object('db_role', p_role, 'host',
                                                 coalesce(p_host, p_role)));
    RETURN p_role;
END
$$;
