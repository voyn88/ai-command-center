-- 0029 down: remove the per-class clearing path. `monitor_clear_finding` is
-- untouched by the up migration, so the 0021 behaviour -- one clearing gate
-- per source, fired only by a tick with no failures at all -- is what
-- remains.
DROP FUNCTION IF EXISTS monitor_clear_findings_except(text, text[]);
