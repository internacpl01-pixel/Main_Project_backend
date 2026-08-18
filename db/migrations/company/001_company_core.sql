-- =============================================================================
-- 001_company_core.sql
-- The 9-table company schema template.
-- Applied to each company_NNN schema by db.migrate.py.
--
-- IMPORTANT: NO {{schema}} substitutions. The runner does
--   SET LOCAL search_path TO "company_001", admin
-- before running it. Unqualified CREATE TABLE lands in the right schema
-- because of search_path, not string-substitution.
-- This keeps the file readable and removes a whole class of bugs.
-- =============================================================================

-- 1. projects (referenced by transactions; no FKs in)
CREATE TABLE projects (
    id         bigserial   PRIMARY KEY,
    name       text        NOT NULL,
    code       text        UNIQUE,
    address    text,
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER projects_set_updated_at
    BEFORE UPDATE ON projects
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

-- 2. bank_master (one row per bank account you upload statements from)
CREATE TABLE bank_master (
    id            bigserial   PRIMARY KEY,
    bank_name     text        NOT NULL,
    account_number text,
    ifsc_code     text,
    is_active     boolean     NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (bank_name, account_number)
);

CREATE TRIGGER bank_master_set_updated_at
    BEFORE UPDATE ON bank_master
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

-- 3. beneficiary_master (people/companies you pay or receive from)
CREATE TABLE beneficiary_master (
    id         bigserial   PRIMARY KEY,
    name       text        NOT NULL,
    category   text,
    pan        text,
    gstin      text,
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER beneficiary_master_set_updated_at
    BEFORE UPDATE ON beneficiary_master
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

-- 4. head_master (general ledger heads: "Brokerage", "Site Expenses", ...)
CREATE TABLE head_master (
    id         bigserial   PRIMARY KEY,
    name       text        NOT NULL,
    category   text,
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (name, category)
);

CREATE TRIGGER head_master_set_updated_at
    BEFORE UPDATE ON head_master
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

-- 5. rera_head_master (RERA-specific heads)
CREATE TABLE rera_head_master (
    id         bigserial   PRIMARY KEY,
    name       text        NOT NULL UNIQUE,
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER rera_head_master_set_updated_at
    BEFORE UPDATE ON rera_head_master
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

-- 6. idw_head_master (IDW-specific heads)
CREATE TABLE idw_head_master (
    id         bigserial   PRIMARY KEY,
    name       text        NOT NULL UNIQUE,
    is_active  boolean     NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TRIGGER idw_head_master_set_updated_at
    BEFORE UPDATE ON idw_head_master
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

-- 7. import_batches (one row per uploaded file)
--    - file_hash: sha256 of original file (dedup against re-uploads)
--    - UNIQUE (file_hash): physically impossible to re-upload same file
CREATE TABLE import_batches (
    id             bigserial   PRIMARY KEY,
    filename       text        NOT NULL,
    file_hash      text        NOT NULL,   -- sha256 of original file (dedup)
    bank_id        bigint      REFERENCES bank_master(id) ON DELETE RESTRICT,
    uploaded_by    text        NOT NULL,   -- username
    uploaded_at    timestamptz NOT NULL DEFAULT now(),
    row_count      integer     NOT NULL DEFAULT 0,
    status         text        NOT NULL DEFAULT 'uploaded'
        CHECK (status IN ('uploaded', 'classified', 'finalized', 'failed')),
    failure_reason text,
    UNIQUE (file_hash) -- physically impossible to re-upload the same file
);

CREATE INDEX idx_import_batches_bank_id ON import_batches(bank_id);

CREATE TRIGGER import_batches_set_updated_at
    BEFORE UPDATE ON import_batches
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

-- 8. temp_import (raw rows from PDF, before classification/finalization)
--    - row_hash: sha256 of (date + desc + amount + batch) for row-level dedup
--    - UNIQUE (row_hash): same row can't be imported twice
CREATE TABLE temp_import (
    id             bigserial   PRIMARY KEY,
    batch_id       bigint      NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
    row_hash       text        NOT NULL,         -- sha256 of (date + desc + amount + batch)
    txn_date       date,
    description    text,
    amount         numeric(18,2),
    credit_debit   text        CHECK (credit_debit IN ('CR', 'DR')),
    balance        numeric(18,2),
    is_classified  boolean     NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (row_hash)
);

CREATE INDEX idx_temp_import_batch_id    ON temp_import(batch_id);
CREATE INDEX idx_temp_import_classified ON temp_import(is_classified);

CREATE TRIGGER temp_import_set_updated_at
    BEFORE UPDATE ON temp_import
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

-- 9. transactions (the ledger -- the only table that matters at month-end)
--    - UNIQUE (temp_import_id): makes double-Finalize physically impossible.
--      Re-clicking the button raises a constraint violation, not a silent
--      double-post that breaks your books.
CREATE TABLE transactions (
    id              bigserial   PRIMARY KEY,
    txn_date        date        NOT NULL,
    description     text,
    amount          numeric(18,2) NOT NULL,
    credit_debit    text        NOT NULL CHECK (credit_debit IN ('CR', 'DR')),
    balance         numeric(18,2),
    project_id      bigint      REFERENCES projects(id)               ON DELETE RESTRICT,
    bank_id         bigint      REFERENCES bank_master(id)            ON DELETE RESTRICT,
    beneficiary_id  bigint      REFERENCES beneficiary_master(id)     ON DELETE RESTRICT,
    head_id         bigint      REFERENCES head_master(id)            ON DELETE RESTRICT,
    rera_head_id    bigint      REFERENCES rera_head_master(id)       ON DELETE RESTRICT,
    idw_head_id     bigint      REFERENCES idw_head_master(id)        ON DELETE RESTRICT,
    temp_import_id  bigint      NOT NULL REFERENCES temp_import(id)   ON DELETE RESTRICT,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (temp_import_id)   -- double-Finalize protection
);

CREATE INDEX idx_transactions_txn_date      ON transactions(txn_date);
CREATE INDEX idx_transactions_project_id   ON transactions(project_id);
CREATE INDEX idx_transactions_head_id      ON transactions(head_id);
CREATE INDEX idx_transactions_temp_import_id ON transactions(temp_import_id);

CREATE TRIGGER transactions_set_updated_at
    BEFORE UPDATE ON transactions
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();