-- VOYN-W0-AICC-SRV-07d-STALL: the pr_evidence tick now reads its own skip
-- streak (tick_skip_event) to find a task that has gone `no_open_pr_on_task_
-- branch` for threshold-or-more consecutive ticks -- the same "actionable
-- stall" signal the watchdog already computes -- so it can self-heal through
-- the 0018 recovery path instead of repeating the identical skip forever.
-- `idx_tick_skip_event_kind_tick` only serves lookup by tick_id; the new read
-- is keyed by (tick_kind, task_id, reason) instead.
CREATE INDEX idx_tick_skip_event_pair_latest
    ON tick_skip_event (tick_kind, task_id, reason, id DESC);
