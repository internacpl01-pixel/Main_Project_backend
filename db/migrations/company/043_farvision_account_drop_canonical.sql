-- Reverts 042_farvision_account_canonical.sql. is_canonical was meant to let
-- an admin pre-pick which duplicate spelling of an Account Head ("India
-- Pride Com" vs "INDIA PRIDE.COM") the export should prefer -- but the
-- export was then changed to always leave an ambiguous match blank with a
-- dropdown of every spelling, regardless of which one was marked canonical.
-- That made the flag (and the Master Data page panel that set it) do
-- nothing observable, so both are removed rather than kept around unused.
ALTER TABLE farvision_account_master_dpl
    DROP COLUMN IF EXISTS is_canonical;

ALTER TABLE farvision_account_master_amb
    DROP COLUMN IF EXISTS is_canonical;
