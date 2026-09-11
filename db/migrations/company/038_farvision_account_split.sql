-- Split farvision_account_master into one table per company (DPL, AMB).
-- The same Account Head name legitimately exists once per company (each
-- company keeps its own Farvision chart of accounts), so a combined table
-- makes two unrelated rows look like duplicates the moment anyone browses
-- it without also reading the Company column. Two tables makes that
-- distinction structural instead of something the UI has to explain.
--
-- 'company' is dropped from both -- which table a row is in already answers
-- that question, and keeping the column would just be repeating it.
CREATE TABLE farvision_account_master_dpl (
    id                   bigserial   PRIMARY KEY,
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
    UNIQUE (account_head)
);

CREATE TABLE farvision_account_master_amb (
    LIKE farvision_account_master_dpl INCLUDING ALL
);

CREATE TRIGGER farvision_account_master_dpl_set_updated_at
    BEFORE UPDATE ON farvision_account_master_dpl
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

CREATE TRIGGER farvision_account_master_amb_set_updated_at
    BEFORE UPDATE ON farvision_account_master_amb
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

INSERT INTO farvision_account_master_dpl
    (account_head, parent_account_head, document_type, financial_year,
     bank_name, deduction_type, description, entry_types, debit_credit,
     payment_mode, payee_name, docno, invoice_no, business_unit, is_active,
     created_at, updated_at)
SELECT account_head, parent_account_head, document_type, financial_year,
       bank_name, deduction_type, description, entry_types, debit_credit,
       payment_mode, payee_name, docno, invoice_no, business_unit, is_active,
       created_at, updated_at
  FROM farvision_account_master
 WHERE upper(btrim(coalesce(company, ''))) = 'DPL'
ON CONFLICT (account_head) DO NOTHING;

INSERT INTO farvision_account_master_amb
    (account_head, parent_account_head, document_type, financial_year,
     bank_name, deduction_type, description, entry_types, debit_credit,
     payment_mode, payee_name, docno, invoice_no, business_unit, is_active,
     created_at, updated_at)
SELECT account_head, parent_account_head, document_type, financial_year,
       bank_name, deduction_type, description, entry_types, debit_credit,
       payment_mode, payee_name, docno, invoice_no, business_unit, is_active,
       created_at, updated_at
  FROM farvision_account_master
 WHERE upper(btrim(coalesce(company, ''))) = 'AMB'
ON CONFLICT (account_head) DO NOTHING;

DROP TABLE farvision_account_master;
