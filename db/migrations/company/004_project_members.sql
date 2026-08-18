-- =============================================================================
-- 004_project_members.sql
-- Which people work on which projects.
-- Applied to every company schema by db.migrate.py.
-- =============================================================================

-- A company runs several projects at once, and a manager or staff member is
-- normally hired onto one or two of them, not all. This table is the assignment
-- the company admin makes: one row per (project, person).
--
-- Company admins and super admins are deliberately NOT listed here. They see
-- every project by definition — an admin who had to assign themselves before
-- they could see anything would be able to lock themselves out of their own
-- company. Only managers and staff are scoped by this table.
--
-- No rows for a person means no projects, which means they see nothing. Access
-- is granted here, never assumed.
CREATE TABLE project_members (
    id          bigserial   PRIMARY KEY,

    -- Unqualified: resolves to this company's schema via search_path.
    -- Dropping a project drops its assignments with it.
    project_id  bigint      NOT NULL REFERENCES projects(id) ON DELETE CASCADE,

    -- Qualified: accounts are cross-company and live in the admin schema.
    -- Deleting a user must not leave assignments pointing at nobody.
    user_id     bigint      NOT NULL REFERENCES admin.users(id) ON DELETE CASCADE,

    assigned_by bigint      REFERENCES admin.users(id) ON DELETE SET NULL,
    assigned_at timestamptz NOT NULL DEFAULT now(),

    -- Assigning the same person twice is a no-op, not a second row.
    UNIQUE (project_id, user_id)
);

-- The hot path is "which projects may this person see?", run on nearly every
-- request a manager or staff member makes.
CREATE INDEX idx_project_members_user ON project_members(user_id);
CREATE INDEX idx_project_members_project ON project_members(project_id);
