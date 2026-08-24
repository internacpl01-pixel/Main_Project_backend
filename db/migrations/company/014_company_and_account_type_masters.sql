-- =============================================================================
-- 014_company_and_account_type_masters.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Two more master tables: the group companies a company's books refer to, and
-- the kinds of bank account it holds.
--
-- Both live in the company schema, so every company keeps its own copy and
-- edits one without touching another's -- and, because this is a file in
-- db/migrations/company/, every company registered from here on is provisioned
-- with them too. That is the same route the head tables took; nothing has to be
-- repeated per company by hand.
--
-- NAMING: company_master inside a schema called company_028 reads oddly, and it
-- is worth being explicit about what it is NOT. admin.companies is the app's
-- tenant registry -- the seven companies you log into, each owning a schema,
-- each with a `code` already serving as an abbreviation. company_master here is
-- unrelated: it is the list of group and associate companies that appear inside
-- ONE company's books, for inter-company transfers and the like. The two will
-- overlap in content and must not be joined or kept in step.
-- =============================================================================

CREATE TABLE IF NOT EXISTS company_master (
    id           bigserial   PRIMARY KEY,
    name         text        NOT NULL UNIQUE,
    -- Optional, and UNIQUE. Postgres allows any number of NULLs in a UNIQUE
    -- column but only one empty string, which is why the master router turns an
    -- untouched input into NULL rather than storing '' -- without that rule the
    -- second company saved without an abbreviation would collide with the first.
    abbreviation text        UNIQUE,
    is_active    boolean     NOT NULL DEFAULT true,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS account_type_master (
    id         bigserial   PRIMARY KEY,
    name       text        NOT NULL UNIQUE,
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Left empty on purpose. Savings / Current / CC / OD / Escrow would be a guess
-- at DPL's own list, and a wrong seed is worse than an empty table: it has to be
-- deleted in seven schemas before the right values can go in. Add them from the
-- Master Data screen, or say the word and they become a 015 seed like the heads.

-- DROP then CREATE because Postgres has no CREATE TRIGGER IF NOT EXISTS before
-- 14, and every file in this directory has to survive being run twice.
DROP TRIGGER IF EXISTS company_master_set_updated_at ON company_master;
CREATE TRIGGER company_master_set_updated_at
    BEFORE UPDATE ON company_master
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

DROP TRIGGER IF EXISTS account_type_master_set_updated_at ON account_type_master;
CREATE TRIGGER account_type_master_set_updated_at
    BEFORE UPDATE ON account_type_master
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();
