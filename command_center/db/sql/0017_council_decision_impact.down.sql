-- Downgrade of 0017_council_decision_impact (VOYN-MIN-WOW-1).

ALTER TABLE council_decision DROP COLUMN IF EXISTS impact_json;
