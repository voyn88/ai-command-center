-- VOYN-W0-AICC-BACKLOG-PG-CANONICAL-GATE-REM: make the migration gate's third
-- proof ("existing records migrated with provenance") hold by construction
-- rather than by sequence.
--
-- 0023 gave the importer somewhere to stamp provenance, but the stamp was a
-- SECOND round trip taken after `backlog_upsert_task` had already returned:
-- two statements, two transactions, and between them a window in which a
-- crash, a timeout or a dropped connection leaves a row inserted and
-- permanently unaudited -- precisely the failure modes an audit trail exists
-- to survive. An audit control that can silently no-op is worse than no
-- control, because `backlog_event` then reports an absence of drift that was
-- never actually measured.
--
-- `backlog_import_task` closes the window by construction: the upsert and the
-- stamp happen inside ONE function call, hence one statement, hence one
-- transaction. Either the row and its provenance event both commit, or
-- neither does. It is additive -- `backlog_upsert_task` keeps its signature
-- and its callers, and this composes with it rather than reimplementing it,
-- because a second copy of the insert/constraint/idempotence handling would
-- be a second authority over the same decision.
CREATE FUNCTION backlog_import_task(
    p_task_id text, p_wave text, p_priority text, p_status text,
    p_kind text, p_title text, p_body text, p_repo text,
    p_source text, p_detail jsonb DEFAULT NULL
) RETURNS TABLE (ok boolean, reason text, changed boolean, revision bigint,
                 provenance_recorded boolean)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE u record; v_stamped boolean;
BEGIN
    -- Checked BEFORE the upsert, not after: a caller that cannot name where a
    -- record came from does not get to create one, and the row never exists
    -- to be left unaudited. Refusal-as-data, in the 0005 idiom.
    IF p_source IS NULL OR length(p_source) = 0 THEN
        PERFORM _backlog_audit(NULL, 'import', 'rejected', 'empty_provenance_source',
                               jsonb_build_object('requested_task_id', p_task_id));
        RETURN QUERY SELECT false, 'empty_provenance_source', false, NULL::bigint, false;
        RETURN;
    END IF;

    SELECT * INTO u FROM backlog_upsert_task(p_task_id, p_wave, p_priority, p_status,
                                             p_kind, p_title, p_body, p_repo);
    IF NOT u.ok OR u.reason <> 'inserted' THEN
        -- A row that came back `unchanged` or `updated` was migrated on some
        -- earlier run and carries its stamp already; restamping would make
        -- "when was this migrated" answer "whenever the importer last ran".
        RETURN QUERY SELECT u.ok, u.reason, u.changed, u.revision, false;
        RETURN;
    END IF;

    v_stamped := backlog_record_provenance(p_task_id, p_source, p_detail);
    IF NOT v_stamped THEN
        -- Unreachable by inspection: the row was inserted moments ago in this
        -- same transaction and the source is non-empty by the check above.
        -- That is exactly why it RAISES rather than returning a flag -- if the
        -- one invariant this function exists to hold is ever violated, the
        -- insert has to go with it instead of committing as an unaudited row.
        RAISE EXCEPTION 'backlog_import_task: provenance not recorded for %', p_task_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN QUERY SELECT true, 'inserted', true, u.revision, true;
END
$$;
