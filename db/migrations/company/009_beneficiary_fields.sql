-- =============================================================================
-- 009_beneficiary_fields.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Reshape beneficiary_master around how a payment is actually made.
--
-- It was created as a party record — name, category, PAN, GSTIN — which
-- describes who somebody is for tax purposes. What the ledger needs is where
-- the money goes: an account number, an IFSC code and the bank holding it. A
-- beneficiary row that cannot tell you that is a label, not a payee.
--
-- category / pan / gstin are dropped rather than kept alongside. They were
-- never populated in any company on this install (checked before writing this:
-- every schema had zero beneficiary rows and zero transactions or staged rows
-- referencing one), so nothing is being thrown away. If a company ever needs
-- PAN back it is a new column in a later file, not a resurrection of this one.
--
-- head1 / head2 / head3 are free text on purpose. They read like the three
-- head tables this app already has — head_master, rera_head_master,
-- idw_head_master — but they are NOT foreign keys to them and must not be
-- treated as such: these are notes about how this payee is usually booked, not
-- a classification the ledger enforces. Making them references later means a
-- migration that adds *_id columns and backfills by name; making them
-- references by accident means every typo becomes a constraint violation.
--
-- Everything here is IF EXISTS / IF NOT EXISTS, so re-running is a no-op and a
-- company provisioned from 001 plus this file lands in the same shape as one
-- that has been through both.
-- =============================================================================

ALTER TABLE beneficiary_master
    DROP COLUMN IF EXISTS category,
    DROP COLUMN IF EXISTS pan,
    DROP COLUMN IF EXISTS gstin;

ALTER TABLE beneficiary_master
    ADD COLUMN IF NOT EXISTS account_number text,
    ADD COLUMN IF NOT EXISTS ifsc_code      text,
    ADD COLUMN IF NOT EXISTS bank_name      text,
    ADD COLUMN IF NOT EXISTS head1          text,
    ADD COLUMN IF NOT EXISTS head2          text,
    ADD COLUMN IF NOT EXISTS head3          text;

-- No UNIQUE constraint on the account number.
--
-- It is the tempting one to add, and it is wrong here. The same account
-- legitimately appears twice — a firm and its proprietor sharing one current
-- account, or the same payee recorded once per project. More to the point, the
-- field is optional: a beneficiary paid in cash has no account number at all,
-- and in Postgres a UNIQUE column permits many NULLs but exactly one blank
-- string, so the first two rows saved from an empty form would collide on ''.
-- The name is not unique either, and never was.
