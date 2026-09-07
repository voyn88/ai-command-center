-- 0018: system-owned watchdog for looping review/merge ticks
-- (VOYN-W0-AICC-TICK-STALL-WATCHDOG).
--
-- The review/merge ticks (`backlog-review`, `backlog-merge`) already
-- classify every task they decline to act on with a structured reason
-- (`no_accept_marker_on_head`, `review_chunk_verdict_missing:N`,
-- `review_chunk_not_succeeded:N:dead`, `no_review_result_yet`, ...) but until
-- this migration that reason existed only as a `print()` line the CLI sent to
-- stdout -- readable in journald, invisible to any watcher that isn't a human
-- running an ad-hoc `journalctl -f | grep` session that dies with the SSH
-- session that started it. Every stall found on 2026-09-07 (PR #774, #707,
-- #766) was found exactly that way.
--
-- `tick_skip_event` is the fix for "invisible": an append-only ledger the
-- ticks themselves write to, one row per (tick, task, reason), so a watchdog
-- reads a table instead of parsing journald. Every CLI invocation ("one
-- tick") allocates a single shared ordinal via one `nextval()` on the
-- table's own identity sequence (`tick_skip_event_id_seq`, resolved through
-- `pg_get_serial_sequence` rather than a second sequence object, so no
-- second entry in `roles.IDENTITY_SEQUENCES` is needed to grant it) and
-- stamps it onto every skip that invocation records -- `review_once`,
-- `reconcile_review_once` and `publish_review_verdicts` all run inside one
-- `backlog-review` tick and must collapse into ONE tick for "N consecutive
-- ticks", not into up to three.
--
-- `tick_stall_escalation` is what makes the watchdog idempotent. An episode
-- is a maximal run of consecutive ticks in which one task was skipped for the
-- same reason; its identity is (task_id, reason, episode_start_tick_seq) --
-- the tick_seq of the OLDEST skip in that run, which cannot change while the
-- run is unbroken. The watchdog's insert is `ON CONFLICT DO NOTHING` against
-- the unique constraint below, so a second run over an episode already
-- escalated is a no-op: "one escalation per stall episode, not one per tick"
-- is a property of this constraint, not of any check the watchdog's own code
-- has to get right every time it runs.
--
-- Neither table is written through a SECURITY DEFINER function the way
-- `backlog_task` is: these are plain append-only ledgers with no status
-- machine to protect (the queue-claim idiom's reason for hiding
-- INSERT/UPDATE behind a function -- auditing a transition -- does not apply
-- to a table that never transitions anything), so `aicc_app`'s blanket
-- default DML grant (`roles.py`'s `_APP_DML`) is exactly the right amount of
-- access, the same treatment `advisor_proposal` already gets.

CREATE TABLE tick_skip_event (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    tick_seq   bigint      NOT NULL,
    tick_name  text        NOT NULL,
    task_id    text        NOT NULL,
    reason     text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tick_skip_event_tick_name_present CHECK (length(tick_name) > 0),
    CONSTRAINT tick_skip_event_task_id_present CHECK (length(task_id) > 0),
    CONSTRAINT tick_skip_event_reason_present CHECK (length(reason) > 0)
);

-- The watchdog's whole read path: "the last K skip events for task X, newest
-- first" -- a window function partitioned on task_id ordered by tick_seq
-- desc, which this index serves directly instead of a sort.
CREATE INDEX idx_tick_skip_event_task_tick ON tick_skip_event(task_id, tick_seq DESC);

CREATE TABLE tick_stall_escalation (
    id                     bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id                text        NOT NULL,
    reason                 text        NOT NULL,
    -- The oldest tick_seq in the consecutive run that crossed the threshold:
    -- the episode's identity. Stable while the run is unbroken, so a repeat
    -- run of the watchdog against the same still-unbroken run reproduces the
    -- identical tuple and is refused by the unique constraint below.
    episode_start_tick_seq bigint      NOT NULL,
    consecutive_count      integer     NOT NULL,
    escalation_task_id     text        NOT NULL,
    escalated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT tick_stall_escalation_task_id_present CHECK (length(task_id) > 0),
    CONSTRAINT tick_stall_escalation_reason_present CHECK (length(reason) > 0),
    CONSTRAINT tick_stall_escalation_count_meets_threshold CHECK (consecutive_count > 0),
    -- One escalation per stall episode: idempotency as a unique constraint,
    -- not as application logic the watchdog has to remember to apply.
    CONSTRAINT tick_stall_escalation_one_per_episode
        UNIQUE (task_id, reason, episode_start_tick_seq)
);
