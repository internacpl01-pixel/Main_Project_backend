-- =============================================================================
-- 002_roles_and_registry.sql
-- Splits the flat 'user' role into a manager/staff hierarchy and records who
-- registered each company.
-- Applied to the 'admin' schema by db.migrate.py.
-- =============================================================================

-- 1. Roles.
--    001 allowed ('super_admin', 'company_admin', 'user'). 'user' was one flat
--    bucket with no way to say "this one may delete, that one may only look",
--    so every non-admin got the same rights. Split it:
--
--      staff         (0) — read + data entry, nothing destructive
--      manager       (1) — staff + master data, projects, mappings, deletes
--      company_admin (2) — manager + user management for their own company
--      super_admin   (3) — company_admin + register companies, switch between them
--
--    Existing 'user' rows become staff, the lowest rung. Nobody gains rights
--    from this migration.
UPDATE admin.users SET role = 'staff' WHERE role = 'user';

ALTER TABLE admin.users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE admin.users ADD CONSTRAINT users_role_check
    CHECK (role IN ('super_admin', 'company_admin', 'manager', 'staff'));

-- 2. Company scope.
--    A super_admin belongs to no company (company_id NULL is what makes login
--    hand them the 'admin' schema). Everyone else MUST have one — a
--    company_admin with a NULL company_id would log in pointed at the admin
--    schema, which holds no projects or transactions, and every page would
--    fail with 'relation "projects" does not exist'.
ALTER TABLE admin.users DROP CONSTRAINT IF EXISTS users_company_scope_check;
ALTER TABLE admin.users ADD CONSTRAINT users_company_scope_check
    CHECK (
        (role = 'super_admin' AND company_id IS NULL)
        OR (role <> 'super_admin' AND company_id IS NOT NULL)
    );

-- 3. Company registry.
--    Who registered this company. Companies created by the CLI before this
--    migration keep NULL — there was no acting user to record.
--    ON DELETE SET NULL: deleting the admin who created a company must not
--    take the company row with it.
ALTER TABLE admin.companies ADD COLUMN IF NOT EXISTS created_by bigint
    REFERENCES admin.users(id) ON DELETE SET NULL;
