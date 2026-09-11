-- Trim farvision_account_master_dpl/amb down to only the columns that are
-- actually related to each other: Account Head and Parent Account Head
-- (Company is already which table a row is in). Every other column the
-- original sheet carried -- Document Type, Financial Year, Bank Name,
-- Deduction Type, Description, EntryTypes, Debit/Credit, Payment Mode,
-- Payee Name, Docno, Invoice No, Business Unit -- was near-empty across the
-- real data (0 rows in AMB, at most a handful of example rows in DPL) and
-- was never genuinely tied to a specific Account Head the way Account Head/
-- Parent Account Head are. Those values move to a hardcoded reference list
-- in code instead of living here as mostly-blank columns.
ALTER TABLE farvision_account_master_dpl
    DROP COLUMN document_type,
    DROP COLUMN financial_year,
    DROP COLUMN bank_name,
    DROP COLUMN deduction_type,
    DROP COLUMN description,
    DROP COLUMN entry_types,
    DROP COLUMN debit_credit,
    DROP COLUMN payment_mode,
    DROP COLUMN payee_name,
    DROP COLUMN docno,
    DROP COLUMN invoice_no,
    DROP COLUMN business_unit;

ALTER TABLE farvision_account_master_amb
    DROP COLUMN document_type,
    DROP COLUMN financial_year,
    DROP COLUMN bank_name,
    DROP COLUMN deduction_type,
    DROP COLUMN description,
    DROP COLUMN entry_types,
    DROP COLUMN debit_credit,
    DROP COLUMN payment_mode,
    DROP COLUMN payee_name,
    DROP COLUMN docno,
    DROP COLUMN invoice_no,
    DROP COLUMN business_unit;
