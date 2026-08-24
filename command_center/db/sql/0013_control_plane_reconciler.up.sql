-- Durable control-plane state for VOYN-W0-AICC-CONTROL-PLANE-RECONCILER.
--
-- Timers are wake-up mechanisms, not memory.  The previous control plane kept
-- its only "next step" in the fact that a particular timer happened to be
-- enabled.  When planner/review timers stopped, green work therefore had no
-- durable owner, deadline or recovery path.  These rows are the authority for
-- delivery progress; systemd merely calls the reconciler that advances them.

CREATE TABLE control_plane_lane (
    task_id          text PRIMARY KEY REFERENCES backlog_task(task_id),
    state            text NOT NULL DEFAULT 'READY'
                     CHECK (state IN ('READY', 'RUNNING', 'WAITING', 'BLOCKED', 'DONE')),
    next_action      text NOT NULL
                     CHECK (next_action IN (
                         'GUARDED_PUBLISH', 'CI_WAIT', 'INDEPENDENT_REVIEW',
                         'ACCEPTANCE', 'MERGE', 'DEPLOY', 'BACKLOG_SYNC', 'NONE'
                     )),
    owner            text NOT NULL,
    claimant         text,
    deadline_at      timestamptz NOT NULL,
    heartbeat_at     timestamptz,
    lease_expires_at timestamptz,
    attempts         integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     integer NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 100),
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    blocked_by       text,
    last_error       text,
    revision         bigint NOT NULL DEFAULT 1,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CHECK ((state = 'RUNNING') = (claimant IS NOT NULL)),
    CHECK ((state = 'RUNNING') = (lease_expires_at IS NOT NULL))
);

CREATE INDEX idx_control_plane_lane_due
    ON control_plane_lane(state, deadline_at, lease_expires_at);

CREATE TABLE control_plane_event (
    event_id     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id      text REFERENCES backlog_task(task_id),
    component    text NOT NULL,
    event        text NOT NULL,
    outcome      text NOT NULL,
    detail       jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_control_plane_event_task_time
    ON control_plane_event(task_id, created_at DESC, event_id DESC);

CREATE TABLE control_plane_notification (
    notification_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id          text NOT NULL REFERENCES backlog_task(task_id),
    kind             text NOT NULL,
    owner            text NOT NULL,
    payload          jsonb NOT NULL DEFAULT '{}'::jsonb,
    state            text NOT NULL DEFAULT 'PENDING'
                     CHECK (state IN ('PENDING', 'SENT')),
    dedupe_key       text NOT NULL UNIQUE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    sent_at          timestamptz
);

CREATE TABLE control_plane_component (
    component         text PRIMARY KEY,
    owner             text NOT NULL,
    desired_state     text NOT NULL CHECK (desired_state IN ('ACTIVE', 'INACTIVE')),
    observed_state    text NOT NULL DEFAULT 'UNKNOWN',
    next_action       text NOT NULL DEFAULT 'PROBE',
    deadline_at       timestamptz NOT NULL,
    heartbeat_at      timestamptz,
    consecutive_failures integer NOT NULL DEFAULT 0 CHECK (consecutive_failures >= 0),
    circuit_open_until timestamptz,
    last_error        text,
    updated_at        timestamptz NOT NULL DEFAULT now()
);

REVOKE ALL ON control_plane_lane, control_plane_event, control_plane_notification,
    control_plane_component FROM PUBLIC;
