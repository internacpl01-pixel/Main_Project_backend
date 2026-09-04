-- =============================================================================
-- 035_head_names_not_unique.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Lets the same name be added more than once in Internal Head, RERA Head and
-- TCP Head. The user asked for this directly after finding the opposite:
-- adding a head from the Rules grid's new "Add head" button refused a repeat
-- name with "already used by another rera head", and that refusal is not
-- wanted.
--
-- Drops exactly the three UNIQUE constraints that caused it, one per master:
--
--     head_master_name_category_key   UNIQUE (name, category)
--     rera_head_master_name_key       UNIQUE (name)
--     idw_head_master_name_key        UNIQUE (name)
--
-- Nothing else on these tables changes. Each master keeps its primary key, so
-- two rows named the same thing are still two different ids -- a rule or a
-- fieldmap entry that points at one by id is unaffected, and Check Rules'
-- Replace dropdown will simply offer both if a company chooses to create both.
-- =============================================================================

ALTER TABLE head_master      DROP CONSTRAINT head_master_name_category_key;
ALTER TABLE rera_head_master DROP CONSTRAINT rera_head_master_name_key;
ALTER TABLE idw_head_master  DROP CONSTRAINT idw_head_master_name_key;
