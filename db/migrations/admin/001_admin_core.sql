-- =============================================================================
-- 001_admin_core.sql
-- Creates the cross-company admin layer.
-- Applied to the 'admin' schema by db.migrate.py.
-- =============================================================================

-- 1. Trigger function: auto-touch updated_at on every UPDATE
--    Used by every table in every schema. Lives in admin so all schemas
--    can reference it without cross-schema gymnastics.
CREATE OR REPLACE FUNCTION admin.set_updated_at()
RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

-- 2. Migration tracking: which SQL files have been applied to which schema.
--    This is the ONLY way to answer "did company_007 get the new column?"
CREATE TABLE IF NOT EXISTS admin.schema_migrations (
    schema_name text        NOT NULL,
    filename    text        NOT NULL,
    applied_at  timestamptz NOT NULL DEFAULT now(),
    checksum    text        NOT NULL,  -- sha256 of the file at apply time
    PRIMARY KEY (schema_name, filename)
);

-- 3. Sequence: generates company_001, company_002, ... in order.
--    Owned by admin, so the sequence survives if a company is dropped.
CREATE SEQUENCE IF NOT EXISTS admin.company_schema_seq START 1;

-- 4. Companies: one row per tenant.
--    schema_name is the bridge between login and physical data isolation.
CREATE TABLE admin.companies (
    id          bigserial   PRIMARY KEY,
    name        text        NOT NULL UNIQUE,
    schema_name text        NOT NULL UNIQUE,  -- 'company_001', 'company_002', ...
    is_active   boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER companies_set_updated_at
    BEFORE UPDATE ON admin.companies
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

-- 5. Users: login accounts. company_id NULL = super_admin (sees all).
--    We deliberately do NOT add company_schema here. Two copies of
--    "which company is this user in" drift apart. We resolve it once at
--    login (SELECT schema_name FROM companies WHERE id = $1) and put
--    the schema_name in the JWT.
CREATE TABLE admin.users (
    id            bigserial   PRIMARY KEY,
    username      text        NOT NULL UNIQUE,
    password_hash text        NOT NULL,         -- bcrypt, never plaintext
    role          text        NOT NULL CHECK (role IN ('super_admin', 'company_admin', 'user')),
    company_id    bigint      REFERENCES admin.companies(id) ON DELETE RESTRICT,
    is_active     boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX idx_users_company_id ON admin.users(company_id);

CREATE TRIGGER users_set_updated_at
    BEFORE UPDATE ON admin.users
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();