-- VOYN-W0-AICC-BACKLOG-PG-CANONICAL-GATE: the migration gate's third proof
-- ("existing records migrated with provenance") needs a place to put it.
-- `backlog_upsert_task` is the one path allowed to set status directly
-- during import (0005's docstring), and until now it left no trace of WHERE
-- a record came from: an operator reading `backlog_event` after an import
-- sees `upsert / granted / inserted` and nothing that ties the row back to
-- the Markdown line it was read from. `backlog_record_provenance` is that
-- trace -- a thin, additive audit act (append to the existing `backlog_event`
-- table, no new table, no change to any existing function's signature) that
-- the importer calls once per newly-inserted task.
CREATE FUNCTION backlog_record_provenance(
    p_task_id text, p_source text, p_detail jsonb DEFAULT NULL
) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE t backlog_task%ROWTYPE;
BEGIN
    SELECT * INTO t FROM backlog_task b WHERE b.task_id = p_task_id;
    IF NOT FOUND THEN
        PERFORM _backlog_audit(NULL, 'provenance', 'rejected', 'unknown_task',
                               jsonb_build_object('requested_task_id', p_task_id));
        RETURN false;
    END IF;
    IF p_source IS NULL OR length(p_source) = 0 THEN
        PERFORM _backlog_audit(p_task_id, 'provenance', 'rejected', 'empty_source');
        RETURN false;
    END IF;
    PERFORM _backlog_audit(p_task_id, 'provenance', 'granted', p_source, p_detail);
    RETURN true;
END
$$;
