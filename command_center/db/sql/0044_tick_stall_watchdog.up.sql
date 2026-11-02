-- VOYN-W0-AICC-TICK-STALL-WATCHDOG: skip reasons become rows, stalls become
-- escalations.
--
-- The review/merge ticks are refusal-as-data everywhere except in one place:
-- the per-task SKIP reasons themselves, which until now existed only as
-- printed lines in the journal. Nothing could therefore notice the
-- pathological pattern where the SAME task repeats the SAME skip reason tick
-- after tick — every stall found live on 2026-09-07 (a verdict-less review
-- chunk on PR #774, quota-dead chunks on PR #707, a stale-head verdict on
-- PR #766) was discovered by a human running ad-hoc ssh monitor loops that
-- die with their session. Journald parsing was rejected outright: the
-- journal is an operator convenience, not a data source, and a detector
-- built on log-line shapes breaks silently the day a print statement is
-- reworded.
--
-- Two tables, both append-only:
--
-- * `tick_skip_event` — one row per (tick, task, reason), written by the
--   tick CLI handlers at exactly the point they print the SKIP lines. The
--   `tick_id` groups one CLI invocation's rows so "consecutive ticks" is a
--   property of the data, not of wall-clock guesswork.
-- * `tick_stall_escalation` — the watchdog's episode ledger. An episode is
--   identified by (tick_kind, task_id, reason, first_tick_id): the streak's
--   first tick is immovable once written, so a growing streak keeps the same
--   identity (no duplicate escalation while the stall persists) and a stall
--   that resolves and later recurs starts a new episode. The UNIQUE
--   constraint is what makes "exactly one escalation per episode" a property
--   of the schema rather than of the watchdog remembering.
--
-- Unlike the backlog store's state machine, these are plain observability
-- ledgers with no transition rules to defend, so they take the schema's
-- default app-role DML (the advisor_proposal treatment) rather than the
-- SECURITY DEFINER function idiom: there is no status a direct INSERT could
-- corrupt, and DELETE is granted to no role on any table.

CREATE TABLE tick_skip_event (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tick_id     text        NOT NULL,
    tick_kind   text        NOT NULL,
    task_id     text        NOT NULL,
    reason      text        NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tick_skip_event_tick_id_present CHECK (length(tick_id) > 0),
    CONSTRAINT tick_skip_event_kind_present CHECK (length(tick_kind) > 0),
    CONSTRAINT tick_skip_event_task_present CHECK (length(task_id) > 0),
    CONSTRAINT tick_skip_event_reason_present CHECK (length(reason) > 0)
);

-- The watchdog reads the last K ticks of one kind; both scans are covered.
CREATE INDEX idx_tick_skip_event_kind_time ON tick_skip_event(tick_kind, observed_at);
CREATE INDEX idx_tick_skip_event_kind_tick ON tick_skip_event(tick_kind, tick_id);

CREATE TABLE tick_stall_escalation (
    id                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tick_kind          text        NOT NULL,
    task_id            text        NOT NULL,
    reason             text        NOT NULL,
    first_tick_id      text        NOT NULL,
    consecutive_ticks  integer     NOT NULL,
    first_seen_at      timestamptz NOT NULL,
    last_seen_at       timestamptz NOT NULL,
    escalation_task_id text        NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tick_stall_escalation_kind_present CHECK (length(tick_kind) > 0),
    CONSTRAINT tick_stall_escalation_task_present CHECK (length(task_id) > 0),
    CONSTRAINT tick_stall_escalation_reason_present CHECK (length(reason) > 0),
    CONSTRAINT tick_stall_escalation_streak_sane CHECK (consecutive_ticks >= 1),
    CONSTRAINT tick_stall_escalation_window_sane CHECK (last_seen_at >= first_seen_at),
    -- One escalation per stall episode, enforced by the schema.
    CONSTRAINT tick_stall_episode_once UNIQUE (tick_kind, task_id, reason, first_tick_id)
);

-- PostgreSQL grants nothing on new tables by default, but state the policy
-- boundary explicitly the way 0015 does: roles.py grants the app role, and
-- nobody else reaches these rows.
REVOKE ALL ON tick_skip_event FROM PUBLIC;
REVOKE ALL ON tick_stall_escalation FROM PUBLIC;
