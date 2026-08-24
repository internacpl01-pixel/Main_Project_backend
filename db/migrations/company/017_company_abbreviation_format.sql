-- =============================================================================
-- 017_company_abbreviation_format.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- An abbreviation must be exactly three capital letters: ACP, DPL, XYZ.
--
-- Checked against live data before writing this: every company_master row on
-- this install either has no abbreviation or already satisfies the rule, so
-- nothing has to be corrected before the constraint can be added. A CHECK is
-- validated against existing rows at ADD time and the whole migration would
-- have failed otherwise -- loudly, which is the right outcome, but it is worth
-- knowing in advance rather than finding out mid-upgrade.
--
-- Still OPTIONAL. A CHECK that evaluates to NULL passes, so a row with no
-- abbreviation is unaffected and only a value that IS given has to be well
-- formed. Making it mandatory is a separate decision from making it valid, and
-- was not asked for.
--
-- [A-Z] and not [[:alpha:]]: the rule is capital LETTERS, so digits, spaces,
-- punctuation and lowercase are all out, and so are accented characters that
-- would otherwise slip through a locale-aware class.
-- =============================================================================

ALTER TABLE company_master
    DROP CONSTRAINT IF EXISTS company_master_abbreviation_format;

ALTER TABLE company_master
    ADD CONSTRAINT company_master_abbreviation_format
    CHECK (abbreviation ~ '^[A-Z]{3}$');
