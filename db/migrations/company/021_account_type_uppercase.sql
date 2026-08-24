-- =============================================================================
-- 021_account_type_uppercase.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Account type names are stored in capitals from now on -- the API upper-cases
-- whatever is typed. This brings the rows entered before that into line, so the
-- Bank tab's dropdown does not show 'Master' next to 'IDW' and 'RERA'.
--
-- Guarded against a collision. account_type_master.name is UNIQUE, so if a
-- company somehow holds both 'Free' and 'FREE' the update would fail and take
-- the whole migration with it. Those rows are left alone instead: two rows that
-- differ only by case are a merge decision, not something a rename can settle,
-- and one is being used somewhere while the other is not.
--
-- bank_master.account_type stores the NAME rather than a reference (see 015),
-- so it is brought along in the same file. Nothing references an account type
-- on this install yet -- checked before writing -- but a migration that renames
-- one side and not the other is only correct by luck.
-- =============================================================================

UPDATE account_type_master a
   SET name = upper(name)
 WHERE name <> upper(name)
   AND NOT EXISTS (
       SELECT 1 FROM account_type_master b
        WHERE b.id <> a.id AND b.name = upper(a.name)
   );

UPDATE bank_master
   SET account_type = upper(account_type)
 WHERE account_type IS NOT NULL
   AND account_type <> upper(account_type);
