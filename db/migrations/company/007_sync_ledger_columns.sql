-- =============================================================================
-- 007_sync_ledger_columns.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Makes `transactions` carry exactly the same data columns as `temp_trans`.
--
-- The two tables are the same row at two stages of its life: staged, then
-- posted. Finalize copies one into the other, so any column on one and not the
-- other is a column finalize cannot carry. Deleting txn_date, description and
-- balance as fields dropped them from temp_trans and left them on
-- transactions, and finalize stopped working the moment it did:
--
--     SELECT t.txn_date ... FROM temp_trans t
--     -> UndefinedColumnError: column t.txn_date does not exist
--
-- So this does not name those three columns. It reads both tables and makes
-- the second match the first: add what is missing, drop what is extra. A
-- schema where the two already agree is left untouched, which is why this is
-- safe to run against every company at once.
--
-- The two internals lists are each table's own plumbing -- identity, the batch
-- link, the audit link, workflow flags, the master foreign keys. They are
-- deliberately NOT synced: temp_trans needs batch_id and row_hash, the ledger
-- needs temp_trans_id and bank_id, and neither belongs on the other.
-- =============================================================================

DO $$
DECLARE
    r record;

    -- temp_trans columns that are workflow machinery, not statement data.
    staging_internals text[] := ARRAY[
        'id', 'batch_id', 'row_hash', 'row_number', 'is_classified', 'raw_data',
        'created_at', 'updated_at',
        'project_id', 'beneficiary_id', 'head_id', 'rera_head_id', 'idw_head_id'
    ];

    -- transactions columns that are ledger machinery. bank_id lives here and
    -- not on temp_trans (it is carried from the batch at finalize), so it must
    -- survive the drop pass below.
    ledger_internals text[] := ARRAY[
        'id', 'temp_trans_id', 'created_at', 'updated_at',
        'project_id', 'bank_id', 'beneficiary_id', 'head_id', 'rera_head_id', 'idw_head_id'
    ];
BEGIN
    -- 1. Every data column on temp_trans that transactions is missing.
    --    format_type() reproduces the declared type exactly, so numeric(18,2)
    --    arrives as numeric(18,2) rather than a bare numeric.
    FOR r IN
        SELECT a.attname, format_type(a.atttypid, a.atttypmod) AS coltype
        FROM pg_attribute a
        WHERE a.attrelid = (current_schema() || '.temp_trans')::regclass
          AND a.attnum > 0 AND NOT a.attisdropped
          AND a.attname <> ALL (staging_internals)
          AND NOT EXISTS (
              SELECT 1 FROM pg_attribute b
              WHERE b.attrelid = (current_schema() || '.transactions')::regclass
                AND b.attnum > 0 AND NOT b.attisdropped
                AND b.attname = a.attname
          )
        ORDER BY a.attnum
    LOOP
        EXECUTE format('ALTER TABLE transactions ADD COLUMN %I %s', r.attname, r.coltype);
        RAISE NOTICE '  + transactions.% %', r.attname, r.coltype;
    END LOOP;

    -- 2. Every data column on transactions that temp_trans no longer has.
    FOR r IN
        SELECT a.attname
        FROM pg_attribute a
        WHERE a.attrelid = (current_schema() || '.transactions')::regclass
          AND a.attnum > 0 AND NOT a.attisdropped
          AND a.attname <> ALL (ledger_internals)
          AND NOT EXISTS (
              SELECT 1 FROM pg_attribute b
              WHERE b.attrelid = (current_schema() || '.temp_trans')::regclass
                AND b.attnum > 0 AND NOT b.attisdropped
                AND b.attname = a.attname
          )
        ORDER BY a.attnum
    LOOP
        EXECUTE format('ALTER TABLE transactions DROP COLUMN %I', r.attname);
        RAISE NOTICE '  - transactions.%', r.attname;
    END LOOP;
END $$;
