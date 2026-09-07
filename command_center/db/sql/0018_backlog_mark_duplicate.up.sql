-- Collapse a REAL, already-OPEN duplicate into DECIDED
-- (VOYN-W0-AICC-BGE-M3-DEDUP-SCAN).
--
-- bge-m3 embeddings of the 346 live findings (OPEN/IN_PROGRESS/DEFER_TO_USER)
-- turned up 34 candidate pairs above cosine 0.75; manual review over the top
-- pairs confirmed real duplicates above ~0.78 (e.g.
-- VOYN-W0-AICC-ISO-NOW-NAIVE-LOCAL / VOYN-W0-AICC-TZ-AWARE-TIMESTAMPS,
-- VOYN-W0-AICC-SCHEMA-VERSION-DRIFT / VOYN-W0-AICC-SQLITE-SCHEMA-16-TO-23).
-- Collapsing any of them hit the same class of gap `backlog_triage` (0008)
-- closed for UNTRIAGED: the status machine has no legal path for a task that
-- is already OPEN and turns out to be a duplicate of another task. Triage's
-- own 'duplicate' decision only fires from UNTRIAGED and records the
-- canonical as free text in an optional audit detail — good enough for a
-- raw, unvetted finding, not for a decision meant to be queried back
-- ("what superseded this?") rather than merely read once from the log.
--
-- backlog_mark_duplicate(task, canonical, detail) is the OPEN -> DECIDED
-- counterpart: the canonical is a required, FK-checked reference to another
-- live backlog_task row, persisted in its own table rather than only inside
-- jsonb prose, so "what is this superseded by" is a join, not a grep.
CREATE TABLE backlog_duplicate (
    task_id            text PRIMARY KEY REFERENCES backlog_task(task_id),
    canonical_task_id  text NOT NULL REFERENCES backlog_task(task_id),
    detail             text,
    recorded_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT backlog_duplicate_not_self CHECK (task_id <> canonical_task_id)
);

CREATE FUNCTION backlog_mark_duplicate(
    p_task_id           text,
    p_canonical_task_id text,
    p_detail            text DEFAULT NULL
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE
    t backlog_task%ROWTYPE;
    v backlog_verdict;
BEGIN
    v.ok := false;
    IF p_canonical_task_id IS NULL OR length(p_canonical_task_id) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected',
                               'canonical_required');
        v.reason := 'canonical_required';
        RETURN v;
    END IF;
    IF p_task_id = p_canonical_task_id THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected', 'self_duplicate');
        v.reason := 'self_duplicate';
        RETURN v;
    END IF;

    -- Lock both endpoints (deterministic order), the same idiom
    -- backlog_add_dependency uses, so a concurrent mutation of the canonical
    -- cannot race this decision.
    PERFORM 1 FROM backlog_task b WHERE b.task_id IN (p_task_id, p_canonical_task_id)
     ORDER BY b.task_id FOR UPDATE;

    SELECT * INTO t FROM backlog_task WHERE task_id = p_task_id;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'mark_duplicate', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM backlog_task WHERE task_id = p_canonical_task_id) THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected', 'unknown_canonical',
                               jsonb_build_object('canonical_task_id', p_canonical_task_id));
        v.reason := 'unknown_canonical'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF t.kind = 'gate' THEN
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected',
                               'gate_is_control_record');
        v.reason := 'gate_is_control_record'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF t.status <> 'OPEN' THEN
        -- The gap this closes is specifically OPEN -> DECIDED; anything else
        -- (UNTRIAGED) already has a legal route through backlog_triage.
        PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'rejected', 'not_open',
                               jsonb_build_object('status', t.status));
        v.reason := 'not_open: ' || t.status; v.revision := t.revision;
        RETURN v;
    END IF;

    INSERT INTO backlog_duplicate (task_id, canonical_task_id, detail)
    VALUES (p_task_id, p_canonical_task_id, p_detail);

    UPDATE backlog_task
       SET status = 'DECIDED', revision = revision + 1, updated_at = clock_timestamp()
     WHERE task_id = p_task_id;
    PERFORM _backlog_audit(p_task_id, 'mark_duplicate', 'granted', 'duplicate',
                           jsonb_build_object('canonical_task_id', p_canonical_task_id,
                                              'detail', p_detail));
    v.ok := true; v.reason := 'DECIDED'; v.revision := t.revision + 1;
    RETURN v;
END
$$;

REVOKE ALL ON FUNCTION backlog_mark_duplicate(text, text, text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION backlog_mark_duplicate(text, text, text) TO aicc_app;
