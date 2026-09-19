-- The Farvision Verify page's own "TDS Rate" column -- a manually entered
-- note next to Description (e.g. "1%", "2%", "10%") for the person reviewing
-- the batch, confirmed with the user as display-only: it never reaches the
-- Farvision export, so it isn't one of farvision.py's COLUMNS at all, just a
-- value that sticks on temp_trans the same way the other Verify-page
-- overrides do.
ALTER TABLE temp_trans
    ADD COLUMN IF NOT EXISTS farvision_tds_rate_override text;
