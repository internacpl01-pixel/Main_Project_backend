-- =============================================================================
-- 006_fieldmap_method.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Adds `method` to fieldmap: what a field is FOR, as opposed to what it holds.
--
-- A field's type says it is text; its aliases say the bank calls it
-- "Narration"; nothing so far says whether it exists to be read off a
-- statement, to be picked from a dropdown during review, or to be tested by a
-- classification rule. That is what this records.
--
-- Deliberately free text with no CHECK constraint. The vocabulary is the
-- user's -- "import", "selection", "rule" are the ones in mind today, and a
-- constraint would mean a migration every time a new one is wanted. The UI
-- suggests the known values without restricting the input.
--
-- Empty string rather than NULL as the default so existing rows read as
-- "unset" without every consumer needing a null check.
-- =============================================================================

ALTER TABLE fieldmap
    ADD COLUMN IF NOT EXISTS method text NOT NULL DEFAULT '';
