-- =============================================================================
-- 024_bank_company_abbreviation.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- bank_master.company holds the abbreviation, matching beneficiary_master since
-- 022. 'DPL' rather than 'DWARKADHIS PROJECTS PRIVATE LIMITED'.
--
-- 022 already carried this same UPDATE and it did nothing, because at that
-- point no bank row had a company set. Six were filled in afterwards, so it
-- needs running again -- and a migration that has already been applied cannot
-- be edited to catch them: migrate.py records a checksum per file and would
-- refuse the changed one.
--
-- Same guard as 022: only rows matching a company's full NAME are rewritten, so
-- anything already holding an abbreviation is left alone and this is safe to
-- re-run. A company with no abbreviation keeps the long name, because blanking
-- it would lose which company the account belongs to in order to make a column
-- narrower.
-- =============================================================================

UPDATE bank_master k
   SET company = c.abbreviation
  FROM company_master c
 WHERE k.company = c.name
   AND c.abbreviation IS NOT NULL
   AND c.abbreviation <> '';
