-- =============================================================================
-- 019_fieldmap_mirrors.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Connect the Classify dialog to the columns people actually look at.
--
-- temp_trans carries two unrelated sets of columns today:
--
--   head_id / rera_head_id / idw_head_id   written by POST /temp-trans/{id}/classify,
--                                          read by finalize to build the ledger row
--   field_text_5 "HEAD", field_text_6      custom fields from the fieldmap, shown on
--   "TYPE FOR RERA IDW", field_text_7      the staging screen, filled only by import
--   "TCP Head"
--
-- Nothing joined them, so classifying a row set the ids correctly and left the
-- visible columns showing an em dash forever. From the screen, Classify did
-- nothing at all.
--
-- `mirrors` names which classification a fieldmap column reflects. It is per
-- company and per column, so a company that has no such column simply gets
-- nothing written -- company_007 and company_017 have no custom fields at all
-- and must keep working untouched.
--
-- Matching on displayname at runtime was the alternative and is worse: renaming
-- "HEAD" to "Expense Head" on the Fieldmap screen would silently disconnect it
-- again, with no error and no way to tell from the UI. A column that says what
-- it mirrors survives being renamed.
-- =============================================================================

ALTER TABLE fieldmap
    ADD COLUMN IF NOT EXISTS mirrors text;

ALTER TABLE fieldmap
    DROP CONSTRAINT IF EXISTS fieldmap_mirrors_check;

ALTER TABLE fieldmap
    ADD CONSTRAINT fieldmap_mirrors_check
    CHECK (mirrors IS NULL OR mirrors IN ('head', 'rera_head', 'idw_head'));

-- At most one column may mirror each classification. Two columns claiming to be
-- "the head column" would both be written and neither would be wrong, which is
-- the kind of duplicate that is only noticed once the numbers are reported.
CREATE UNIQUE INDEX IF NOT EXISTS fieldmap_mirrors_unique
    ON fieldmap (mirrors) WHERE mirrors IS NOT NULL;

-- Seed from the display names in use on this install. Exact, upper-cased,
-- trimmed matches only: a fuzzy match here would be a guess written into
-- financial data.
--
-- A company whose columns are named differently gets nothing and keeps behaving
-- as it does now; set `mirrors` by hand for it rather than widening this.
UPDATE fieldmap SET mirrors = 'head'
    WHERE upper(trim(displayname)) = 'HEAD' AND mirrors IS NULL;

UPDATE fieldmap SET mirrors = 'rera_head'
    WHERE upper(trim(displayname)) = 'TYPE FOR RERA IDW' AND mirrors IS NULL;

UPDATE fieldmap SET mirrors = 'idw_head'
    WHERE upper(trim(displayname)) = 'TCP HEAD' AND mirrors IS NULL;
