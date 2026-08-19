-- =============================================================================
-- 003_company_code.sql
-- Applied to the admin schema by:  python -m db.migrate upgrade
--
-- Gives every company a short code, and makes it the prefix every one of that
-- company's usernames carries: code 'dpl' means accounts are named dpl-ravi,
-- dpl-anita. Login resolves a user from the username alone, before any company
-- is known, so the username is the only place the company can be written down
-- where it is visible at the moment it matters.
--
-- Exactly three lowercase letters. Stored lowercase and only ever compared
-- lowercase, so the code is case-insensitive without a functional index or a
-- citext dependency -- 'DPL' typed into the form is 'dpl' by the time it
-- reaches here. The CHECK is what makes that true of rows written by hand as
-- well as rows written by the app.
--
-- Nullable, deliberately. Companies registered before this migration have no
-- code and there is nothing to invent for them; the app requires a code on
-- every NEW company and lets a super admin fill one in on an old one once.
-- Their existing usernames keep working either way -- the prefix rule is
-- enforced when an account is created or renamed, never at login.
-- =============================================================================

ALTER TABLE admin.companies ADD COLUMN IF NOT EXISTS code text;

ALTER TABLE admin.companies DROP CONSTRAINT IF EXISTS companies_code_shape;
ALTER TABLE admin.companies ADD CONSTRAINT companies_code_shape
    CHECK (code IS NULL OR code ~ '^[a-z]{3}$');

-- Partial: the codeless companies above would otherwise all collide on NULL in
-- some engines, and a unique index that excludes them says what is meant --
-- every code that exists is unique.
CREATE UNIQUE INDEX IF NOT EXISTS companies_code_uq
    ON admin.companies (code) WHERE code IS NOT NULL;
