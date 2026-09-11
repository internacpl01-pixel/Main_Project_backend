-- Drop the UNIQUE(account_head) constraint on both Farvision account tables.
-- The user's real master sheet legitimately repeats the same Account Head
-- more than once per company (229 such rows across DPL/AMB) and wants every
-- sheet row imported as its own row rather than collapsed -- so uniqueness
-- can no longer be enforced here.
ALTER TABLE farvision_account_master_dpl
    DROP CONSTRAINT farvision_account_master_dpl_account_head_key;

ALTER TABLE farvision_account_master_amb
    DROP CONSTRAINT farvision_account_master_amb_account_head_key;
