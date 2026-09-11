-- Marks which row wins when two Account Head rows in the same company table
-- are the same real party spelled differently (e.g. "India Pride Com" vs
-- "INDIA PRIDE.COM") -- confirmed live: normalizing every Account Head
-- (uppercase, punctuation stripped, whitespace collapsed) and grouping found
-- 412 such groups in DPL and 416 in AMB, almost all pure formatting drift
-- from the source sheet, not distinct parties.
--
-- Nothing is deleted or deactivated here: every row the sheet had stays, so
-- historical exports that already used a non-canonical spelling keep making
-- sense. is_canonical only decides which spelling farvision.py's matching
-- prefers going forward, and defaults true so a freshly imported row (or one
-- with no duplicate) needs no admin action before it can match.
ALTER TABLE farvision_account_master_dpl
    ADD COLUMN IF NOT EXISTS is_canonical boolean NOT NULL DEFAULT true;

ALTER TABLE farvision_account_master_amb
    ADD COLUMN IF NOT EXISTS is_canonical boolean NOT NULL DEFAULT true;
