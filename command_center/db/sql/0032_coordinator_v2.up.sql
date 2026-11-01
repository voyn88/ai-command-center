-- Durable Grok↔Cursor coordination state (coordination v2).
--
-- The planner tick remains the authority for dispatch. This table only stores
-- (1) the last candidate/evidence snapshot hash, (2) at-most-one in-flight
-- coordination work-item key, and (3) an advisory ordering of already-eligible
-- task ids. Every mutation goes through SECURITY DEFINER CAS helpers so the
-- control plane cannot widen permissions or mark tasks DONE via DML.
--
-- Opt-in is environmental (AICC_COORDINATOR_V2); this migration only creates
-- the durable seat. Default row is not inserted -- first CAS creates it.

CREATE TABLE coordinator_state (
    name              text PRIMARY KEY,
    snapshot_hash     text NOT NULL DEFAULT '',
    blocker_hash      text NOT NULL DEFAULT '',
    advisory_order    jsonb NOT NULL DEFAULT '[]'::jsonb,
    in_flight_key     text,
    in_flight_at      timestamptz,
    last_result_hash  text NOT NULL DEFAULT '',
    last_status       text NOT NULL DEFAULT 'idle',
    cooldown_until    timestamptz,
    detail            jsonb NOT NULL DEFAULT '{}'::jsonb,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT coordinator_state_name_present CHECK (length(name) > 0),
    CONSTRAINT coordinator_state_status_vocab CHECK (
        last_status IN (
            'idle', 'queued', 'applied', 'rejected', 'cooldown', 'stopped', 'enqueue_failed'
        )
    ),
    CONSTRAINT coordinator_state_order_is_array CHECK (
        jsonb_typeof(advisory_order) = 'array'
    )
);

CREATE FUNCTION coordinator_state_read(p_name text)
RETURNS TABLE (
    snapshot_hash text,
    blocker_hash text,
    advisory_order jsonb,
    in_flight_key text,
    in_flight_at timestamptz,
    last_result_hash text,
    last_status text,
    cooldown_until timestamptz,
    detail jsonb
)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
    SELECT s.snapshot_hash, s.blocker_hash, s.advisory_order, s.in_flight_key,
           s.in_flight_at, s.last_result_hash, s.last_status, s.cooldown_until,
           s.detail
      FROM coordinator_state s
     WHERE s.name = p_name
$$;

-- Claim the right to enqueue exactly one coordination action for a NEW
-- snapshot (or a NEW blocker). Refuses when:
--   * another action is already in flight
--   * snapshot_hash is unchanged (economical dedup)
--   * blocker_hash is unchanged AND a prior reject/cooldown recorded it
--   * cooldown_until is still in the future
CREATE FUNCTION coordinator_cas_begin(
    p_name text,
    p_snapshot_hash text,
    p_blocker_hash text,
    p_key text,
    p_now timestamptz DEFAULT clock_timestamp()
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE s coordinator_state%ROWTYPE;
        attempts integer;
BEGIN
    IF p_name IS NULL OR length(p_name) = 0
       OR p_snapshot_hash IS NULL OR length(p_snapshot_hash) = 0
       OR p_key IS NULL OR length(p_key) = 0 THEN
        RETURN false;
    END IF;
    INSERT INTO coordinator_state (name) VALUES (p_name)
        ON CONFLICT (name) DO NOTHING;
    SELECT * INTO s FROM coordinator_state WHERE name = p_name FOR UPDATE;
    IF s.in_flight_key IS NOT NULL THEN
        RETURN false;
    END IF;
    IF s.cooldown_until IS NOT NULL AND s.cooldown_until > p_now THEN
        RETURN false;
    END IF;
    IF s.snapshot_hash = p_snapshot_hash AND s.last_status <> 'enqueue_failed' THEN
        RETURN false;
    END IF;
    attempts := CASE WHEN s.snapshot_hash = p_snapshot_hash
        THEN coalesce((s.detail->>'enqueue_attempts')::integer, 0) ELSE 0 END;
    IF attempts >= 3 THEN
        RETURN false;
    END IF;
    IF p_blocker_hash IS NOT NULL
       AND length(p_blocker_hash) > 0
       AND s.blocker_hash = p_blocker_hash
       AND s.last_status IN ('rejected', 'cooldown') THEN
        RETURN false;
    END IF;
    UPDATE coordinator_state
       SET snapshot_hash = p_snapshot_hash,
           blocker_hash = coalesce(p_blocker_hash, ''),
           in_flight_key = p_key,
           in_flight_at = p_now,
           last_status = 'queued',
           detail = jsonb_build_object('begin_key', p_key, 'enqueue_attempts', attempts + 1),
           updated_at = p_now
     WHERE name = p_name;
    RETURN true;
END
$$;

-- Finish (apply / reject / clear) only the in-flight key we claimed.
CREATE FUNCTION coordinator_cas_finish(
    p_name text,
    p_key text,
    p_status text,
    p_advisory_order jsonb DEFAULT NULL,
    p_result_hash text DEFAULT '',
    p_detail jsonb DEFAULT '{}'::jsonb,
    p_cooldown_seconds integer DEFAULT 0,
    p_now timestamptz DEFAULT clock_timestamp()
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE s coordinator_state%ROWTYPE;
        v_order jsonb;
        v_cooldown timestamptz;
BEGIN
    IF p_status NOT IN ('applied', 'rejected', 'cooldown', 'idle', 'stopped') THEN
        RETURN false;
    END IF;
    SELECT * INTO s FROM coordinator_state WHERE name = p_name FOR UPDATE;
    IF NOT FOUND OR s.in_flight_key IS DISTINCT FROM p_key THEN
        RETURN false;
    END IF;
    v_order := s.advisory_order;
    IF p_status = 'applied' THEN
        IF p_advisory_order IS NULL
           OR jsonb_typeof(p_advisory_order) <> 'array' THEN
            RETURN false;
        END IF;
        v_order := p_advisory_order;
    END IF;
    v_cooldown := NULL;
    IF greatest(coalesce(p_cooldown_seconds, 0), 0) > 0 THEN
        v_cooldown := p_now + make_interval(secs => greatest(p_cooldown_seconds, 0));
    END IF;
    UPDATE coordinator_state
       SET in_flight_key = NULL,
           in_flight_at = NULL,
           advisory_order = v_order,
           last_result_hash = coalesce(p_result_hash, ''),
           last_status = p_status,
           cooldown_until = v_cooldown,
           detail = coalesce(p_detail, '{}'::jsonb),
           updated_at = p_now
     WHERE name = p_name;
    RETURN true;
END
$$;

-- Clear a stale in-flight claim whose work item vanished (crash between CAS
-- begin and enqueue, or operator purge). Only the recorded key may clear.
CREATE FUNCTION coordinator_cas_clear_inflight(
    p_name text,
    p_key text,
    p_reason text DEFAULT 'stale_inflight',
    p_now timestamptz DEFAULT clock_timestamp()
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE s coordinator_state%ROWTYPE;
BEGIN
    SELECT * INTO s FROM coordinator_state WHERE name = p_name FOR UPDATE;
    IF NOT FOUND OR s.in_flight_key IS DISTINCT FROM p_key THEN
        RETURN false;
    END IF;
    IF EXISTS (SELECT 1 FROM work_item WHERE idempotency_key = p_key) THEN
        RETURN false;
    END IF;
    UPDATE coordinator_state
       SET in_flight_key = NULL,
           in_flight_at = NULL,
           last_status = 'enqueue_failed',
           cooldown_until = p_now + interval '5 minutes',
           detail = s.detail || jsonb_build_object('cleared', p_reason),
           updated_at = p_now
     WHERE name = p_name;
    RETURN true;
END
$$;

REVOKE ALL ON TABLE coordinator_state FROM PUBLIC;
REVOKE ALL ON FUNCTION coordinator_state_read(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION coordinator_cas_begin(text, text, text, text, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION coordinator_cas_finish(text, text, text, jsonb, text, jsonb, integer, timestamptz) FROM PUBLIC;
REVOKE ALL ON FUNCTION coordinator_cas_clear_inflight(text, text, text, timestamptz) FROM PUBLIC;


-- Claim and queue insertion are one transaction: a crash cannot commit half
-- an enqueue. Only pre-queue failures retry, up to three attempts per snapshot
-- with five-minute cooldown. A model failure is never retried by this helper.
CREATE FUNCTION coordinator_enqueue(
    p_name text, p_snapshot_hash text, p_blocker_hash text, p_key text,
    p_queue text, p_payload jsonb
) RETURNS TABLE (work_item_id text, failure text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_id text;
BEGIN
    IF NOT coordinator_cas_begin(p_name, p_snapshot_hash, p_blocker_hash, p_key) THEN
        RETURN QUERY SELECT NULL::text, 'cas_begin_refused'::text;
        RETURN;
    END IF;
    BEGIN
        v_id := queue_enqueue(p_queue, p_key, p_payload,
            'VOYN-AICC-COORD-V2', NULL, 1, 100, 0, 2);
    EXCEPTION WHEN OTHERS THEN
        UPDATE coordinator_state SET in_flight_key = NULL, in_flight_at = NULL,
            last_status = 'enqueue_failed',
            cooldown_until = clock_timestamp() + interval '5 minutes',
            detail = detail || jsonb_build_object('enqueue_sqlstate', SQLSTATE),
            updated_at = clock_timestamp()
        WHERE name = p_name AND in_flight_key = p_key;
        RETURN QUERY SELECT NULL::text, ('enqueue_failed:' || SQLSTATE)::text;
        RETURN;
    END;
    RETURN QUERY SELECT v_id, NULL::text;
END
$$;
REVOKE ALL ON FUNCTION coordinator_enqueue(text, text, text, text, text, jsonb) FROM PUBLIC;
