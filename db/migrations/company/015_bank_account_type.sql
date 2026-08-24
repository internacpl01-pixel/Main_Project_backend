-- =============================================================================
-- 015_bank_account_type.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Record which kind of account a bank row is: current, CC, escrow, and so on.
--
-- Stored as the type's NAME rather than a foreign key to account_type_master,
-- for the same reason the beneficiary head columns are (013): the master table
-- exists to fill a dropdown, so the value cannot be mistyped, and text keeps
-- the generic master router reading one table with no join — the Bank tab shows
-- "Current" instead of an integer nobody can read.
--
-- The trade is that renaming a type in Type of Account does not follow through
-- to banks already carrying the old name. That matters less here than getting
-- it wrong would in the ledger, where classification really is a reference.
--
-- Optional, like account_number and ifsc_code beside it. A bank row entered
-- before the company had decided its account types is still a usable bank row,
-- and account_type_master starts empty.
-- =============================================================================

ALTER TABLE bank_master
    ADD COLUMN IF NOT EXISTS account_type text;
