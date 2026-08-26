-- VOYN-OPS-AICC-PUBLISH-WINDOW-STARVATION: the persisted KEYSET scan cursor.
--
-- Five reviewed counterexamples killed every lighter scheme for the tick
-- scan windows: pseudo-random sampling, fixed pages starved by
-- action-consuming heads, strides wider than the window, wall-clock
-- aliasing against the real tick cadence, and finally a persisted NUMERIC
-- offset whose ordinal meaning shifts under membership churn (insertions
-- before a waiting task move it out from under the cursor indefinitely).
-- A keyset cursor has none of these failure modes: it stores the LAST
-- EXAMINED task_id, which is immovable relative to every other persisting
-- id, so the cursor provably passes every waiter within one lap of the id
-- space regardless of churn, cadence, restarts, or which rows consume the
-- action budget.
--
-- Writes go only through backlog_scan_claim -- an insert-if-missing
-- compare-and-set under the row lock. A failed CAS means a concurrent
-- same-name tick advanced first; the caller proceeds with its
-- already-fetched window (a bounded duplicate examination, never a skip).

CREATE TABLE backlog_scan_cursor (
    name       text PRIMARY KEY,
    position   text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT backlog_scan_cursor_name_present CHECK (length(name) > 0)
);

CREATE FUNCTION backlog_scan_claim(
    p_name text, p_expected text, p_new text
) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, public AS $$
DECLARE v_position text;
BEGIN
    INSERT INTO backlog_scan_cursor (name) VALUES (p_name)
        ON CONFLICT (name) DO NOTHING;
    SELECT position INTO v_position FROM backlog_scan_cursor
     WHERE name = p_name FOR UPDATE;
    IF v_position IS DISTINCT FROM p_expected THEN
        RETURN false;
    END IF;
    UPDATE backlog_scan_cursor
       SET position = coalesce(p_new, ''), updated_at = now()
     WHERE name = p_name;
    RETURN true;
END
$$;
