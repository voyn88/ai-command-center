-- VOYN-W0-AICC-BGE-M3-DEDUP-SCAN — the legal exit for a duplicate that is
-- already OPEN.
--
-- The dedup scan that opened this gap was objective, not a judgement call:
-- `bge-m3` embeddings of the 346 live tasks (OPEN / IN_PROGRESS /
-- DEFER_TO_USER), pairwise cosine similarity, threshold 0.75, 34 candidate
-- pairs. Manual review of the top pairs confirmed everything above 0.78 as a
-- real duplicate — `VOYN-W0-AICC-ISO-NOW-NAIVE-LOCAL` and
-- `VOYN-W0-AICC-TZ-AWARE-TIMESTAMPS` are one finding written twice (the second
-- was filed precisely because it cited the first as non-existent), and so are
-- `VOYN-W0-AICC-SCHEMA-VERSION-DRIFT` and
-- `VOYN-W0-AICC-SQLITE-SCHEMA-16-TO-23`, which describe the same 16-vs-23
-- drift. Every one of them is OPEN.
--
-- And there was no way to close them. `backlog_triage` (0008) decides
-- 'duplicate' only from UNTRIAGED; the linear machine (`backlog_transition`)
-- runs OPEN -> IN_PROGRESS -> READY_TO_REVIEW -> DONE and has no branch to
-- DECIDED at all. So a confirmed duplicate sitting in OPEN had exactly two
-- fates: get dispatched to the fleet to re-deliver work already delivered, or
-- stay OPEN forever. That is the gap UNTRIAGED had before 0008, one state
-- over, and this is the same shape of seam.
--
-- Deliberately narrow, in the same three ways 0008 is:
--
-- * **Only from OPEN.** Not from IN_PROGRESS (a run holds the lease), not from
--   READY_TO_REVIEW (a PR is in flight), not from UNTRIAGED (that is
--   `backlog_triage`'s decision and it already exists). One state in, one
--   state out.
-- * **The canonical task is mandatory and must exist.** "Duplicate" is a
--   claim about another row; without naming it, DECIDED is just a quieter way
--   of deleting a finding. The reference is a foreign key in a table, not a
--   sentence in an audit detail, so the claim stays queryable and the canonical
--   cannot later vanish out from under it.
-- * **Refusals are data.** Every rejection returns a `backlog_verdict` and
--   writes a `backlog_event`; nothing here raises.
--
-- The last point is why the validation order below is what it is, and the
-- order is load-bearing rather than cosmetic. `backlog_event.task_id` is a
-- foreign key to `backlog_task`, so auditing a rejection *against* a task id
-- that does not exist raises a foreign-key violation — the caller gets an
-- aborted transaction instead of the `ok=false` verdict this function promises,
-- and the audit row the refusal was supposed to leave behind is lost with it.
-- A first cut of this function checked the arguments' shape (canonical missing,
-- canonical equal to the subject) BEFORE confirming the subject existed, and
-- both of those branches audit with `p_task_id`: automation retrying with a
-- stale or mistyped id would have crashed rather than been refused
-- (adversarial review of 69267384, VOYN-W0-AICC-BGE-M3-DEDUP-SCAN-REM).
-- Existence is therefore established first, and only after it is every
-- subsequent refusal free to name the task; the one refusal that cannot —
-- `unknown_task` itself — audits with NULL and carries the requested id in the
-- detail, exactly as `backlog_transition` does.

-- One row per superseded task, and the row IS the decision: which task is
-- retired, which one carries the work now, and when it was decided. Separate
-- from `backlog_task` rather than a nullable column on it because the fact is
-- about a PAIR — the FK on `canonical_task_id` is what makes "the canonical
-- exists" a property of the schema, and a self-referencing column on the task
-- row could not carry it without inviting the row to point at itself.
CREATE TABLE backlog_duplicate (
    task_id           text PRIMARY KEY REFERENCES backlog_task(task_id),
    canonical_task_id text        NOT NULL REFERENCES backlog_task(task_id),
    detail            text,
    recorded_at       timestamptz NOT NULL DEFAULT now(),
    -- The function refuses a self-duplicate as data; this is the same rule at
    -- the storage layer, so no future writer can record the degenerate pair.
    CONSTRAINT backlog_duplicate_not_self CHECK (task_id <> canonical_task_id)
);

-- "What else was folded into this task?" is the question an operator asks, and
-- the primary key answers only the other direction.
CREATE INDEX idx_backlog_duplicate_canonical
    ON backlog_duplicate (canonical_task_id);

CREATE FUNCTION backlog_mark_duplicate(
    p_task_id           text,
    p_canonical_task_id text,
    p_detail            text DEFAULT NULL
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    t backlog_task%ROWTYPE;
    v backlog_verdict;
    v_canonical text;
    v_canonical_status text;
    v_canonical_head text;
    v_dependents text[];
BEGIN
    v.ok := false;
    v_canonical := nullif(btrim(p_canonical_task_id), '');

    -- Lock both endpoints in a deterministic order (`backlog_add_dependency`'s
    -- idiom) so two callers marking A duplicate-of-B and B duplicate-of-A at
    -- the same moment cannot deadlock, and neither can pass the chain check
    -- below against a snapshot that is missing the other's write.
    PERFORM 1 FROM backlog_task b
      WHERE b.task_id IN (p_task_id, v_canonical)
      ORDER BY b.task_id FOR UPDATE;

    -- Existence FIRST. Everything after this point may audit with p_task_id;
    -- nothing before it may, because backlog_event.task_id is a foreign key.
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'mark_duplicate', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id,
                                                  'requested_canonical', p_canonical_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;

    IF t.kind = 'gate' THEN
        -- A gate is a control record; it closes through its own acceptance act,
        -- which is the rule backlog_transition enforces. Retiring one as a
        -- duplicate would be that decision made by the back door.
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected',
                               'gate_is_control_record');
        v.reason := 'gate_is_control_record'; v.revision := t.revision;
        RETURN v;
    END IF;

    IF t.status <> 'OPEN' THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected', 'not_open',
                               jsonb_build_object('status', t.status));
        v.reason := 'not_open: ' || t.status; v.revision := t.revision;
        RETURN v;
    END IF;

    IF v_canonical IS NULL THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected',
                               'canonical_required');
        v.reason := 'canonical_required'; v.revision := t.revision;
        RETURN v;
    END IF;

    IF v_canonical = p_task_id THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected',
                               'self_duplicate');
        v.reason := 'self_duplicate'; v.revision := t.revision;
        RETURN v;
    END IF;

    SELECT b.status INTO v_canonical_status
      FROM backlog_task b WHERE b.task_id = v_canonical;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected',
                               'unknown_canonical',
                               jsonb_build_object('canonical', v_canonical));
        v.reason := 'unknown_canonical'; v.revision := t.revision;
        RETURN v;
    END IF;

    -- One hop, never a chain. If the named canonical has itself been retired,
    -- the pointer would lead to a DECIDED row rather than to the work, so the
    -- refusal names the actual head and the caller can retry against it.
    SELECT d.canonical_task_id INTO v_canonical_head
      FROM backlog_duplicate d WHERE d.task_id = v_canonical;
    IF FOUND THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected',
                               'canonical_is_a_duplicate',
                               jsonb_build_object('canonical', v_canonical,
                                                  'head', v_canonical_head));
        v.reason := 'canonical_is_a_duplicate: ' || v_canonical_head;
        v.revision := t.revision;
        RETURN v;
    END IF;

    -- Two things this function deliberately does NOT constrain, both recorded
    -- so the decision stays inspectable rather than implicit:
    --
    -- * **The canonical's own status.** "The work is already delivered over
    --   there" is a legitimate reason to retire a duplicate, so a DONE or
    --   IN_PROGRESS canonical is allowed; the status it had at the moment of
    --   the decision goes into the audit detail below, because a canonical
    --   that was DONE and a canonical that was OPEN make the same row here and
    --   are different decisions.
    -- * **Tasks that depend on the one being retired.** `backlog_eligible`
    --   requires every dependency to be DONE, and DECIDED is not DONE, so a
    --   dependent of a retired task stops being dispatchable. That is a
    --   pre-existing property of the store rather than something introduced
    --   here — `backlog_triage(..., 'duplicate')` has moved UNTRIAGED tasks
    --   with dependents to DECIDED since 0008 — and re-pointing dependency
    --   edges at the canonical is a cycle-checked act that belongs with
    --   `backlog_add_dependency`, not inside this one. What changes here is
    --   that it is no longer silent: the dependents are named in the audit, so
    --   "this task went quiet when its dependency was retired" is answerable
    --   from the event trail instead of by reading the dependency graph.
    SELECT array_agg(d.task_id ORDER BY d.task_id) INTO v_dependents
      FROM backlog_dependency d WHERE d.depends_on_task_id = p_task_id;

    -- ON CONFLICT rather than a plain INSERT: the importer's upsert can move a
    -- task's status, so a row retired once can legitimately come back to OPEN
    -- and be retired again against a different canonical. The audit keeps both
    -- decisions; this table keeps the current one.
    INSERT INTO backlog_duplicate (task_id, canonical_task_id, detail)
    VALUES (p_task_id, v_canonical, p_detail)
    ON CONFLICT (task_id) DO UPDATE
        SET canonical_task_id = EXCLUDED.canonical_task_id,
            detail            = EXCLUDED.detail,
            recorded_at       = now();

    UPDATE backlog_task b
       SET status = 'DECIDED', revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'granted', v_canonical,
                           jsonb_build_object('canonical', v_canonical,
                                              'canonical_status', v_canonical_status,
                                              'from', t.status,
                                              'to', 'DECIDED',
                                              'dependents', to_jsonb(coalesce(v_dependents, '{}'::text[])),
                                              'detail', p_detail));
    v.ok := true; v.reason := 'DECIDED';
    RETURN v;
END
$$;

REVOKE ALL ON FUNCTION backlog_mark_duplicate(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION backlog_mark_duplicate(text, text, text) TO aicc_app;
GRANT SELECT ON backlog_duplicate TO aicc_app;
