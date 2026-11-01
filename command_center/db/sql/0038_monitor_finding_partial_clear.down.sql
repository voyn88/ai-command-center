-- The one-argument `monitor_clear_finding(text)` is 0021's and is untouched by
-- the up migration, so the downgrade only has to remove the overload.
DROP FUNCTION IF EXISTS monitor_clear_finding(text, text[]);
