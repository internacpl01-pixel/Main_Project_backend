-- =============================================================================
-- 054_narration_rule_exclusive_then.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Narrows narration_rule's THEN from "at least one of From/To or Purpose" to
-- "exactly one" -- per the user, the two are unrelated answers to unrelated
-- questions (which branch a row falls into decides which one could ever be
-- read), and a single rule should not carry both at once.
--
-- Nothing to migrate: 053 only just shipped, so any rule saved between the two
-- either already sets exactly one (the ordinary case so far) or sets both, in
-- which case this would refuse it -- checked defensively below rather than
-- assumed.
-- =============================================================================

DO $$
DECLARE
    n int;
BEGIN
    SELECT count(*) INTO n FROM narration_rule
     WHERE from_label IS NOT NULL AND purpose_label IS NOT NULL;
    IF n > 0 THEN
        RAISE EXCEPTION
            '% narration rule(s) set both From/To and Purpose -- this '
            'migration requires exactly one. Edit them on the Rules page '
            'first, then re-run.', n;
    END IF;
END $$;

ALTER TABLE narration_rule DROP CONSTRAINT IF EXISTS narration_rule_has_a_then_check;

ALTER TABLE narration_rule
    ADD CONSTRAINT narration_rule_exclusive_then_check CHECK (
        (from_label IS NOT NULL AND to_label IS NOT NULL AND purpose_label IS NULL)
        OR
        (from_label IS NULL AND to_label IS NULL AND purpose_label IS NOT NULL)
    );
