-- 0031: a failure class that measures healthy clears ITS OWN finding
-- (VOYN-MON-WORKER-01-INFRA-DEPLOY-LAG-5ABD205763).
--
-- 0021 gave the fail-closed monitors one finding row per (source, failure
-- class) and one way to close them: `monitor_clear_finding(source)`, which
-- the probe calls only on a tick with NO failures at all. Every probe on a
-- host therefore shares one clearing gate. worker-01:infra runs five of
-- them, and `--unit-health` exists because that host carries units with
-- six-figure NRestarts, so `crash_loop` is red there as a standing fact: a
-- `deploy_lag` that recovered stayed an OPEN finding -- and so stayed a
-- live P1 pipeline task -- until something unrelated on the same host went
-- green too. The planner reads `state = 'open'`, not "red right now", so a
-- stale row is indistinguishable from the outage it once described.
--
-- `monitor_clear_findings_except` closes that gap without widening the write
-- surface: the probe names the classes it measured RED this tick and every
-- other open finding of that source is cleared, in one statement, under the
-- same SECURITY DEFINER boundary. `monitor_clear_finding(source)` is kept --
-- it is exactly this function with an empty keep-list, and the operator and
-- test call sites that use it read better that way.
CREATE OR REPLACE FUNCTION monitor_clear_findings_except(p_source text, p_keep text[])
    RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_n integer;
BEGIN
    -- NULL is "keep nothing", not "keep everything": a caller that lost its
    -- keep-list must not silently stop clearing. `= ANY` on an empty array
    -- is false for every row, so the empty case clears the source entirely
    -- and is identical to monitor_clear_finding().
    UPDATE monitor_finding SET state = 'cleared', cleared_at = now()
     WHERE source = p_source AND state = 'open'
       AND NOT (failure = ANY (coalesce(p_keep, ARRAY[]::text[])));
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END
$$;
GRANT EXECUTE ON FUNCTION monitor_clear_findings_except(text, text[]) TO aicc_app, aicc_worker;
