-- Farvision chart of accounts: the master the Farvision export's Account Head
-- and Parent Account Head come from, fuzzy-matched against a row's narration.
-- Plain reference data -- no other table points at it, and nothing here is
-- computed from temp_trans or the head masters, because Farvision's ledger
-- names (specific vendors, customers, employees) are not the same list as
-- this company's own Internal/RERA/TCP heads.
CREATE TABLE farvision_account_master (
    id                   bigserial   PRIMARY KEY,
    company              text,
    account_head         text        NOT NULL,
    parent_account_head  text,
    document_type        text,
    financial_year       text,
    bank_name            text,
    deduction_type       text,
    description          text,
    entry_types          text,
    debit_credit         text,
    payment_mode         text,
    payee_name           text,
    docno                text,
    invoice_no           text,
    business_unit        text,
    is_active            boolean     NOT NULL DEFAULT true,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    UNIQUE (company, account_head)
);

CREATE TRIGGER farvision_account_master_set_updated_at
    BEFORE UPDATE ON farvision_account_master
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();
