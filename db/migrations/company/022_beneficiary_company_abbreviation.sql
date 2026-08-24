-- =============================================================================
-- 022_beneficiary_company_abbreviation.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- beneficiary_master.company holds the company's ABBREVIATION from now on --
-- 'DPL' rather than 'DWARKADHIS PROJECTS PRIVATE LIMITED'.
--
-- The column stores a name rather than a reference (see 013), so switching
-- which name it stores means rewriting the rows that already carry the long
-- one. 169 of them on this install: 113 Ambition, 56 Dwarkadhis.
--
-- Matched on the full name and replaced with the abbreviation. A company with
-- no abbreviation is left alone -- there is nothing to put there, and blanking
-- the column would lose which company the payee belongs to in order to make a
-- display shorter.
--
-- Rows already holding an abbreviation are untouched, so this is safe to re-run
-- and safe on a company whose data was entered after the change: the WHERE
-- matches c.name, and an abbreviation does not equal a name.
--
-- bank_master.company gets the same treatment. Nothing on this install has one
-- set yet, but the two columns are filled from the same dropdown and one of
-- them quietly holding a different form of the same value is the kind of thing
-- that is only discovered when a report tries to group by it.
-- =============================================================================

UPDATE beneficiary_master b
   SET company = c.abbreviation
  FROM company_master c
 WHERE b.company = c.name
   AND c.abbreviation IS NOT NULL
   AND c.abbreviation <> '';

UPDATE bank_master k
   SET company = c.abbreviation
  FROM company_master c
 WHERE k.company = c.name
   AND c.abbreviation IS NOT NULL
   AND c.abbreviation <> '';
