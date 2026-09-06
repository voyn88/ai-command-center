-- Downgrade of 0017_queue_reopen.

DROP FUNCTION IF EXISTS queue_reopen(text, text, integer);
