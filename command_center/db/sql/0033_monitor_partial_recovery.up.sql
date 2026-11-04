-- A recovered queue symptom must not stay open merely because another
-- failure (for example recent dead-letter growth) is still present.
CREATE FUNCTION monitor_clear_resolved_findings(
    p_source text, p_active_failures jsonb, p_observed_at timestamptz
) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, public AS $$
DECLARE v_n integer;
BEGIN
    IF p_source IS NULL OR p_source !~ '^[A-Za-z0-9._:-]{1,120}$'
       OR p_active_failures IS NULL OR p_observed_at IS NULL
       OR jsonb_typeof(p_active_failures) <> 'array' THEN
        RAISE EXCEPTION 'invalid monitor recovery snapshot';
    END IF;
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(p_active_failures) f
               WHERE jsonb_typeof(f) <> 'string' OR length(f #>> '{}') NOT BETWEEN 1 AND 200) THEN
        RAISE EXCEPTION 'invalid active monitor failure';
    END IF;
    UPDATE monitor_finding SET state = 'cleared', cleared_at = now()
     WHERE source = p_source AND state = 'open'
       AND NOT (p_active_failures ? failure)
       -- A slow/older observation must not erase a newer failure report.
       AND seen_at <= p_observed_at;
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END
$$;
REVOKE ALL ON FUNCTION monitor_clear_resolved_findings(text, jsonb, timestamptz) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION monitor_clear_resolved_findings(text, jsonb, timestamptz)
    TO aicc_app, aicc_worker;
