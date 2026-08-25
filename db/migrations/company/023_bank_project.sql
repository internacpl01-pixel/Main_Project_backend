-- =============================================================================
-- 023_bank_project.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Record which project a bank account belongs to, chosen from the Projects
-- list. Same shape as account_type (015) and company (016): the NAME is
-- stored, the value is picked from a dropdown so it cannot be mistyped, and
-- nothing here is a foreign key.
--
-- Deliberately NOT projects.id, even though a real reference is available and
-- the ledger uses one. project_id on transactions is scoped -- services/scoping
-- decides which projects a user may see, and a reference here would invite a
-- join that quietly bypasses that. This column is a label on a bank account,
-- not a claim about who may read it.
--
-- Optional. A bank account that serves the whole company belongs to no single
-- project, and that is a normal thing for one to do.
-- =============================================================================

ALTER TABLE bank_master
    ADD COLUMN IF NOT EXISTS project text;
