-- 0018: fleet-wide executor availability, with a TTL a worker can set and
-- every worker can see (VOYN-W0-AICC-EXECUTOR-QUOTA-VISIBILITY).
--
-- Incident, live 2026-08-30: Copilot CLI (authorized, 1.0.80) returned "You
-- have exceeded your monthly quota" on an independent-review dispatch. The
-- executor cascade (BO-S2a, `routing.ROUTING_MATRIX`) exists precisely so an
-- exhausted account fails over to a DIFFERENT one -- and it did, inside that
-- one already-claimed attempt (`worker.handlers._run_agent`'s in-lease
-- failover, VOYN-W0-AICC-PROVIDER-FAILURE-IN-ATTEMPT-FAILOVER). But nothing
-- outlived that attempt: `_executor_preflight` only ever probed the CLI
-- binary's presence, never the account's quota state, so the very next claim
-- -- this task's own redelivery, another task on the same host, or any other
-- worker in the fleet -- picked Copilot again and paid the same cost:
-- process launch time and a burned queue attempt, for zero model work.
--
-- This migration gives the fact a home worker processes across the fleet
-- share: `executor_availability` is the CURRENT verdict (one row per
-- executor, O(1) lookup on the hot path before every dispatch),
-- `executor_availability_event` is the durable history (why, and when,
-- something was marked unavailable) -- the same split `work_item`/`work_event`
-- already uses, for the same reason: a live check that is fast, and an audit
-- trail that is complete.
--
-- The TTL is a COOLDOWN, not a promise of the provider's true reset time --
-- nothing on this side of the CLI knows a monthly quota's actual reset
-- instant. A bounded cooldown that the caller chooses (`p_ttl_seconds`) is
-- the honest alternative: it stops the immediate re-selection this incident
-- exposed, and `executor_mark_available` lets a human (the operator role) or
-- a later successful run clear it early rather than waiting out a guess.
--
-- Mutation is function-only, matching the queue-claim idiom (0002): a worker
-- that could `UPDATE executor_availability` directly could also forge
-- "available" over a genuinely exhausted account, masking the very outage
-- this table exists to make visible. `aicc_worker` gets EXECUTE on the two
-- functions and plain SELECT on the live-state table (no sensitive column
-- here, unlike `work_attempt` -- there is nothing here worth hiding from the
-- role that is the sole source of the facts in it); `aicc_app` gets SELECT on
-- both tables, for dashboards and the delivery-metrics reads that already
-- work this way (`work_queue_read.queue_metrics()` is the same shape: a
-- read-only aggregation over durable rows, not a bespoke metrics-writer
-- path).

CREATE TABLE executor_availability (
    executor_id        text        PRIMARY KEY,

    -- 'available' | 'unavailable'.
    status             text        NOT NULL DEFAULT 'available',

    reason             text,
    unavailable_until  timestamptz,

    updated_at         timestamptz NOT NULL,

    CONSTRAINT executor_availability_status_valid
        CHECK (status IN ('available', 'unavailable')),
    -- Makes "unavailable with no reason" or "unavailable with no expiry"
    -- unrepresentable, the same discipline `work_item_dead_has_reason` (0002)
    -- applies to the queue's own terminal state.
    CONSTRAINT executor_availability_unavailable_is_complete
        CHECK ((status = 'unavailable') = (reason IS NOT NULL AND unavailable_until IS NOT NULL))
);

-- ---------------------------------------------------------------------------
-- executor_availability_event — append-only audit of every mark/clear.
-- Same shape as work_event / run_event / council_event: an identity id, no
-- gap-free per-parent seq (there is no natural parent to be gap-free against
-- -- an executor is not owned by one item), a plain insert order.
-- ---------------------------------------------------------------------------
CREATE TABLE executor_availability_event (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    executor_id        text        NOT NULL,
    event              text        NOT NULL,  -- marked_unavailable | cleared
    reason             text,
    unavailable_until  timestamptz,
    actor_role         text        NOT NULL,  -- session_user, same rule as work_event
    created_at         timestamptz NOT NULL,

    CONSTRAINT executor_availability_event_event_valid
        CHECK (event IN ('marked_unavailable', 'cleared'))
);

CREATE INDEX idx_executor_availability_event_executor
    ON executor_availability_event(executor_id, created_at);

-- ---------------------------------------------------------------------------
-- executor_mark_unavailable — a worker's own observation that an executor's
-- account cannot run right now (quota, auth, ...). Idempotent: a second
-- failure inside the cooldown window simply refreshes the deadline, a sliding
-- window rather than a queue of expiries to reconcile.
-- ---------------------------------------------------------------------------
CREATE FUNCTION executor_mark_unavailable(
    p_executor_id text, p_reason text, p_ttl_seconds integer
) RETURNS void
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_until timestamptz;
BEGIN
    IF p_executor_id IS NULL OR length(p_executor_id) = 0 THEN
        RAISE EXCEPTION 'executor_id is required';
    END IF;
    IF p_reason IS NULL OR length(p_reason) = 0 THEN
        RAISE EXCEPTION 'reason is required';
    END IF;
    -- Floored at 1 second: a zero/negative TTL from a caller bug must not
    -- collapse into "no cooldown at all" (unavailable_until <= now(), which a
    -- reader would treat as already-expired) nor into a NULL that the
    -- completeness constraint above would reject outright.
    v_until := now() + make_interval(secs => greatest(coalesce(p_ttl_seconds, 0), 1));

    INSERT INTO executor_availability (executor_id, status, reason, unavailable_until, updated_at)
    VALUES (p_executor_id, 'unavailable', p_reason, v_until, now())
    ON CONFLICT (executor_id) DO UPDATE
       SET status = 'unavailable',
           reason = EXCLUDED.reason,
           unavailable_until = EXCLUDED.unavailable_until,
           updated_at = now();

    INSERT INTO executor_availability_event
        (executor_id, event, reason, unavailable_until, actor_role, created_at)
    VALUES (p_executor_id, 'marked_unavailable', p_reason, v_until, session_user, now());
END
$$;

-- ---------------------------------------------------------------------------
-- executor_mark_available — clear a mark before its cooldown elapses: an
-- operator override, or a worker's own signal that a later run through the
-- same executor actually succeeded.
-- ---------------------------------------------------------------------------
CREATE FUNCTION executor_mark_available(p_executor_id text) RETURNS void
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF p_executor_id IS NULL OR length(p_executor_id) = 0 THEN
        RAISE EXCEPTION 'executor_id is required';
    END IF;

    INSERT INTO executor_availability (executor_id, status, reason, unavailable_until, updated_at)
    VALUES (p_executor_id, 'available', NULL, NULL, now())
    ON CONFLICT (executor_id) DO UPDATE
       SET status = 'available', reason = NULL, unavailable_until = NULL, updated_at = now();

    INSERT INTO executor_availability_event
        (executor_id, event, reason, unavailable_until, actor_role, created_at)
    VALUES (p_executor_id, 'cleared', NULL, NULL, session_user, now());
END
$$;
