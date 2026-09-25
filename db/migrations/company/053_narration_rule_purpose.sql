-- =============================================================================
-- 053_narration_rule_purpose.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Adds a second, independent THEN to narration_rule (052): a Purpose override,
-- alongside From/To.
--
-- build_narration prints "Purpose: ..." in two branches neither From/To
-- touches -- Receipt Credit (Purpose: Remarks, or Head when Remarks is blank)
-- and Payment Disbursement (Purpose: "Salary" or Remarks). One rule can now
-- set From/To, Purpose, or both: whichever field is filled is used by
-- whichever branch the matched row actually falls into. A row is only ever in
-- one branch, so a rule carrying both never has to choose between them.
--
-- from_label/to_label go from NOT NULL to nullable, and become a pair: both
-- filled or both blank, enforced below, since "(From X to Y)" with only one
-- side is not printable. purpose_label is independent of that pair. The
-- existing "at least a From/To" CHECKs are dropped and replaced by one CHECK
-- requiring at least one of the two THENs to be set at all -- a rule with
-- neither is not a rule, the same reason a condition needs at least one head.
-- =============================================================================

ALTER TABLE narration_rule
    ALTER COLUMN from_label DROP NOT NULL,
    ALTER COLUMN to_label DROP NOT NULL,
    ADD COLUMN IF NOT EXISTS purpose_label text;

ALTER TABLE narration_rule DROP CONSTRAINT IF EXISTS narration_rule_from_filled;
ALTER TABLE narration_rule DROP CONSTRAINT IF EXISTS narration_rule_to_filled;

ALTER TABLE narration_rule
    ADD CONSTRAINT narration_rule_leg_pair_check CHECK (
        (from_label IS NULL AND to_label IS NULL)
        OR (btrim(from_label) <> '' AND btrim(to_label) <> '')
    ),
    ADD CONSTRAINT narration_rule_purpose_filled_check CHECK (
        purpose_label IS NULL OR btrim(purpose_label) <> ''
    ),
    ADD CONSTRAINT narration_rule_has_a_then_check CHECK (
        (from_label IS NOT NULL AND to_label IS NOT NULL)
        OR purpose_label IS NOT NULL
    );
