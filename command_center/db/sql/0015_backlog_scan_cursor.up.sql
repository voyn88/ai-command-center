-- VOYN-OPS-AICC-PUBLISH-WINDOW-STARVATION: the persisted scan cursor.
--
-- Every stateless schedule tried for the tick scan windows fell to a
-- reviewed counterexample: a fixed page starved its own tail behind
-- action-consuming rows; wall-clock-derived offsets alias against the
-- actual tick cadence (bucket*stride mod N can resonate to a constant --
-- e.g. 15-minute ticks over 12 rows with stride 4 select offset 0
-- forever) and are not restart-safe. Fairness that survives arbitrary
-- scheduling requires one small piece of state: a named cursor advanced
-- atomically PER ACTUAL INVOCATION, not per wall-clock bucket.
--
-- backlog_scan_advance returns the offset the CALLING tick should use and
-- advances the cursor by p_step modulo p_modulo under a row lock, so
-- concurrent ticks get disjoint windows and a restart resumes exactly
-- where the last tick stopped.

CREATE TABLE backlog_scan_cursor (
    name       text PRIMARY KEY,
    position   bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT backlog_scan_cursor_name_present CHECK (length(name) > 0),
    CONSTRAINT backlog_scan_cursor_position_nonnegative CHECK (position >= 0)
);

CREATE FUNCTION backlog_scan_advance(
    p_name text, p_step integer, p_modulo integer
) RETURNS integer
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_modulo integer; v_old bigint;
BEGIN
    v_modulo := greatest(coalesce(p_modulo, 1), 1);
    INSERT INTO backlog_scan_cursor (name) VALUES (p_name)
        ON CONFLICT (name) DO NOTHING;
    SELECT position INTO v_old FROM backlog_scan_cursor
     WHERE name = p_name FOR UPDATE;
    UPDATE backlog_scan_cursor
       SET position = (v_old + greatest(coalesce(p_step, 1), 1)) % v_modulo,
           updated_at = now()
     WHERE name = p_name;
    RETURN (v_old % v_modulo)::integer;
END
$$;
