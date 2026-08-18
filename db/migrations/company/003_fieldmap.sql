-- =============================================================================
-- 003_fieldmap.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- The column-alias table that drives the PDF/Excel importer ported from
-- DPL_project. parsers.py contains no bank-specific logic at all -- it matches a
-- statement's printed column headers against the aliases in this table. A bank
-- whose statement says "Withdrawal Amt." imports because that string is listed
-- under the withdrawal field, not because anything was coded for that bank.
--
-- One copy per company schema, deliberately. Different developers bank with
-- different banks; a new statement format becomes a row here instead of a code
-- change and a redeploy.
-- =============================================================================

CREATE TABLE IF NOT EXISTS fieldmap (
    id          bigserial   PRIMARY KEY,
    fieldname   text        NOT NULL UNIQUE,
    displayname text        NOT NULL,
    mapfields   text        NOT NULL DEFAULT '',
    data_type   text        NOT NULL DEFAULT 'text',
    is_active   boolean     NOT NULL DEFAULT true,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),

    -- Drives the parser's type-aware coercion. In DPL this came from
    -- information_schema on the master table; here temp_import has fixed
    -- columns, so the type is declared per field instead.
    CONSTRAINT fieldmap_data_type_valid
        CHECK (data_type IN ('date', 'text', 'numeric'))
);

DROP TRIGGER IF EXISTS fieldmap_set_updated_at ON fieldmap;
CREATE TRIGGER fieldmap_set_updated_at
    BEFORE UPDATE ON fieldmap
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();


-- --- Seed --------------------------------------------------------------------
-- fieldname values are NOT arbitrary. parsers.py resolves each column's
-- semantic role through _CATEGORY_VOCABULARY, which recognises exactly these
-- concept names: date, description, withdrawal, deposits, balance,
-- reference_no. Renaming a fieldname below silently disables the parser's
-- balance-chain scoring and row-repair logic for that column.
--
-- mapfields is a comma-separated alias list. Matching is normalised
-- (lowercased, punctuation stripped), so "Withdrawal Amt." matches
-- "withdrawal amt". Priority is exact > starts-with > contains, longest first.
INSERT INTO fieldmap (fieldname, displayname, mapfields, data_type) VALUES
    ('txn_date',     'Date',         'date,txn date,tran date,transaction date,value date,entry date,posting date,date of transaction,trans date', 'date'),
    ('description',  'Description',  'description,desc,particulars,narration,narrations,remarks,transaction details,transaction particulars,transaction remarks', 'text'),
    ('reference_no', 'Reference No', 'reference no,ref no,ref,reference,chq no,cheque no,chq ref no,cheque ref no,instrument no,utr,utr no,transaction id', 'text'),
    ('withdrawal',   'Withdrawal',   'withdrawal,withdrawals,withdrawal amt,withdrawal amount,debit,debit amount,debit amt,dr,dr amount,amount out,paid out', 'numeric'),
    ('deposits',     'Deposits',     'deposits,deposit,deposit amt,deposit amount,credit,credit amount,credit amt,cr,cr amount,amount in,paid in', 'numeric'),
    ('balance',      'Balance',      'balance,closing balance,available balance,running balance,balance amt,balance amount', 'numeric')
ON CONFLICT (fieldname) DO NOTHING;
