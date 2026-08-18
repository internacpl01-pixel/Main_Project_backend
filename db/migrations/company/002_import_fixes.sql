-- =============================================================================
-- 002_import_fixes.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- 001 is immutable -- it is already applied and checksum-locked, so every
-- correction lands in a new numbered file. This one fixes three defects that
-- make the import pipeline impossible to run, and adds the columns the PDF
-- importer (ported from DPL_project) needs.
-- =============================================================================


-- --- FIX 1 -------------------------------------------------------------------
-- import_batches had a BEFORE UPDATE trigger calling admin.set_updated_at(),
-- which does  NEW.updated_at = now()  -- but the table has no updated_at column.
-- Every UPDATE failed with:  record "new" has no field "updated_at"
-- The import flow must move status uploaded -> classified -> finalized, so this
-- blocked the entire pipeline.
ALTER TABLE import_batches
    ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();


-- --- FIX 2 -------------------------------------------------------------------
-- temp_import was missing every classification column. routers/transactions.py
-- selects and updates head_id, rera_head_id, idw_head_id and project_id on this
-- table; none existed, so /temp-import, /classify and /finalize all failed with
-- column "head_id" does not exist.
--
-- ON DELETE SET NULL (not RESTRICT as on transactions): temp_import is staging.
-- Deleting a head should orphan a draft classification, not block it. The
-- posted ledger keeps RESTRICT because that data is permanent.
ALTER TABLE temp_import
    ADD COLUMN IF NOT EXISTS project_id     bigint REFERENCES projects(id)           ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS beneficiary_id bigint REFERENCES beneficiary_master(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS head_id        bigint REFERENCES head_master(id)        ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS rera_head_id   bigint REFERENCES rera_head_master(id)   ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS idw_head_id    bigint REFERENCES idw_head_master(id)    ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_temp_import_head_id ON temp_import(head_id);


-- --- FIX 3 -------------------------------------------------------------------
-- Row-level dedup was at the wrong level. 001 had a global UNIQUE (row_hash).
-- Two legitimately identical rows in one statement -- say two 500.00 UPI debits
-- on the same day with the same narration -- hash identically, and the second
-- INSERT aborts the whole batch. Re-importing an overlapping statement period
-- fails the same way. Real bank statements contain both cases.
--
-- Correct layering:
--   file level      import_batches.file_hash UNIQUE   hard block, kept from 001
--   row identity    UNIQUE (batch_id, row_number)     position within the file
--   row content     row_hash, indexed but NOT unique  soft duplicate warning
ALTER TABLE temp_import
    ADD COLUMN IF NOT EXISTS row_number integer;

-- Backfill any rows that predate this column so the NOT NULL below can hold.
UPDATE temp_import SET row_number = id WHERE row_number IS NULL;

ALTER TABLE temp_import ALTER COLUMN row_number SET NOT NULL;

ALTER TABLE temp_import DROP CONSTRAINT IF EXISTS temp_import_row_hash_key;

CREATE UNIQUE INDEX IF NOT EXISTS temp_import_batch_row_uq
    ON temp_import (batch_id, row_number);

CREATE INDEX IF NOT EXISTS idx_temp_import_row_hash
    ON temp_import (row_hash);


-- --- Parser output retention --------------------------------------------------
-- The PDF parser returns every column the bank actually printed: cheque number,
-- reference number, value date, branch. temp_import has five fixed columns, so
-- everything else would be dropped on the floor. Keeping the original parsed row
-- means a disputed import can be reconstructed without re-reading the PDF, and a
-- new column can be promoted later from data already captured.
ALTER TABLE temp_import
    ADD COLUMN IF NOT EXISTS raw_data jsonb;

-- Which headers the parser detected, what it could not map, and its timing
-- counters. Stored per batch so a bad import can be diagnosed after the fact.
ALTER TABLE import_batches
    ADD COLUMN IF NOT EXISTS parse_stats jsonb;
