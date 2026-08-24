-- =============================================================================
-- 016_bank_company.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Record which group company a bank account belongs to, chosen from
-- company_master (the Company tab added in 014).
--
-- A separate file rather than an edit to 015 because 015 is already applied
-- everywhere and migrate.py records a checksum per file — changing an applied
-- file makes it disagree with what is recorded, which is the check that exists
-- to catch exactly this.
--
-- `company` here means a row in company_master: a group or associate company
-- inside this schema's books. It is NOT admin.companies, the tenant that owns
-- the schema — that is already implied by which schema the row is in.
--
-- Name, not a reference, and optional: same reasoning as account_type in 015.
-- =============================================================================

ALTER TABLE bank_master
    ADD COLUMN IF NOT EXISTS company text;
