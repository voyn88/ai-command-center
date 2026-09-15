-- Reverse of 0025: drop the function, then the table it writes.
--
-- The order matters only for readability here (the function is not a
-- dependency of the table), but dropping the writer before the store is the
-- honest direction, and `IF EXISTS` keeps a partially applied upgrade
-- reversible.
DROP FUNCTION IF EXISTS backlog_mark_duplicate(text, text, text);
DROP TABLE IF EXISTS backlog_duplicate;
