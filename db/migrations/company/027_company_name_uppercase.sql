-- =============================================================================
-- 027_company_name_uppercase.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Company names are stored in capitals from now on -- the API upper-cases
-- whatever is typed, the same way it already does for the abbreviation beside
-- it and for account_type. This brings the rows entered before that into line,
-- so the Company tab does not show 'Ambition Colonisers Private Limited' next
-- to 'DWARKADHIS PROJECTS PRIVATE LIMITED'.
--
-- Why it matters beyond looks: company_master.name is UNIQUE, and a UNIQUE
-- index is case-sensitive. Two spellings of one company are two companies to
-- the database and to anything grouping by it, and that is the version of the
-- mistake nobody catches by reading.
--
-- Guarded against a collision, like 021. If a company somehow holds both
-- 'Testing' and 'TESTING' the update would fail and take the whole migration
-- with it. Those rows are left alone instead: two rows differing only by case
-- are a merge decision, not something a rename can settle, and one of them is
-- referenced somewhere while the other is not.
--
-- Nothing else needs bringing along. bank_master.company and
-- beneficiary_master.company store the ABBREVIATION, not the name (see 022 and
-- 024), and the beneficiary importer matches company names case-insensitively
-- -- both checked against this install before writing.
-- =============================================================================

UPDATE company_master a
   SET name = upper(name)
 WHERE name <> upper(name)
   AND NOT EXISTS (
       SELECT 1 FROM company_master b
        WHERE b.id <> a.id AND b.name = upper(a.name)
   );
