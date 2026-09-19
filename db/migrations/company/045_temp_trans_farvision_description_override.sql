-- Lets a person on the Farvision Verify page override the auto-computed TDS
-- Description/Deduction Type for one row -- e.g. the row's existing head
-- implies "TDS ON CONTRACTORS" but no TDS is actually due this time, and the
-- real answer is "TDS PAYABLE (NIL)". Same pattern as
-- farvision_account_head_override (044): written back onto temp_trans so it
-- sticks for good, confirmed with the user, rather than only for the export
-- about to run.
ALTER TABLE temp_trans
    ADD COLUMN IF NOT EXISTS farvision_description_override text;
