-- =============================================================================
-- 026_financial_year_column.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- A Financial Year column on the staged rows and the ledger, derived from the
-- date each row carries. The Indian financial year runs 1 April to 31 March, so
-- 2025-04-01 through 2026-03-31 is "FY 25-26".
--
-- Derived, never typed. The date is already on the row, so a person entering
-- the year is a person who can enter the wrong one -- and the one row where
-- that happens is a row that drops out of a year-end report without anything
-- looking wrong.
--
-- The column is an ordinary custom field, created the same way the Custom
-- Fields page creates one: a real text column on BOTH temp_trans and
-- transactions, plus a fieldmap row naming it. That is what makes it appear on
-- the Imported Rows and Ledger screens, sortable and filterable, with no change
-- to either page.
--
-- Column numbering is taken across both tables. company_018 has field_text_17
-- as its highest on temp_trans and field_text_18 on transactions; numbering
-- from temp_trans alone would pick 18 and collide on the ledger side, leaving
-- the two tables holding different things under one name.
-- =============================================================================

DO $$
DECLARE
    col      text;
    next_n   int;
    date_col text;
    expr     text;
BEGIN
    -- Re-runnable: if this company already has an FY field, reuse it rather
    -- than adding a second one under a different number.
    SELECT fieldname INTO col
      FROM fieldmap
     WHERE upper(btrim(displayname)) IN ('FY', 'FINANCIAL YEAR', 'FIN YEAR')
       AND fieldname ~ '^field_text_[0-9]+$'
     ORDER BY id
     LIMIT 1;

    IF col IS NULL THEN
        SELECT coalesce(max(substring(column_name from 'field_text_([0-9]+)$')::int), 0) + 1
          INTO next_n
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name IN ('temp_trans', 'transactions')
           AND column_name ~ '^field_text_[0-9]+$';

        col := 'field_text_' || next_n;

        INSERT INTO fieldmap (fieldname, displayname, mapfields, data_type, method)
        VALUES (col, 'FY', 'fy,financial year,fin year', 'text', 'rule')
        ON CONFLICT (fieldname) DO NOTHING;
    END IF;

    -- IF NOT EXISTS on both, so a company that has the fieldmap row but lost a
    -- column on one side is repaired rather than skipped.
    EXECUTE format('ALTER TABLE temp_trans   ADD COLUMN IF NOT EXISTS %I text', col);
    EXECUTE format('ALTER TABLE transactions ADD COLUMN IF NOT EXISTS %I text', col);

    -- ---- Backfill ---------------------------------------------------------
    -- The date column, taken as the first DATE-typed custom field on the table.
    -- At runtime the fieldmap resolves this properly, by category; here the
    -- simple rule is enough and it is the only one expressible in plain SQL.
    -- A company with no date field gets the column and no values, which is the
    -- correct outcome rather than an error.
    SELECT column_name INTO date_col
      FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'temp_trans'
       AND data_type = 'date'
       AND column_name ~ '^field_date_[0-9]+$'
     ORDER BY ordinal_position
     LIMIT 1;

    IF date_col IS NULL THEN
        RETURN;
    END IF;

    -- Shift back three months to land on the year the FY started, forward nine
    -- to land on the year it ends. 2026-03-31 goes to 2025 and 2026; the next
    -- day goes to 2026 and 2027. No CASE on the month, and it crosses a century
    -- correctly -- 2000-03-31 is FY 99-00.
    expr := format(
        '''FY '' || to_char(t.%I - interval ''3 months'', ''YY'') || ''-'' || '
        'to_char(t.%I + interval ''9 months'', ''YY'')', date_col, date_col);

    -- Blank cells only. Nothing should be in this column yet, but a value that
    -- somehow is came from the statement, and a backfill is not the place to
    -- overwrite it.
    EXECUTE format(
        'UPDATE temp_trans t SET %I = %s '
        ' WHERE t.%I IS NOT NULL AND (t.%I IS NULL OR btrim(t.%I) = '''')',
        col, expr, date_col, col, col);

    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'transactions' AND column_name = date_col
    ) THEN
        EXECUTE format(
            'UPDATE transactions t SET %I = %s '
            ' WHERE t.%I IS NOT NULL AND (t.%I IS NULL OR btrim(t.%I) = '''')',
            col, expr, date_col, col, col);
    END IF;
END $$;
