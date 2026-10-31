-- AICC PostgreSQL — task queue and atomic execution-attempt claim
-- (VOYN-W0-AICC-SRV-04b, queue-claim-protocol).
--
-- =========================================================================
-- WHERE THIS LIVES, AND WHY IT DEPENDS ON NOTHING ELSE
-- =========================================================================
-- Verified placement, not assumed:
--
--   * AICC has ONE database, configured by `AICC_PG_*` and nothing else
--     (`command_center/db/config.py`). It owns the 0001 domain schema and the
--     `aicc_*` roles.
--   * `aios_db` is a LIBRARY consumed through `command_center/db/adapter.py` —
--     pool lifecycle, advisory locks, the migration runner. Its own docstring
--     draws the line: "what stays on this side of the seam is the domain: the
--     schema and its migrations, the aicc_* roles and grants, the
--     repositories". It is not a route to another database.
--   * `repo_lease` is in the AIOS PLATFORM database
--     (`aios/migrations/versions/0010_repo_lease.py`). `grep -rn repo_lease`
--     over both AICC checkouts returns nothing.
--   * `principal` now exists in the AICC database too, created by
--     `0003_worker_enrollment.up.sql` (SRV-02 landed after this file). That
--     does not reopen this file's independence: `grep -rn principal
--     command_center/db/sql/0002_queue_claim.up.sql` still returns 0 outside
--     this comment, and `work_attempt.claimed_by_role` (the join point named
--     below) has no join to `principal` anywhere in the codebase today
--     (verified for `VOYN-W0-AICC-SRV-04b-DEPENDENCY-FIRST`). AIOS separately
--     has its own tenant-scoped registry (`principals`, `auth_principals`,
--     `credentials`), unrelated to either.
--
-- The queue is AICC execution work, so it belongs in the AICC database — the
-- same database as `queue_entry`, `run` and `task`, which is what lets a claim
-- be transactional with the run records it governs.
--
-- THE IDENTITY DECISION, which is what makes this file self-contained:
--
--   THE CLAIMANT IS `session_user` — the PostgreSQL role the server itself
--   authenticated at connect time.
--
-- That is the one identity fact AICC can establish locally, with no seam, no
-- second authority, and no table that has to exist first. SCRAM established it
-- before any statement ran; a request body cannot forge it; a SECURITY DEFINER
-- function cannot launder it (which is why `session_user` and not
-- `current_user` — inside a definer function the latter is the owner).
--
-- The consequence is the one worth having: THIS PROTOCOL HAS NO DEPENDENCY ON
-- SRV-02, SRV-03 OR SRV-04a. It does not call `identity_assert()`, does not
-- read `principal`, does not read `repo_lease`. Each of those, if and when it
-- lands, joins to `work_attempt.claimed_by_role` — a join, not a dependency.
-- Measured by `tests/db/test_queue_claim.py`, whose whole suite runs against a
-- database holding 0001 and 0002 and nothing else: if the protocol needed any
-- of those three it would fail there.
--
-- =========================================================================
-- WHAT THIS IS NOT: the repository writer lease
-- =========================================================================
-- SRV-04-SCOPE separates two questions that look alike:
--
--   repo_lease (SRV-04a, in the AIOS platform)  "who may MUTATE repository R"
--   this file                                   "who is EXECUTING attempt N of W"
--
-- The claim does not and must not depend on the lease. Every acceptance clause
-- of this task — exclusivity, stale-owner rejection, SIGKILL and partition
-- recovery, durable-ack, bounded retry, DLQ — is decided by facts in this
-- database. A lease check at claim time would also be worthless: it is stale by
-- the time a git command runs. An attempt that wants to mutate a repository
-- must ADDITIONALLY hold the platform lease, verified by SRV-04a AT THE
-- MUTATION SITE, immediately before and after each mutating operation. Claim is
-- necessary and not sufficient; the protocols compose, they do not nest.
--
-- They therefore have deliberately DIFFERENT recovery policies, and that is
-- correct rather than inconsistent:
--
--   repo_lease takeover  requires PROOF OF DEATH (beacon + same-host ps).
--       A second writer on a live worktree corrupts it irreversibly.
--   queue re-delivery    requires ONLY an elapsed visibility timeout.
--       Requiring proof of death would mean a SIGKILLed worker on an
--       unreachable host blocks its task forever — the exact opposite of this
--       task's acceptance.
--
-- Re-execution is made safe not by the timeout but by the FENCE
-- (`work_item.current_attempt_id`): a superseded attempt is refused at the
-- write path whether or not anyone proved it dead.
--
-- =========================================================================
-- WHY NOT `queue_entry`
-- =========================================================================
-- `queue_entry` (0001) is a MIRROR, not an authority — `docs/AUTHORITY_MAP.md`
-- says so, ADR-0007 says so, and `command_center/db/queue_store.py`
-- `replace_entries()` implements it: `DELETE FROM queue_entry` then a bulk
-- re-INSERT of the whole list, on every sync from the authoritative
-- `data/execution_queue.json`. Claim state written there is destroyed by the
-- next sync, silently, and `aicc_worker` has no DELETE privilege so it cannot
-- even observe the loss. It also has no attempt dimension: one row per entry
-- cannot carry N attempts, per-attempt owners, retry history or a dead-letter
-- cause.
--
-- 0002 DOES NOT TOUCH `queue_entry` — no ALTER, no trigger, no grant change to
-- its shape. That is deliberate and load-bearing: `queue_entry` sits OUTSIDE
-- the SRV-07 parity gate, so any change to its shape would be a data migration
-- with no coverage. The mirror keeps mirroring until ADR-0007's contraction
-- retires both it and the JSON file.
--
-- =========================================================================
-- CONVENTIONS
-- =========================================================================
-- Follows 0001_initial.up.sql: timestamptz, jsonb payloads, singular
-- snake_case tables, `idx_<table>_<column>` after the tables, app-supplied
-- created_at/updated_at with no DEFAULT now(). Departs from 0001 in using CHECK
-- constraints: a row saying "succeeded" with no result is not a data-quality
-- nuisance, it is a lost acknowledgement.
--
-- EVERY REFUSAL RETURNS A REASON AND NEVER RAISES.
-- Measured, not assumed (VOYN-W0-AICC-AUDIT-ROLLBACK-CLASS): two probes
-- differing only in how they refuse leave 0 audit rows after RAISE and 1 after
-- RETURN, because the exception aborts the transaction that contained the audit
-- write. Reproduced on this surface by
-- `test_a_refusal_is_audited_because_it_returned_rather_than_raised`.


-- ---------------------------------------------------------------------------
-- work_item — the durable unit of work. THE authority for what is queued.
-- ---------------------------------------------------------------------------
CREATE TABLE work_item (
    work_item_id      text PRIMARY KEY,

    queue             text        NOT NULL,

    -- Enqueue-side idempotency. The dispatcher may retry an enqueue after a
    -- timeout without knowing whether the first landed; UNIQUE(queue, key)
    -- turns that into a no-op returning the existing id. Without it,
    -- at-least-once delivery of the ENQUEUE produces duplicate work that no
    -- amount of claim-side exclusivity can undo.
    idempotency_key   text        NOT NULL,

    task_id           text,

    -- OPAQUE PROVENANCE ONLY, read by NO decision in this file. Gating dispatch
    -- on it — "one in-flight item per repository" — would be a repository lease
    -- implemented in the wrong database. Asserted by
    -- `test_repository_id_is_provenance_and_gates_nothing`.
    repository_id     text,

    payload           jsonb       NOT NULL,

    priority          integer     NOT NULL,
    available_at      timestamptz NOT NULL,

    -- 'ready' | 'claimed' | 'succeeded' | 'dead'.
    -- Deliberately NO 'failed': a failed attempt either returns the item to
    -- 'ready' with a backoff or exhausts it to 'dead'. A resting 'failed' state
    -- is where queues leak work nobody retries or reaps.
    state             text        NOT NULL,

    attempt_count     integer     NOT NULL,
    max_attempts      integer     NOT NULL,
    retry_backoff_seconds integer NOT NULL,

    -- THE FENCE. The single attempt permitted to write a result. Set at claim,
    -- cleared whenever the item returns to the queue. A superseded worker is
    -- refused by comparing against this — a fact about the ITEM — so the
    -- refusal needs no clock the worker can see, no proof it died, and not even
    -- the reaper to have run.
    current_attempt_id text,

    result_id         text,
    dead_reason       text,
    dead_at           timestamptz,

    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,

    CONSTRAINT work_item_state_valid
        CHECK (state IN ('ready', 'claimed', 'succeeded', 'dead')),
    -- Makes "two live owners" unrepresentable rather than merely prevented.
    CONSTRAINT work_item_claimed_has_attempt
        CHECK ((state = 'claimed') = (current_attempt_id IS NOT NULL)),
    -- THE ACK CONSTRAINT. Succeeded implies a result, always — even against a
    -- role with direct DML. Measured by
    -- `test_a_half_acknowledged_item_is_unrepresentable`.
    CONSTRAINT work_item_succeeded_has_result
        CHECK ((state = 'succeeded') = (result_id IS NOT NULL)),
    CONSTRAINT work_item_dead_has_reason
        CHECK ((state = 'dead') = (dead_reason IS NOT NULL)),
    CONSTRAINT work_item_attempts_bounded
        CHECK (attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts),
    CONSTRAINT work_item_backoff_sane CHECK (retry_backoff_seconds >= 0),
    CONSTRAINT work_item_idempotency_key_present CHECK (length(idempotency_key) > 0)
);

CREATE UNIQUE INDEX idx_work_item_idempotency ON work_item(queue, idempotency_key);

-- The claim hot path: one partial index serving exactly the claim predicate, in
-- the claim's order. Partial so it does not carry terminal rows, which are the
-- majority of the table in steady state.
CREATE INDEX idx_work_item_claimable
    ON work_item(queue, priority DESC, available_at, created_at)
    WHERE state = 'ready';

CREATE INDEX idx_work_item_state ON work_item(state);
CREATE INDEX idx_work_item_task  ON work_item(task_id);


-- ---------------------------------------------------------------------------
-- work_attempt — one row per claim.
-- ---------------------------------------------------------------------------
CREATE TABLE work_attempt (
    attempt_id        text PRIMARY KEY,
    work_item_id      text        NOT NULL REFERENCES work_item(work_item_id),

    -- Monotonic per item. UNIQUE(work_item_id, attempt_no) is defence in depth
    -- against a double claim: two winners would have to insert the same
    -- attempt_no, and the unique index would refuse the loser even if the claim
    -- predicate were removed.
    --
    -- It is NOT what produces exclusivity today, and the concurrency tests do
    -- not pin it: making the index non-unique leaves them green, because the row
    -- lock and the predicate mean two claimers never reach the same attempt_no
    -- in the first place. What is pinned is the constraint itself —
    -- `test_the_attempt_number_is_unique_per_item` inserts a duplicate as the
    -- table owner and asserts the database refuses it. That is the honest claim:
    -- the constraint exists and works, and it is a backstop rather than the
    -- mechanism.
    attempt_no        integer     NOT NULL,

    -- THE CLAIMANT. `session_user`, written by the database, enforced by
    -- `trg_work_attempt_claimant_is_derived`. This is the column that closes
    -- VOYN-W0-AICC-AUTH-HTTP-01: today `dispatch/api.py` reads `actor` out of
    -- the POST body, so the executor names itself. Here it cannot: the value is
    -- whoever PostgreSQL authenticated, and no argument of any function in this
    -- file can influence it.
    --
    -- When an identity registry lands (SRV-02, wherever it is finally placed),
    -- it JOINS to this column on its `db_role`. It is not a dependency, and
    -- nothing here has to change to accommodate it.
    claimed_by_role   text        NOT NULL,

    -- SHA-256 hex of a 256-bit capability generated BY THE WORKER PROCESS; the
    -- preimage never reaches the database. The role says which HOST may act;
    -- the token says which PROCESS on that host holds this particular attempt,
    -- so two worker processes sharing one role cannot steal each other's work.
    -- A role with SELECT on this table, a backup, or a log cannot impersonate
    -- the holder — it would need the preimage.
    claim_token_hash  text        NOT NULL,

    -- The visibility timeout. Clamped server-side at claim; heartbeat recomputes
    -- `now() + visibility_seconds` FROM THIS ROW, so a heartbeat cannot widen
    -- its own lease. Both are server now(); no client clock is ever stored.
    visibility_seconds integer    NOT NULL,
    visible_until     timestamptz NOT NULL,
    heartbeat_at      timestamptz,

    state             text        NOT NULL,   -- active|succeeded|failed|expired
    outcome_reason    text,
    result_id         text,

    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,

    CONSTRAINT work_attempt_state_valid
        CHECK (state IN ('active', 'succeeded', 'failed', 'expired')),
    CONSTRAINT work_attempt_succeeded_has_result
        CHECK ((state = 'succeeded') = (result_id IS NOT NULL)),
    CONSTRAINT work_attempt_no_positive CHECK (attempt_no >= 1),
    CONSTRAINT work_attempt_visibility_sane CHECK (visibility_seconds >= 1),
    CONSTRAINT work_attempt_token_present CHECK (length(claim_token_hash) = 64)
);

CREATE UNIQUE INDEX idx_work_attempt_item_no ON work_attempt(work_item_id, attempt_no);

-- The reaper's predicate, partial so it scans only live claims.
CREATE INDEX idx_work_attempt_expiring
    ON work_attempt(visible_until) WHERE state = 'active';

CREATE INDEX idx_work_attempt_role ON work_attempt(claimed_by_role);

ALTER TABLE work_item
    ADD CONSTRAINT work_item_current_attempt_fk
    FOREIGN KEY (current_attempt_id) REFERENCES work_attempt(attempt_id)
    DEFERRABLE INITIALLY DEFERRED;


-- ---------------------------------------------------------------------------
-- work_result — the durable result, written in the SAME transaction that acks.
-- ---------------------------------------------------------------------------
CREATE TABLE work_result (
    result_id     text PRIMARY KEY,
    -- UNIQUE: one result per attempt, so a retried complete() cannot append a
    -- second, divergent record of what happened.
    attempt_id    text        NOT NULL UNIQUE REFERENCES work_attempt(attempt_id),
    work_item_id  text        NOT NULL REFERENCES work_item(work_item_id),
    payload       jsonb       NOT NULL,
    created_at    timestamptz NOT NULL
);

CREATE INDEX idx_work_result_item ON work_result(work_item_id);

ALTER TABLE work_item
    ADD CONSTRAINT work_item_result_fk FOREIGN KEY (result_id)
    REFERENCES work_result(result_id) DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE work_attempt
    ADD CONSTRAINT work_attempt_result_fk FOREIGN KEY (result_id)
    REFERENCES work_result(result_id) DEFERRABLE INITIALLY DEFERRED;


-- ---------------------------------------------------------------------------
-- work_event — append-only audit of every decision, INCLUDING THE REFUSALS.
-- Same shape as run_event / council_event: an identity id paired with a
-- gap-free per-parent `seq`, so a deleted row is detectable.
-- ---------------------------------------------------------------------------
CREATE TABLE work_event (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    work_item_id  text        REFERENCES work_item(work_item_id),
    -- Gap-free per item. NULL only for decisions that never resolved to an item
    -- (a claim on an empty queue, a malformed token).
    seq           integer,
    attempt_id    text,
    event         text        NOT NULL,  -- claim|heartbeat|complete|fail|expire|enqueue|redrive
    outcome       text        NOT NULL,  -- granted|rejected
    reason        text,
    actor_role    text        NOT NULL,  -- session_user, same rule as the claimant
    detail        jsonb,
    created_at    timestamptz NOT NULL,

    CONSTRAINT work_event_outcome_valid CHECK (outcome IN ('granted', 'rejected'))
);

CREATE UNIQUE INDEX idx_work_event_item_seq
    ON work_event(work_item_id, seq) WHERE work_item_id IS NOT NULL;
CREATE INDEX idx_work_event_created_at ON work_event(created_at);
CREATE INDEX idx_work_event_event ON work_event(event);


-- ---------------------------------------------------------------------------
-- Views: metadata without capabilities.
-- ---------------------------------------------------------------------------
-- `claim_token_hash` is omitted; no role is granted SELECT on work_attempt.
CREATE VIEW work_attempt_public AS
    SELECT attempt_id, work_item_id, attempt_no, claimed_by_role,
           visibility_seconds, visible_until, heartbeat_at, state,
           outcome_reason, result_id, created_at, updated_at
      FROM work_attempt;

-- THE DEAD LETTER QUEUE. Deliberately a VIEW, not a table.
--
-- A separate table would be a second authority over the same fact ("this item
-- is finished and failed"), needing its own insert path, its own consistency
-- rule against work_item.state, and a redrive that moves rows between two
-- tables non-atomically. The dead-lettered item is already the complete record:
-- terminal state, preserved cause, and every attempt that led there. The view
-- is the queue's interface; `queue_redrive()` is its exit.
CREATE VIEW work_dlq AS
    SELECT i.work_item_id, i.queue, i.task_id, i.repository_id, i.idempotency_key,
           i.payload, i.attempt_count, i.max_attempts, i.dead_reason, i.dead_at,
           i.created_at,
           (SELECT count(*) FROM work_attempt a WHERE a.work_item_id = i.work_item_id)
               AS attempts_recorded,
           (SELECT a.outcome_reason FROM work_attempt a
             WHERE a.work_item_id = i.work_item_id
             ORDER BY a.attempt_no DESC LIMIT 1) AS last_attempt_reason
      FROM work_item i
     WHERE i.state = 'dead';

CREATE VIEW work_item_public AS
    SELECT work_item_id, queue, idempotency_key, task_id, repository_id, priority,
           available_at, state, attempt_count, max_attempts, current_attempt_id,
           result_id, dead_reason, dead_at, created_at, updated_at
      FROM work_item;


-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------
-- Local id generator. 0001 has no shared one and SRV-02's `_new_id()` is a
-- proposal in another task, so depending on it would couple this migration to
-- an unlanded design for the sake of eight lines.
CREATE FUNCTION _queue_new_id(p_prefix text) RETURNS text
    LANGUAGE sql VOLATILE AS $$
    SELECT p_prefix || '_' || replace(gen_random_uuid()::text, '-', '')
$$;

CREATE FUNCTION _queue_audit(
    p_work_item_id text, p_attempt_id text, p_event text, p_outcome text,
    p_reason text, p_detail jsonb DEFAULT NULL
) RETURNS void
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    -- `max(seq)+1` is collision-free without a retry loop because every caller
    -- passing a non-null work_item_id already holds that item's row lock, taken
    -- by the FOR UPDATE or by the matching UPDATE in the same transaction.
    --
    -- `session_user` again, not an argument: the audit's actor is no more
    -- declarable than the claimant's.
    INSERT INTO work_event (work_item_id, seq, attempt_id, event, outcome, reason,
                            actor_role, detail, created_at)
    VALUES (p_work_item_id,
            CASE WHEN p_work_item_id IS NULL THEN NULL
                 ELSE coalesce((SELECT max(seq) FROM work_event
                                 WHERE work_item_id = p_work_item_id), 0) + 1 END,
            p_attempt_id, p_event, p_outcome, p_reason, session_user,
            p_detail, now());
END
$$;

-- Deterministic exponential backoff. No jitter: jitter belongs in the
-- dispatcher's poll interval, not in a stored deadline a test must predict.
CREATE FUNCTION _queue_backoff(p_base_seconds integer, p_attempt_no integer)
    RETURNS interval LANGUAGE sql IMMUTABLE AS $$
    SELECT make_interval(secs => least(
        p_base_seconds::numeric * power(2, greatest(p_attempt_no - 1, 0)), 300
    )::double precision)
$$;

CREATE TYPE queue_verdict AS (
    ok            boolean,
    reason        text,
    work_item_id  text,
    attempt_id    text,
    attempt_no    integer,
    visible_until timestamptz,
    payload       jsonb
);

-- The claimant is derived, not declared. Belt and braces over the fact that
-- `queue_claim()` is the only granted route to an INSERT here: if a future
-- migration widened the grants, this would still refuse a forged claimant.
CREATE FUNCTION work_attempt_claimant_is_derived() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public AS $$
BEGIN
    IF NEW.claimed_by_role IS DISTINCT FROM session_user THEN
        RAISE EXCEPTION 'work_attempt: claimant is derived, not declared (% <> %)',
            NEW.claimed_by_role, session_user USING ERRCODE = '28000';
    END IF;
    RETURN NEW;
END
$$;

-- INSERT only. The reaper legitimately UPDATEs another role's attempt to
-- 'expired'; firing on UPDATE would forbid exactly the recovery this protocol
-- depends on.
CREATE TRIGGER trg_work_attempt_claimant_is_derived
    BEFORE INSERT ON work_attempt
    FOR EACH ROW EXECUTE FUNCTION work_attempt_claimant_is_derived();


-- ---------------------------------------------------------------------------
-- queue_enqueue — control plane only.
-- ---------------------------------------------------------------------------
CREATE FUNCTION queue_enqueue(
    p_queue text, p_idempotency_key text, p_payload jsonb,
    p_task_id text DEFAULT NULL, p_repository_id text DEFAULT NULL,
    p_max_attempts integer DEFAULT 3, p_priority integer DEFAULT 0,
    p_delay_seconds integer DEFAULT 0, p_backoff_seconds integer DEFAULT 2
) RETURNS text
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_id text;
BEGIN
    INSERT INTO work_item (
        work_item_id, queue, idempotency_key, task_id, repository_id, payload,
        priority, available_at, state, attempt_count, max_attempts,
        retry_backoff_seconds, created_at, updated_at)
    VALUES (_queue_new_id('wki'), p_queue, p_idempotency_key, p_task_id, p_repository_id,
            coalesce(p_payload, '{}'::jsonb), p_priority,
            now() + make_interval(secs => greatest(p_delay_seconds, 0)),
            'ready', 0, greatest(p_max_attempts, 1), greatest(p_backoff_seconds, 0),
            now(), now())
    ON CONFLICT (queue, idempotency_key) DO NOTHING
    RETURNING work_item_id INTO v_id;

    IF v_id IS NULL THEN
        SELECT work_item_id INTO v_id FROM work_item
         WHERE queue = p_queue AND idempotency_key = p_idempotency_key;
        PERFORM _queue_audit(v_id, NULL, 'enqueue', 'rejected',
                             'duplicate_idempotency_key',
                             jsonb_build_object('queue', p_queue));
        RETURN v_id;
    END IF;

    PERFORM _queue_audit(v_id, NULL, 'enqueue', 'granted', NULL,
                         jsonb_build_object('queue', p_queue,
                                            'max_attempts', p_max_attempts));
    RETURN v_id;
END
$$;


-- ---------------------------------------------------------------------------
-- queue_claim — THE ATOMIC CLAIM.
-- ---------------------------------------------------------------------------
-- Note what is NOT a parameter: who is claiming. There is no actor argument to
-- pass, spoof, or forget to validate.
CREATE FUNCTION queue_claim(
    p_queue text, p_claim_token_hash text, p_visibility_seconds integer DEFAULT 60
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    it work_item%ROWTYPE;
    v  queue_verdict;
    v_attempt_id text;
    v_no  integer;
    v_vis integer;
BEGIN
    v.ok := false;

    IF p_claim_token_hash IS NULL OR length(p_claim_token_hash) <> 64 THEN
        PERFORM _queue_audit(NULL, NULL, 'claim', 'rejected', 'bad_claim_token_hash');
        v.reason := 'bad_claim_token_hash';
        RETURN v;
    END IF;

    v_vis := least(greatest(coalesce(p_visibility_seconds, 60), 1), 3600);

    -- THE MUTUAL EXCLUSION, stated as measurement rather than as intent.
    --
    -- What produces it is the ROW LOCK plus the `state = 'ready'` PREDICATE, and
    -- it is PostgreSQL's rather than this design's. Under READ COMMITTED a
    -- claimer that waits on a locked row re-evaluates the predicate once the
    -- lock is released; the winner has since set `state = 'claimed'`, so the row
    -- no longer matches and the waiter gets no row at all.
    --
    -- The predicate is the guard the suite pins: widening it to
    -- `state IN ('ready','claimed')` fails three tests, including both
    -- concurrency ones.
    --
    -- `SKIP LOCKED` BUYS THROUGHPUT, NOT CORRECTNESS — measured, and the
    -- correction of an earlier claim in this file that said otherwise. Replacing
    -- it with a plain `FOR UPDATE` leaves the whole suite green, because the
    -- re-evaluation above is what refuses the loser either way. What changes is
    -- that claimers serialise behind each other instead of stepping past a held
    -- row, so one slow claim transaction stalls every other claimer.
    -- `test_a_claimer_is_never_blocked_by_another_transactions_row_lock` pins
    -- that property, and pins it as liveness rather than dressing it up as
    -- exclusivity.
    --
    -- The two remaining guards are defence in depth, and neither is pinned by a
    -- concurrency test because neither can be made to fire while the row lock
    -- holds — see the `state = 'ready'` CAS on the UPDATE below, and
    -- UNIQUE(work_item_id, attempt_no) on `work_attempt`. Saying so is the
    -- point: a comment claiming a guard is test-covered when it is not makes a
    -- refactor that deletes the guard land green.
    SELECT * INTO it FROM work_item
     WHERE queue = p_queue AND state = 'ready' AND available_at <= now()
     ORDER BY priority DESC, available_at, created_at
     FOR UPDATE SKIP LOCKED LIMIT 1;

    IF NOT FOUND THEN
        v.reason := 'no_work';
        RETURN v;
    END IF;

    v_no := it.attempt_count + 1;

    -- Fail closed: an item that already spent its budget must never be handed
    -- out again. The failure and expiry paths dead-letter such items, so
    -- reaching here means an upstream invariant broke.
    IF v_no > it.max_attempts THEN
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               dead_reason = 'attempt_budget_exhausted', dead_at = now(),
               updated_at = now()
         WHERE work_item_id = it.work_item_id;
        PERFORM _queue_audit(it.work_item_id, NULL, 'claim', 'rejected',
                             'attempt_budget_exhausted');
        v.reason := 'attempt_budget_exhausted';
        RETURN v;
    END IF;

    v_attempt_id := _queue_new_id('wat');

    INSERT INTO work_attempt (
        attempt_id, work_item_id, attempt_no, claimed_by_role, claim_token_hash,
        visibility_seconds, visible_until, state, created_at, updated_at)
    VALUES (v_attempt_id, it.work_item_id, v_no, session_user, p_claim_token_hash,
            v_vis, now() + make_interval(secs => v_vis), 'active', now(), now());

    UPDATE work_item
       SET state = 'claimed', attempt_count = v_no,
           current_attempt_id = v_attempt_id, updated_at = now()
     WHERE work_item_id = it.work_item_id
       -- CAS, defence in depth. Unreachable today and deliberately kept: the
       -- row is locked by the SELECT above, so nothing can change its state
       -- between the two statements and this can never be false. NO TEST PINS
       -- IT, and none can without removing the lock. It is here so that a future
       -- change dropping the lock fails closed (`lost_claim_race` below)
       -- instead of handing one item to two workers.
       AND state = 'ready';

    IF NOT FOUND THEN
        PERFORM _queue_audit(it.work_item_id, v_attempt_id, 'claim', 'rejected',
                             'lost_claim_race');
        v.reason := 'lost_claim_race';
        RETURN v;
    END IF;

    PERFORM _queue_audit(it.work_item_id, v_attempt_id, 'claim', 'granted', NULL,
                         jsonb_build_object('attempt_no', v_no,
                                            'visibility_seconds', v_vis));

    v.ok := true;
    v.work_item_id := it.work_item_id;
    v.attempt_id := v_attempt_id;
    v.attempt_no := v_no;
    v.visible_until := now() + make_interval(secs => v_vis);
    v.payload := it.payload;
    RETURN v;
END
$$;


-- ---------------------------------------------------------------------------
-- _queue_owns — the single ownership test, used by every write path.
-- ---------------------------------------------------------------------------
-- Returns the refusal reason, or NULL when the caller genuinely owns the live
-- claim. Takes the ITEM's row lock first, which serialises against a concurrent
-- reaper and against a concurrent claim, so the decision is made against a
-- state nobody can change underneath it.
CREATE FUNCTION _queue_owns(
    p_attempt_id text, p_claim_token text,
    OUT reason text, OUT work_item_id text, OUT attempt_no integer
) RETURNS record
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    a work_attempt%ROWTYPE;
    i work_item%ROWTYPE;
BEGIN
    SELECT * INTO a FROM work_attempt WHERE attempt_id = p_attempt_id;
    IF NOT FOUND THEN
        reason := 'unknown_attempt';
        RETURN;
    END IF;
    work_item_id := a.work_item_id;
    attempt_no := a.attempt_no;

    SELECT * INTO i FROM work_item WHERE work_item.work_item_id = a.work_item_id FOR UPDATE;
    -- Re-read the attempt UNDER the item lock: a reaper may have expired it
    -- between the two statements.
    SELECT * INTO a FROM work_attempt WHERE attempt_id = p_attempt_id;

    -- Capability first, so a caller without the token learns nothing about the
    -- item's state from the reason it gets back.
    IF a.claim_token_hash IS DISTINCT FROM
       encode(sha256(convert_to(coalesce(p_claim_token, ''), 'UTF8')), 'hex') THEN
        reason := 'bad_claim_token';
        RETURN;
    END IF;

    -- The connected role must be the claimant. Token possession alone is not
    -- enough: a leaked token used from another host's role is refused here.
    IF a.claimed_by_role IS DISTINCT FROM session_user THEN
        reason := 'not_claimant';
        RETURN;
    END IF;

    IF a.state <> 'active' THEN
        reason := 'attempt_' || a.state;      -- attempt_expired|_failed|_succeeded
        RETURN;
    END IF;

    -- THE FENCE. The decisive test, and note what it does NOT depend on: no
    -- clock the worker can see, no proof the worker died, and not on the reaper
    -- having run yet. If the item moved on to another attempt, this caller is
    -- superseded, full stop.
    IF i.current_attempt_id IS DISTINCT FROM p_attempt_id THEN
        reason := 'attempt_superseded';
        RETURN;
    END IF;

    -- Belt and braces for the window before the reaper runs: the deadline has
    -- passed, so the claim is forfeit even though nobody has taken it yet.
    -- Without this a worker waking from a long pause could write a result in
    -- the gap between expiry and the next reap.
    IF a.visible_until <= now() THEN
        reason := 'claim_expired';
        RETURN;
    END IF;

    reason := NULL;
END
$$;


-- ---------------------------------------------------------------------------
-- queue_heartbeat
-- ---------------------------------------------------------------------------
CREATE FUNCTION queue_heartbeat(p_attempt_id text, p_claim_token text)
    RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o record; v queue_verdict;
BEGIN
    v.ok := false;
    o := _queue_owns(p_attempt_id, p_claim_token);
    IF o.reason IS NOT NULL THEN
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'heartbeat', 'rejected', o.reason);
        v.reason := o.reason;
        RETURN v;
    END IF;

    -- `now() + visibility_seconds` FROM THE ROW. The caller supplies no
    -- duration, so a compromised or buggy worker cannot extend itself past the
    -- policy its claim was granted under.
    UPDATE work_attempt
       SET visible_until = now() + make_interval(secs => visibility_seconds),
           heartbeat_at = now(), updated_at = now()
     WHERE attempt_id = p_attempt_id
    RETURNING visible_until INTO v.visible_until;

    PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'heartbeat', 'granted', NULL);
    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;


-- ---------------------------------------------------------------------------
-- queue_complete — DURABLE RESULT AND ACKNOWLEDGEMENT, INSEPARABLY.
-- ---------------------------------------------------------------------------
-- There is no `queue_ack()`. The result is a REQUIRED argument of the only
-- function that can move an item to 'succeeded', and both writes happen in this
-- one transaction. The window "acknowledged but the result was lost" therefore
-- does not exist to be closed: it is not expressible through the granted API,
-- and `work_item_succeeded_has_result` blocks it even for a role with direct
-- DML. A crash at any instant commits both or neither —
-- `test_a_sigkilled_worker_exposes_no_terminal_state_before_it_is_durable`
-- measures it by SIGKILLing a worker mid-transaction.
CREATE FUNCTION queue_complete(p_attempt_id text, p_claim_token text, p_result jsonb)
    RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o record; v queue_verdict; v_result_id text;
BEGIN
    v.ok := false;
    IF p_result IS NULL THEN
        v.reason := 'result_required';
        RETURN v;
    END IF;

    o := _queue_owns(p_attempt_id, p_claim_token);
    IF o.reason IS NOT NULL THEN
        -- THE STALE-OWNER REFUSAL. A worker whose visibility timeout elapsed and
        -- whose item was re-claimed lands here with 'attempt_superseded', and
        -- its result is not written. Audited, because it returns.
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'complete', 'rejected', o.reason);
        v.reason := o.reason;
        RETURN v;
    END IF;

    v_result_id := _queue_new_id('wrs');

    INSERT INTO work_result (result_id, attempt_id, work_item_id, payload, created_at)
    VALUES (v_result_id, p_attempt_id, o.work_item_id, p_result, now());

    UPDATE work_attempt SET state = 'succeeded', result_id = v_result_id,
           updated_at = now()
     WHERE attempt_id = p_attempt_id;

    UPDATE work_item SET state = 'succeeded', result_id = v_result_id,
           current_attempt_id = NULL, updated_at = now()
     WHERE work_item_id = o.work_item_id;

    PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'complete', 'granted', NULL,
                         jsonb_build_object('result_id', v_result_id));
    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;


-- ---------------------------------------------------------------------------
-- queue_fail — the worker reports its own failure. BOUNDED RETRY / DLQ.
-- ---------------------------------------------------------------------------
CREATE FUNCTION queue_fail(
    p_attempt_id text, p_claim_token text, p_reason text,
    p_retryable boolean DEFAULT true
) RETURNS queue_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE o record; v queue_verdict; i work_item%ROWTYPE;
BEGIN
    v.ok := false;
    o := _queue_owns(p_attempt_id, p_claim_token);
    IF o.reason IS NOT NULL THEN
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'rejected', o.reason);
        v.reason := o.reason;
        RETURN v;
    END IF;

    UPDATE work_attempt SET state = 'failed', outcome_reason = p_reason, updated_at = now()
     WHERE attempt_id = p_attempt_id;

    SELECT * INTO i FROM work_item WHERE work_item_id = o.work_item_id;

    IF NOT p_retryable OR i.attempt_count >= i.max_attempts THEN
        -- DEAD LETTER. The cause is preserved on the item and every attempt
        -- that led there is preserved beside it; `work_dlq` exposes both.
        UPDATE work_item
           SET state = 'dead', current_attempt_id = NULL,
               dead_reason = CASE WHEN NOT p_retryable
                    THEN 'non_retryable: ' || coalesce(p_reason, 'unspecified')
                    ELSE 'max_attempts_exhausted: ' || coalesce(p_reason, 'unspecified') END,
               dead_at = now(), updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted',
                             'dead_lettered',
                             jsonb_build_object('attempt_count', i.attempt_count,
                                                'max_attempts', i.max_attempts,
                                                'retryable', p_retryable));
        v.reason := 'dead_lettered';
    ELSE
        UPDATE work_item
           SET state = 'ready', current_attempt_id = NULL,
               available_at = now() + _queue_backoff(i.retry_backoff_seconds, i.attempt_count),
               updated_at = now()
         WHERE work_item_id = o.work_item_id;
        PERFORM _queue_audit(o.work_item_id, p_attempt_id, 'fail', 'granted', 'requeued',
                             jsonb_build_object('attempt_count', i.attempt_count,
                                                'max_attempts', i.max_attempts));
        v.reason := 'requeued';
    END IF;

    v.ok := true;
    v.work_item_id := o.work_item_id;
    v.attempt_id := p_attempt_id;
    v.attempt_no := o.attempt_no;
    RETURN v;
END
$$;


-- ---------------------------------------------------------------------------
-- queue_reap — AUTOMATIC RECOVERY, and the only thing that runs after a
-- SIGKILL or a partition. A scheduled job, not a human.
-- ---------------------------------------------------------------------------
-- Deliberately requires NO proof that the worker died — see the header. It takes
-- each item's row lock, so it cannot race a concurrent complete(): either the
-- completion commits first and the attempt is no longer 'active', or the reap
-- commits first and the completion is refused 'attempt_superseded'.
CREATE FUNCTION queue_reap() RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE r record; i work_item%ROWTYPE; n integer := 0;
BEGIN
    FOR r IN
        SELECT a.attempt_id, a.work_item_id FROM work_attempt a
         WHERE a.state = 'active' AND a.visible_until <= now()
         ORDER BY a.visible_until
    LOOP
        SELECT * INTO i FROM work_item WHERE work_item_id = r.work_item_id FOR UPDATE;

        -- Re-test under the lock: a completion may have landed since the scan.
        CONTINUE WHEN NOT EXISTS (
            SELECT 1 FROM work_attempt WHERE attempt_id = r.attempt_id AND state = 'active');

        UPDATE work_attempt SET state = 'expired', outcome_reason = 'visibility_timeout',
               updated_at = now()
         WHERE attempt_id = r.attempt_id;

        IF i.attempt_count >= i.max_attempts THEN
            UPDATE work_item
               SET state = 'dead', current_attempt_id = NULL,
                   dead_reason = 'visibility_timeout_exhausted',
                   dead_at = now(), updated_at = now()
             WHERE work_item_id = r.work_item_id;
            PERFORM _queue_audit(r.work_item_id, r.attempt_id, 'expire', 'granted',
                                 'dead_lettered');
        ELSE
            UPDATE work_item
               SET state = 'ready', current_attempt_id = NULL,
                   available_at = now() + _queue_backoff(i.retry_backoff_seconds,
                                                         i.attempt_count),
                   updated_at = now()
             WHERE work_item_id = r.work_item_id;
            PERFORM _queue_audit(r.work_item_id, r.attempt_id, 'expire', 'granted',
                                 'requeued');
        END IF;
        n := n + 1;
    END LOOP;
    RETURN n;
END
$$;


-- ---------------------------------------------------------------------------
-- queue_redrive — the DLQ's exit. Control plane / operator, audited.
-- ---------------------------------------------------------------------------
CREATE FUNCTION queue_redrive(p_work_item_id text, p_extra_attempts integer DEFAULT 1)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE i work_item%ROWTYPE;
BEGIN
    SELECT * INTO i FROM work_item WHERE work_item_id = p_work_item_id FOR UPDATE;
    IF NOT FOUND THEN
        -- The audit's `work_item_id` is a foreign key, so naming an item that
        -- does not exist would make the AUDIT raise — turning a refusal into an
        -- aborted transaction and losing the very record of it. The unknown id
        -- goes into `detail` instead, where nothing references it.
        PERFORM _queue_audit(NULL, NULL, 'redrive', 'rejected', 'unknown_work_item',
                             jsonb_build_object('requested_work_item_id', p_work_item_id));
        RETURN false;
    END IF;
    IF i.state <> 'dead' THEN
        PERFORM _queue_audit(p_work_item_id, NULL, 'redrive', 'rejected', 'not_dead_lettered',
                             jsonb_build_object('state', i.state));
        RETURN false;
    END IF;
    -- The attempt history is NOT reset. `attempt_count` keeps counting and the
    -- budget is widened explicitly, so a redrive loop cannot silently grant
    -- infinite retries — each widening is a recorded act.
    UPDATE work_item
       SET state = 'ready', dead_reason = NULL, dead_at = NULL,
           max_attempts = max_attempts + greatest(p_extra_attempts, 1),
           available_at = now(), updated_at = now()
     WHERE work_item_id = p_work_item_id;
    PERFORM _queue_audit(p_work_item_id, NULL, 'redrive', 'granted', NULL,
                         jsonb_build_object('extra_attempts', greatest(p_extra_attempts, 1),
                                            'previous_dead_reason', i.dead_reason));
    RETURN true;
END
$$;
