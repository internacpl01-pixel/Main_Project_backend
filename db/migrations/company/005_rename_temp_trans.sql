-- =============================================================================
-- 005_rename_temp_trans.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Renames temp_import -> temp_trans.
--
-- Postgres renames a table without touching the names of anything attached to
-- it: the primary key stays temp_import_pkey, the sequence stays
-- temp_import_id_seq, every index and foreign key keeps its old name. Those
-- stale names are what you actually read in an error message six months from
-- now ("duplicate key value violates unique constraint temp_import_pkey" on a
-- table called temp_trans), so each one is renamed explicitly here.
--
-- transactions.temp_import_id is renamed too. A foreign key named after a table
-- that no longer exists is worse than either name on its own.
-- =============================================================================

-- --- The table ----------------------------------------------------------------
ALTER TABLE temp_import RENAME TO temp_trans;

-- --- The referencing column on the ledger ---------------------------------------
ALTER TABLE transactions RENAME COLUMN temp_import_id TO temp_trans_id;

-- --- Identity: primary key and its sequence ------------------------------------
ALTER INDEX    temp_import_pkey   RENAME TO temp_trans_pkey;
ALTER SEQUENCE temp_import_id_seq RENAME TO temp_trans_id_seq;

-- --- Indexes on temp_trans -------------------------------------------------------
ALTER INDEX idx_temp_import_batch_id   RENAME TO idx_temp_trans_batch_id;
ALTER INDEX idx_temp_import_classified RENAME TO idx_temp_trans_classified;
ALTER INDEX idx_temp_import_head_id    RENAME TO idx_temp_trans_head_id;
ALTER INDEX idx_temp_import_row_hash   RENAME TO idx_temp_trans_row_hash;
ALTER INDEX temp_import_batch_row_uq   RENAME TO temp_trans_batch_row_uq;

-- --- Constraints on temp_trans ---------------------------------------------------
ALTER TABLE temp_trans RENAME CONSTRAINT temp_import_batch_id_fkey       TO temp_trans_batch_id_fkey;
ALTER TABLE temp_trans RENAME CONSTRAINT temp_import_beneficiary_id_fkey TO temp_trans_beneficiary_id_fkey;
ALTER TABLE temp_trans RENAME CONSTRAINT temp_import_credit_debit_check  TO temp_trans_credit_debit_check;
ALTER TABLE temp_trans RENAME CONSTRAINT temp_import_head_id_fkey        TO temp_trans_head_id_fkey;
ALTER TABLE temp_trans RENAME CONSTRAINT temp_import_idw_head_id_fkey    TO temp_trans_idw_head_id_fkey;
ALTER TABLE temp_trans RENAME CONSTRAINT temp_import_project_id_fkey     TO temp_trans_project_id_fkey;
ALTER TABLE temp_trans RENAME CONSTRAINT temp_import_rera_head_id_fkey   TO temp_trans_rera_head_id_fkey;

-- --- Constraints and index on transactions ---------------------------------------
-- transactions_temp_import_id_key is the UNIQUE that makes a double-click on
-- Finalize physically unable to post the same staged row twice. Keep its
-- meaning obvious by keeping its name accurate.
ALTER TABLE transactions RENAME CONSTRAINT transactions_temp_import_id_fkey TO transactions_temp_trans_id_fkey;
ALTER TABLE transactions RENAME CONSTRAINT transactions_temp_import_id_key  TO transactions_temp_trans_id_key;
ALTER INDEX idx_transactions_temp_import_id RENAME TO idx_transactions_temp_trans_id;

-- --- Trigger -----------------------------------------------------------------------
ALTER TRIGGER temp_import_set_updated_at ON temp_trans RENAME TO temp_trans_set_updated_at;
