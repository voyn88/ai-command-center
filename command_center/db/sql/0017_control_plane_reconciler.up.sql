-- 0017_control_plane_reconciler
--
-- VOYN-W0-AICC-CONTROL-PLANE-RESILIENCE. Live 2026-08-29:
-- `aicc-backlog-planner.timer` went `inactive (dead)` with no service
-- failure, no OOM, no reboot -- and every OTHER timer (review, merge,
-- self-deploy) kept running, so the loop looked alive from the outside
-- while zero new work was dispatched for 13 hours. `systemctl is-active`
-- alone cannot catch that class of failure: the tick itself never ran to
-- report an error. Two things are needed, and they are deliberately two
-- tables:
--
-- `control_plane_heartbeat` answers "is the TICK doing work" -- one row per
-- named tick, written by the tick itself (or the CLI wrapper around it)
-- every time it completes, regardless of whether it found anything to do.
-- A tick that keeps succeeding with nothing to dispatch still advances
-- `last_ok_at`; only a tick that stopped running at all goes stale.
--
-- `control_plane_unit_state` + `control_plane_event` answer "is the
-- RECONCILER's own corrective action working" -- the bounded-backoff circuit
-- breaker (VOYN-W0-AICC-CONTROL-PLANE-RESILIENCE's acceptance: retry with
-- backoff, then escalate once, never retry forever). One row of current
-- circuit state per unit, and an append-only event ledger an operator (or
-- worker-01's independent cross-check, VOYN-W0-AICC-CONTROL-PLANE-
-- RESILIENCE's second acceptance clause) can read to see what the
-- reconciler already tried.

CREATE TABLE control_plane_heartbeat (
    tick_name text PRIMARY KEY,
    last_ok_at timestamptz NOT NULL,
    detail text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE control_plane_unit_state (
    unit_name text PRIMARY KEY,
    consecutive_failures integer NOT NULL DEFAULT 0
        CHECK (consecutive_failures >= 0),
    circuit_open_until timestamptz,
    last_action text NOT NULL DEFAULT '',
    last_outcome text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE control_plane_event (
    event_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at timestamptz NOT NULL DEFAULT now(),
    unit_name text NOT NULL,
    action text NOT NULL,
    outcome text NOT NULL,
    detail text NOT NULL DEFAULT ''
);

CREATE INDEX idx_control_plane_event_unit_at
    ON control_plane_event (unit_name, at DESC);

REVOKE ALL ON TABLE control_plane_heartbeat FROM PUBLIC;
REVOKE ALL ON TABLE control_plane_unit_state FROM PUBLIC;
REVOKE ALL ON TABLE control_plane_event FROM PUBLIC;
