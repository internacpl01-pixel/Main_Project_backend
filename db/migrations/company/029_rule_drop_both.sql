-- =============================================================================
-- 029_rule_drop_both.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Drop BOTH as a direction a rule cell can hold.
--
-- 028 allowed three answers per cell: CR, DR, or BOTH — a head that satisfies
-- either side of the same account type. Nobody used it. Every company holds the
-- same three seeded rows and all of them are CR or DR, so this removes an option
-- that has only ever been a third thing to explain on a screen whose whole point
-- is that a head means money in or money out.
--
-- Tightened here rather than left permissive on purpose. A BOTH row inserted by
-- hand would still occupy its cell's UNIQUE(head_id, account_type) slot while no
-- longer being understood by anything that reads the table — the grid could not
-- paint it and the check would not accept it in either direction. A head that is
-- silently no answer at all is worse than a rejected INSERT.
--
-- Verified before applying: 0 rows with direction = 'BOTH' across all 8 company
-- schemas, so nothing is rewritten or lost. Should BOTH ever be wanted back, it
-- is this constraint plus the direction set in services/rules.py and
-- routers/rules.py; 028's comments describe what it meant.
-- =============================================================================

-- Belt and braces: this is a no-op today (checked, zero rows), and it means the
-- constraint below cannot fail on a company that acquired one between the check
-- and the deploy. DR is the safer landing of the two — a rule that accepts a
-- head on debits only flags more than one that accepted both, and over-flagging
-- is visible on the Check Rules screen while under-flagging is not.
UPDATE rule SET direction = 'DR', updated_at = now() WHERE direction = 'BOTH';

ALTER TABLE rule DROP CONSTRAINT IF EXISTS rule_direction_check;
ALTER TABLE rule ADD CONSTRAINT rule_direction_check
    CHECK (direction IN ('CR', 'DR'));
