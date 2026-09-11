-- A flat list of Farvision Bank Names -- one plain reference list, the same
-- shape as account_type_master, with no link back to a specific Account Head
-- or company (confirmed with the user: Bank Name in the Farvision sheet
-- wasn't genuinely tied to one Account Head anyway, so a flat list is all
-- this needs to be).
--
-- Structure only, left empty on purpose -- this migration runs against every
-- company schema, but the real 45 bank names live only in company_028's own
-- "Copy of Master MASTERS.xlsx" sheet and are DPL/AMB-specific, so seeding
-- them here would put someone else's bank names in every other company's
-- schema. They're loaded into company_028 separately, the same way the
-- Farvision Account data was.
CREATE TABLE IF NOT EXISTS farvision_bank_name_master (
    id         bigserial   PRIMARY KEY,
    name       text        NOT NULL UNIQUE,
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

DROP TRIGGER IF EXISTS farvision_bank_name_master_set_updated_at ON farvision_bank_name_master;
CREATE TRIGGER farvision_bank_name_master_set_updated_at
    BEFORE UPDATE ON farvision_bank_name_master
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();
