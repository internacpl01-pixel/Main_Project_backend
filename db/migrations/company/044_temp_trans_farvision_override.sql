-- Backs the new "Farvision Verify" page: when a temp_trans row's Account
-- Head is ambiguous (a duplicate Account Head spelling, or -- for an
-- Internal transfer -- more than one candidate bank account), the page lets
-- someone pick the right one before exporting. That choice is written back
-- here so it is resolved for good -- confirmed with the user -- rather than
-- asked again the next time the same batch is exported.
--
-- Both columns, not one: farvision.py's Account Head and Parent Account
-- Head can differ even for the same duplicate group (confirmed live), so
-- the override has to carry both rather than re-deriving the parent from a
-- possibly-stale master lookup at export time.
ALTER TABLE temp_trans
    ADD COLUMN IF NOT EXISTS farvision_account_head_override text,
    ADD COLUMN IF NOT EXISTS farvision_parent_account_head_override text;
