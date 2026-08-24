-- =============================================================================
-- 018_beneficiary_company.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Record which group company a beneficiary belongs to, chosen from
-- company_master — the same dropdown bank_master gained in 016.
--
-- Name, not a reference, and optional: same reasoning as 015 and 016. These
-- columns exist to be read on screen and filled from a dropdown that makes a
-- typo impossible; the ledger's own references are elsewhere.
-- =============================================================================

ALTER TABLE beneficiary_master
    ADD COLUMN IF NOT EXISTS company text;
