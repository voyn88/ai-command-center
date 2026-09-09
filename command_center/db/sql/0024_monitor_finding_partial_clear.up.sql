-- 0024: a monitor source's findings are RECORDED per failure but were only
-- CLEARABLE all at once (VOYN-MON-CONTROL-01-QUEUE-QUEUE-STALLED).
--
-- 0021 gave the fail-closed monitors two calls: `monitor_record_finding` opens
-- one row per (source, failure) -- deliberately per failure, so the planner
-- mints one task per distinct thing that is wrong -- and
-- `monitor_clear_finding(source)` closes EVERY open row of that source. The
-- monitor calls the second one only on a tick where its whole report is green
-- (`command_center/ops/infra_monitor.py`), which makes clearing all-or-nothing
-- across checks that have nothing to do with each other.
--
-- Live consequence on `control-01:queue` (monitor_finding #481, the finding
-- this migration is filed under): that one probe reports six independent
-- failures -- `queue_stalled`, `claim_overdue`, `dead_letter_growth`,
-- `executor_quota_exhausted`, `throughput_stalled`, `prometheus_unready`. It
-- runs with `--max-recent-dead 0`, so a SINGLE dead-lettered item in the
-- trailing hour keeps `dead_letter_growth` red (control-01 measured 53 of them
-- in one hour), and a fleet that dead-letters at all therefore never produces
-- the all-green tick that the only clear path needs. A `queue_stalled` finding
-- whose measurement has been healthy for days stays `open` behind an unrelated
-- red sibling, its task stays open with it, and the monitor's own promise --
-- "the monitor clears the finding when it measures healthy" -- is one the
-- schema could not keep.
--
-- The fix is the set difference, computed where the rows are: clear the open
-- findings of this source EXCEPT the ones the current tick still measures.
-- Every tick becomes a full reconciliation of the source instead of an
-- append-only log with a rarely reachable reset.
--
-- WHY AN OVERLOAD AND NOT A REPLACEMENT. `monitor_clear_finding(text)` stays,
-- granted and unchanged. Control hosts and worker hosts deploy independently
-- (`voyn-queue-monitor.service` runs from the control host's checkout; the
-- worker-host probe from `/opt/aicc/current`), so a host still running
-- pre-0024 code keeps a working clear path instead of turning every healthy
-- tick into `findings_error`. PostgreSQL resolves the two by arity, and
-- NEITHER declares a default -- a `DEFAULT '{}'` on this one would make the
-- one-argument call ambiguous and break exactly the callers it is kept for.
CREATE FUNCTION monitor_clear_finding(p_source text, p_active_failures text[])
    RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_n integer;
BEGIN
    -- `array_remove(..., NULL)` is not decoration: `failure = ANY (array
    -- containing NULL)` is NULL rather than false, and `NOT NULL` is NULL, so
    -- one NULL element would silently protect every open row of the source
    -- from being cleared -- the exact failure this migration exists to end.
    -- A NULL array is the empty array: nothing is still failing, clear it all,
    -- which is `monitor_clear_finding(source)`'s behaviour exactly.
    UPDATE monitor_finding
       SET state = 'cleared', cleared_at = now()
     WHERE source = p_source
       AND state = 'open'
       AND failure <> ALL (array_remove(coalesce(p_active_failures, '{}'), NULL));
    GET DIAGNOSTICS v_n = ROW_COUNT;
    RETURN v_n;
END
$$;

-- Same grantees as the pair it joins (0021): both probes clear their own
-- source, and neither may read or write another's rows -- `p_source` is the
-- whole authority here, as it was for the one-argument form.
GRANT EXECUTE ON FUNCTION monitor_clear_finding(text, text[]) TO aicc_app, aicc_worker;
