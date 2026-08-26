-- AICC PostgreSQL — the structured backlog store (VOYN-W0-BACKLOG-ORCHESTRATOR
-- BO-S1, structured-store).
--
-- =========================================================================
-- WHAT THIS IS, AND WHAT IT DELIBERATELY IS NOT
-- =========================================================================
-- The canonical backlog stops being a Markdown file a coordinator re-reads
-- and becomes a transactional store: tasks, waves, priorities, dependencies,
-- writer leases and acceptance evidence, with STATUS TRANSITIONS enforced by
-- functions instead of by prompt discipline. The Markdown file becomes a
-- projection (BO-S4); until then the importer (backlog_store.py) reconciles
-- the file into these tables idempotently.
--
-- It is NOT a queue: execution travels through work_item (0002). BO-S2 will
-- read this store and enqueue; nothing here dispatches.
--
-- Design decisions, each traceable:
--
-- * **Machine values, not substrings.** `wave` is the exact normalized token
--   ('0', '0.5', '1', ...) — 'Wave 0' and 'Wave 0.5' are distinct values and
--   the CHECK refuses anything that is not the canonical shape. Status is a
--   closed vocabulary; UNTRIAGED / DEFER_TO_USER / SPLIT / NEEDS_REFINEMENT
--   are real states but OUTSIDE the executable model: the transition
--   function moves only along OPEN -> IN_PROGRESS -> READY_TO_REVIEW ->
--   DONE, one step at a time.
-- * **Gates are control records, not executable tasks** (the recorded
--   classification invariant). A `-G<n>` id is stored with kind='gate' and
--   the transition function refuses it; a gate closes through its own
--   acceptance act, recorded as evidence.
-- * **Writes go through SECURITY DEFINER functions only.** No role holds
--   INSERT/UPDATE on these tables (the queue-claim idiom): every state
--   change audits, and OPEN -> DONE is not merely discouraged — there is no
--   SQL path that performs it.
-- * **Refusals are data.** Functions return verdict rows; an illegal
--   transition, a stale revision, a dependency cycle (with its path) come
--   back as (ok=false, reason), never as an exception that aborts the
--   caller's transaction and loses the audit row.
-- * **The lease is the voyn_coordination.writer_lease semantics implemented
--   in THIS schema** — that database is another installation's authority,
--   and AIOS's repo_lease (0010) documents why a lease table constrains row
--   shape, not lease meaning: canonical authority naming stays with the
--   caller. One row per authority, owner + heartbeat + expiry, takeover only
--   through proven expiry.

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

CREATE TABLE backlog_task (
    task_id     text PRIMARY KEY,
    wave        text        NOT NULL,
    priority    text,
    status      text        NOT NULL,
    kind        text        NOT NULL DEFAULT 'task',
    title       text        NOT NULL,
    body        text        NOT NULL DEFAULT '',
    repo        text,
    -- Optimistic lock. Every accepted mutation increments it; a caller that
    -- read revision N and writes against N+1 is refused, not overwritten.
    revision    bigint      NOT NULL DEFAULT 1,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT backlog_task_id_shape CHECK (task_id ~ '^VOYN-[A-Za-z0-9][A-Za-z0-9._-]*$'),
    -- Numeric waves order execution. The named tokens are the file's parallel
    -- lanes and idea pools, closed and exactly as observed in the canon:
    -- 'W1'/'W7' are idea pools for FUTURE waves and deliberately distinct
    -- from waves '1'/'7' (the same distinctness rule that separates W0 from
    -- W00); 'P1'/'P0.5' are lane names in the idea sections, not priorities.
    CONSTRAINT backlog_task_wave_shape CHECK (
        wave ~ '^[0-9]+(\.[0-9]+)?$'
        OR wave IN ('COM', 'WOW', 'AICOS', 'W1', 'W7', 'P1', 'P0.5')),
    CONSTRAINT backlog_task_priority_shape CHECK (priority IS NULL OR priority ~ '^P[0-9]$'),
    -- The executable four plus the observed non-executable states; DECIDED
    -- is the file's status for an accepted architecture-decision record.
    CONSTRAINT backlog_task_status_vocabulary CHECK (status IN
        ('OPEN', 'IN_PROGRESS', 'READY_TO_REVIEW', 'DONE', 'UNTRIAGED',
         'DEFER_TO_USER', 'SPLIT', 'NEEDS_REFINEMENT', 'DECIDED')),
    CONSTRAINT backlog_task_kind_vocabulary CHECK (kind IN ('task', 'gate')),
    CONSTRAINT backlog_task_revision_positive CHECK (revision >= 1),
    CONSTRAINT backlog_task_title_present CHECK (length(title) > 0)
);

CREATE INDEX idx_backlog_task_wave_status ON backlog_task(wave, status);

CREATE TABLE backlog_dependency (
    task_id            text NOT NULL REFERENCES backlog_task(task_id),
    depends_on_task_id text NOT NULL REFERENCES backlog_task(task_id),
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, depends_on_task_id),
    CONSTRAINT backlog_dependency_not_self CHECK (task_id <> depends_on_task_id)
);

CREATE INDEX idx_backlog_dependency_reverse ON backlog_dependency(depends_on_task_id);

CREATE TABLE backlog_writer_lease (
    authority    text PRIMARY KEY,
    owner        text        NOT NULL,
    acquired_at  timestamptz NOT NULL DEFAULT now(),
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    expires_at   timestamptz NOT NULL,
    CONSTRAINT backlog_lease_authority_present CHECK (length(authority) > 0),
    CONSTRAINT backlog_lease_owner_present CHECK (length(owner) > 0),
    -- A deadline at or before its own heartbeat is the takeover-race failure
    -- repo_lease's review demonstrated; refuse the row shape outright.
    CONSTRAINT backlog_lease_deadline_sane CHECK (expires_at > heartbeat_at)
);

CREATE TABLE backlog_evidence (
    evidence_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id     text NOT NULL REFERENCES backlog_task(task_id),
    kind        text NOT NULL,
    value       text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT backlog_evidence_kind_vocabulary CHECK (kind IN ('pr', 'sha', 'ci', 'acceptance')),
    CONSTRAINT backlog_evidence_value_present CHECK (length(value) > 0),
    -- Idempotent recording: the same fact recorded twice is one row.
    CONSTRAINT backlog_evidence_unique UNIQUE (task_id, kind, value)
);

-- Append-only audit, refusals included — the work_event idiom.
CREATE TABLE backlog_event (
    event_id   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task_id    text REFERENCES backlog_task(task_id),
    event      text NOT NULL,
    outcome    text NOT NULL,
    reason     text,
    actor      text NOT NULL,
    detail     jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT backlog_event_outcome_vocabulary CHECK (outcome IN ('granted', 'rejected'))
);

-- ---------------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------------

CREATE FUNCTION _backlog_audit(
    p_task_id text, p_event text, p_outcome text, p_reason text,
    p_detail jsonb DEFAULT NULL
) RETURNS void LANGUAGE sql AS $$
    INSERT INTO backlog_event (task_id, event, outcome, reason, actor, detail)
    VALUES (p_task_id, p_event, p_outcome, p_reason, session_user, p_detail);
$$;

CREATE TYPE backlog_verdict AS (
    ok       boolean,
    reason   text,
    revision bigint
);

CREATE TYPE backlog_dependency_verdict AS (
    ok     boolean,
    reason text,
    path   text[]
);

CREATE TYPE backlog_lease_verdict AS (
    ok         boolean,
    reason     text,
    owner      text,
    expires_at timestamptz
);

-- ---------------------------------------------------------------------------
-- backlog_upsert_task — the importer's reconciliation path.
-- ---------------------------------------------------------------------------
-- May set any status DIRECTLY, on purpose and only here: during the
-- migration period the Markdown file is the incumbent authority, and
-- reconciling its current truth is ingest, not a transition. Post-import
-- mutation goes through backlog_transition. Idempotence is measurable: an
-- upsert that changes nothing reports changed=false and does not touch
-- revision or updated_at, so "second run = 0 changes" is a query, not a hope.
CREATE FUNCTION backlog_upsert_task(
    p_task_id text, p_wave text, p_priority text, p_status text,
    p_kind text, p_title text, p_body text, p_repo text
) RETURNS TABLE (ok boolean, reason text, changed boolean, revision bigint)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE;
BEGIN
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        BEGIN
            INSERT INTO backlog_task (task_id, wave, priority, status, kind, title, body, repo)
            VALUES (p_task_id, p_wave, p_priority, p_status, coalesce(p_kind, 'task'),
                    p_title, coalesce(p_body, ''), p_repo)
            RETURNING backlog_task.revision INTO revision;
        EXCEPTION
            WHEN check_violation THEN
                PERFORM _backlog_audit(NULL, 'upsert', 'rejected', 'constraint: ' || SQLERRM,
                                       jsonb_build_object('requested_task_id', p_task_id));
                RETURN QUERY SELECT false, 'constraint: ' || SQLERRM, false, NULL::bigint;
                RETURN;
            WHEN unique_violation THEN
                -- Two importers racing the same NEW id: one row exists now;
                -- the caller re-runs and reconciles against it.
                RETURN QUERY SELECT false, 'concurrent_insert', false, NULL::bigint;
                RETURN;
        END;
        PERFORM _backlog_audit(p_task_id, 'upsert', 'granted', 'inserted');
        RETURN QUERY SELECT true, 'inserted', true, revision;
        RETURN;
    END IF;

    IF t.wave = p_wave AND t.priority IS NOT DISTINCT FROM p_priority
       AND t.status = p_status AND t.kind = coalesce(p_kind, 'task')
       AND t.title = p_title AND t.body = coalesce(p_body, '')
       AND t.repo IS NOT DISTINCT FROM p_repo THEN
        RETURN QUERY SELECT true, 'unchanged', false, t.revision;
        RETURN;
    END IF;

    BEGIN
        UPDATE backlog_task b
           SET wave = p_wave, priority = p_priority, status = p_status,
               kind = coalesce(p_kind, 'task'), title = p_title,
               body = coalesce(p_body, ''), repo = p_repo,
               revision = b.revision + 1, updated_at = now()
         WHERE b.task_id = p_task_id
        RETURNING b.revision INTO revision;
    EXCEPTION WHEN check_violation THEN
        PERFORM _backlog_audit(p_task_id, 'upsert', 'rejected', 'constraint: ' || SQLERRM);
        RETURN QUERY SELECT false, 'constraint: ' || SQLERRM, false, t.revision;
        RETURN;
    END;
    PERFORM _backlog_audit(p_task_id, 'upsert', 'granted', 'updated');
    RETURN QUERY SELECT true, 'updated', true, revision;
END
$$;

-- ---------------------------------------------------------------------------
-- backlog_transition — THE machine status model. One step, no skips.
-- ---------------------------------------------------------------------------
CREATE FUNCTION backlog_transition(
    p_task_id text, p_to_status text, p_expected_revision bigint
) RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict; v_allowed text;
        v_pr integer; v_sha integer;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'transition', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF t.kind = 'gate' THEN
        PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'gate_is_control_record');
        v.reason := 'gate_is_control_record'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF t.revision <> p_expected_revision THEN
        PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'revision_conflict',
                               jsonb_build_object('expected', p_expected_revision,
                                                  'actual', t.revision));
        v.reason := 'revision_conflict'; v.revision := t.revision;
        RETURN v;
    END IF;

    -- Adjacency, not reachability: OPEN -> DONE does not exist as a move.
    v_allowed := CASE t.status
        WHEN 'OPEN'            THEN 'IN_PROGRESS'
        WHEN 'IN_PROGRESS'     THEN 'READY_TO_REVIEW'
        WHEN 'READY_TO_REVIEW' THEN 'DONE'
        ELSE NULL
    END;
    IF v_allowed IS NULL OR p_to_status <> v_allowed THEN
        PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'illegal_transition',
                               jsonb_build_object('from', t.status, 'to', p_to_status));
        v.reason := 'illegal_transition: ' || t.status || ' -> ' || coalesce(p_to_status, '?');
        v.revision := t.revision;
        RETURN v;
    END IF;

    IF p_to_status = 'DONE' THEN
        -- DONE is a claim about the repositories; the machine demands the
        -- receipts: a PR and a merged SHA, recorded as evidence rows.
        SELECT count(*) FILTER (WHERE kind = 'pr'),
               count(*) FILTER (WHERE kind = 'sha')
          INTO v_pr, v_sha
          FROM backlog_evidence e WHERE e.task_id = p_task_id;
        IF v_pr = 0 OR v_sha = 0 THEN
            PERFORM _backlog_audit(p_task_id, 'transition', 'rejected', 'missing_evidence',
                                   jsonb_build_object('pr', v_pr, 'sha', v_sha));
            v.reason := 'missing_evidence: DONE requires pr and sha';
            v.revision := t.revision;
            RETURN v;
        END IF;
    END IF;

    UPDATE backlog_task b
       SET status = p_to_status, revision = b.revision + 1, updated_at = now()
     WHERE b.task_id = p_task_id
    RETURNING b.revision INTO v.revision;
    PERFORM _backlog_audit(p_task_id, 'transition', 'granted', NULL,
                           jsonb_build_object('from', t.status, 'to', p_to_status));
    v.ok := true;
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- backlog_record_evidence — idempotent by UNIQUE (task, kind, value).
-- ---------------------------------------------------------------------------
CREATE FUNCTION backlog_record_evidence(p_task_id text, p_kind text, p_value text)
    RETURNS backlog_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE; v backlog_verdict;
BEGIN
    v.ok := false;
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id FOR UPDATE;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'evidence', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;
    IF p_kind NOT IN ('pr', 'sha', 'ci', 'acceptance') THEN
        PERFORM _backlog_audit(p_task_id, 'evidence', 'rejected', 'unknown_kind',
                               jsonb_build_object('kind', p_kind));
        v.reason := 'unknown_kind'; v.revision := t.revision;
        RETURN v;
    END IF;
    IF p_value IS NULL OR length(p_value) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'evidence', 'rejected', 'empty_value');
        v.reason := 'empty_value'; v.revision := t.revision;
        RETURN v;
    END IF;
    INSERT INTO backlog_evidence (task_id, kind, value)
    VALUES (p_task_id, p_kind, p_value)
    ON CONFLICT (task_id, kind, value) DO NOTHING;
    PERFORM _backlog_audit(p_task_id, 'evidence', 'granted', p_kind);
    v.ok := true; v.reason := 'recorded'; v.revision := t.revision;
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- backlog_add_dependency — cycle-checked at insert, path in the refusal.
-- ---------------------------------------------------------------------------
CREATE FUNCTION backlog_add_dependency(p_task_id text, p_depends_on text)
    RETURNS backlog_dependency_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v backlog_dependency_verdict; v_path text[];
BEGIN
    v.ok := false;
    IF p_task_id = p_depends_on THEN
        PERFORM _backlog_audit(p_task_id, 'dependency', 'rejected', 'self_dependency');
        v.reason := 'self_dependency';
        RETURN v;
    END IF;
    -- Lock both endpoints (deterministic order) so two concurrent inserts
    -- cannot each pass the cycle check against a snapshot missing the other.
    PERFORM 1 FROM backlog_task b WHERE b.task_id IN (p_task_id, p_depends_on)
     ORDER BY b.task_id FOR UPDATE;
    IF (SELECT count(*) FROM backlog_task b
         WHERE b.task_id IN (p_task_id, p_depends_on)) < 2 THEN
        PERFORM _backlog_audit(NULL, 'dependency', 'rejected', 'unknown_task',
                               jsonb_build_object('task', p_task_id, 'depends_on', p_depends_on));
        v.reason := 'unknown_task';
        RETURN v;
    END IF;

    -- Would p_depends_on reach p_task_id? Then the new edge closes a cycle.
    WITH RECURSIVE walk (task_id, path) AS (
        SELECT d.depends_on_task_id, ARRAY[d.task_id, d.depends_on_task_id]
          FROM backlog_dependency d WHERE d.task_id = p_depends_on
        UNION ALL
        SELECT d.depends_on_task_id, w.path || d.depends_on_task_id
          FROM backlog_dependency d JOIN walk w ON d.task_id = w.task_id
         WHERE NOT d.depends_on_task_id = ANY(w.path)
    )
    SELECT w.path INTO v_path FROM walk w WHERE w.task_id = p_task_id LIMIT 1;

    IF v_path IS NOT NULL THEN
        -- v_path = [p_depends_on, ..., p_task_id]; present the full cycle
        -- starting from the edge the caller tried to add.
        v_path := p_task_id || v_path;
        PERFORM _backlog_audit(p_task_id, 'dependency', 'rejected', 'cycle',
                               jsonb_build_object('path', to_jsonb(v_path)));
        v.reason := 'cycle'; v.path := v_path;
        RETURN v;
    END IF;

    INSERT INTO backlog_dependency (task_id, depends_on_task_id)
    VALUES (p_task_id, p_depends_on)
    ON CONFLICT (task_id, depends_on_task_id) DO NOTHING;
    PERFORM _backlog_audit(p_task_id, 'dependency', 'granted', NULL,
                           jsonb_build_object('depends_on', p_depends_on));
    v.ok := true; v.reason := 'recorded';
    RETURN v;
END
$$;

-- ---------------------------------------------------------------------------
-- The writer lease: acquire / heartbeat / release. Takeover only via expiry.
-- ---------------------------------------------------------------------------
CREATE FUNCTION backlog_lease_acquire(p_authority text, p_owner text, p_ttl_seconds integer)
    RETURNS backlog_lease_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE l backlog_writer_lease%ROWTYPE; v backlog_lease_verdict; v_had boolean;
        v_ttl integer := greatest(coalesce(p_ttl_seconds, 0), 1);
BEGIN
    v.ok := false;
    IF coalesce(length(p_authority), 0) = 0 OR coalesce(length(p_owner), 0) = 0 THEN
        v.reason := 'authority_and_owner_required';
        RETURN v;
    END IF;
    SELECT * INTO l FROM backlog_writer_lease w
     WHERE w.authority = p_authority FOR UPDATE;
    v_had := FOUND;
    IF v_had AND l.owner <> p_owner AND l.expires_at > now() THEN
        PERFORM _backlog_audit(NULL, 'lease_acquire', 'rejected', 'held',
                               jsonb_build_object('authority', p_authority,
                                                  'holder', l.owner,
                                                  'until', l.expires_at));
        v.reason := 'held'; v.owner := l.owner; v.expires_at := l.expires_at;
        RETURN v;
    END IF;
    INSERT INTO backlog_writer_lease (authority, owner, acquired_at, heartbeat_at, expires_at)
    VALUES (p_authority, p_owner, now(), now(), now() + make_interval(secs => v_ttl))
    ON CONFLICT (authority) DO UPDATE
       SET owner = excluded.owner, acquired_at = excluded.acquired_at,
           heartbeat_at = excluded.heartbeat_at, expires_at = excluded.expires_at
    RETURNING backlog_writer_lease.owner, backlog_writer_lease.expires_at
        INTO v.owner, v.expires_at;
    PERFORM _backlog_audit(NULL, 'lease_acquire', 'granted',
                           CASE WHEN v_had AND l.owner IS DISTINCT FROM p_owner
                                THEN 'takeover_after_expiry' ELSE 'acquired' END,
                           jsonb_build_object('authority', p_authority, 'owner', p_owner));
    v.ok := true; v.reason := 'acquired';
    RETURN v;
END
$$;

CREATE FUNCTION backlog_lease_heartbeat(p_authority text, p_owner text, p_ttl_seconds integer)
    RETURNS backlog_lease_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE l backlog_writer_lease%ROWTYPE; v backlog_lease_verdict;
        v_ttl integer := greatest(coalesce(p_ttl_seconds, 0), 1);
BEGIN
    v.ok := false;
    SELECT * INTO l FROM backlog_writer_lease w
     WHERE w.authority = p_authority FOR UPDATE;
    IF NOT FOUND OR l.owner <> p_owner THEN
        PERFORM _backlog_audit(NULL, 'lease_heartbeat', 'rejected', 'not_owner',
                               jsonb_build_object('authority', p_authority, 'caller', p_owner));
        v.reason := 'not_owner';
        IF FOUND THEN v.owner := l.owner; v.expires_at := l.expires_at; END IF;
        RETURN v;
    END IF;
    IF l.expires_at <= now() THEN
        -- An expired lease is not silently revived: the owner must
        -- re-acquire, racing any takeover fairly.
        PERFORM _backlog_audit(NULL, 'lease_heartbeat', 'rejected', 'expired',
                               jsonb_build_object('authority', p_authority));
        v.reason := 'expired'; v.owner := l.owner; v.expires_at := l.expires_at;
        RETURN v;
    END IF;
    UPDATE backlog_writer_lease w
       SET heartbeat_at = now(), expires_at = now() + make_interval(secs => v_ttl)
     WHERE w.authority = p_authority
    RETURNING w.owner, w.expires_at INTO v.owner, v.expires_at;
    v.ok := true; v.reason := 'renewed';
    RETURN v;
END
$$;

CREATE FUNCTION backlog_lease_release(p_authority text, p_owner text)
    RETURNS backlog_lease_verdict
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE l backlog_writer_lease%ROWTYPE; v backlog_lease_verdict;
BEGIN
    v.ok := false;
    SELECT * INTO l FROM backlog_writer_lease w
     WHERE w.authority = p_authority FOR UPDATE;
    IF NOT FOUND OR l.owner <> p_owner THEN
        PERFORM _backlog_audit(NULL, 'lease_release', 'rejected', 'not_owner',
                               jsonb_build_object('authority', p_authority, 'caller', p_owner));
        v.reason := 'not_owner';
        IF FOUND THEN v.owner := l.owner; v.expires_at := l.expires_at; END IF;
        RETURN v;
    END IF;
    DELETE FROM backlog_writer_lease w WHERE w.authority = p_authority;
    PERFORM _backlog_audit(NULL, 'lease_release', 'granted', NULL,
                           jsonb_build_object('authority', p_authority, 'owner', p_owner));
    v.ok := true; v.reason := 'released';
    RETURN v;
END
$$;
