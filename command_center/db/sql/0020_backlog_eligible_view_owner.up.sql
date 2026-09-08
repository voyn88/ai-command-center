-- VOYN-W0-AICC-BACKLOG-ELIGIBLE-VIEW-OWNER-AFTER-0019: 0019 recreated
-- `backlog_eligible` (DROP VIEW + CREATE VIEW) to add `task_class`. A view
-- created that way belongs to whichever role ran the upgrade -- on
-- control-01 (2026-09-08 11:17 UTC) that was `aicc_admin`, not the
-- `aicc_migrator` that owns every other backlog relation and, crucially,
-- owns the SECURITY DEFINER `backlog_dispatch`. Inside that function the
-- view is read as `aicc_migrator`, which no longer had SELECT on it:
-- "permission denied for view backlog_eligible", and every planner tick
-- died until an operator restored the owner by hand.
--
-- Restore the pre-0019 ownership and read grant explicitly, guarded so a
-- database without the production roles (test rigs) is unaffected.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aicc_migrator') THEN
        EXECUTE 'ALTER VIEW backlog_eligible OWNER TO aicc_migrator';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aicc_app') THEN
        EXECUTE 'GRANT SELECT ON backlog_eligible TO aicc_app';
    END IF;
END
$$;
