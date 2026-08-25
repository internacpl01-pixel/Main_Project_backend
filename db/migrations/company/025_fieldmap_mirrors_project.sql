-- =============================================================================
-- 025_fieldmap_mirrors_project.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- BUSINESS UNIT is the Project column. Make it follow the Classify dialog.
--
-- 019 connected three of the five Classify pickers to the columns people
-- actually read: HEAD, TYPE FOR RERA IDW and TCP Head. Project was left out, so
-- picking a project set temp_trans.project_id -- correct, and what finalize
-- reads -- while the BUSINESS UNIT column on screen stayed an em dash forever.
-- From the table, choosing a project looked like it did nothing.
--
-- Same mechanism, two more values. `mirrors` names which classification a
-- fieldmap column reflects, per company and per column, so a company without
-- such a column still gets nothing written -- company_007 and company_017 have
-- no custom fields at all and must keep working untouched.
--
-- 'beneficiary' is allowed here too, though nothing is seeded to it: the
-- Classify dialog offers five pickers and the mechanism should cover all five,
-- so a company that adds a Beneficiary display column can point it at this by
-- setting `mirrors` on that one row. Widening the constraint is what makes that
-- possible; guessing which column meant it is not.
-- =============================================================================

ALTER TABLE fieldmap
    DROP CONSTRAINT IF EXISTS fieldmap_mirrors_check;

ALTER TABLE fieldmap
    ADD CONSTRAINT fieldmap_mirrors_check
    CHECK (mirrors IS NULL OR mirrors IN
           ('head', 'rera_head', 'idw_head', 'project', 'beneficiary'));

-- Exact, upper-cased, trimmed match only, as in 019. A fuzzy match here would
-- be a guess written into financial data.
--
-- Restricted to text columns because what gets written is a project's name. A
-- numeric or date column called BUSINESS UNIT cannot hold one, and pointing
-- `mirrors` at it would turn every classify into a type error.
--
-- The unique partial index from 019 still applies: at most one column per
-- company may mirror the project, so this cannot quietly claim a second one.
UPDATE fieldmap SET mirrors = 'project'
    WHERE upper(trim(displayname)) = 'BUSINESS UNIT'
      AND mirrors IS NULL
      AND fieldname ~ '^field_text_[0-9]+$';

-- Rows classified before this existed have a project_id and a blank Business
-- Unit. Fill them in, so the column reads correctly for the whole table rather
-- than only for rows classified from today on.
--
-- Blank cells only. Nothing anywhere has a Business Unit value today, so this
-- changes nothing that was imported -- but a statement that does carry the
-- column belongs to the bank, not to us, and a backfill nobody asked for should
-- not overwrite it. Classifying a row still writes what was picked: that is an
-- explicit act, and this is not.
DO $$
DECLARE
    col text;
    tbl text;
BEGIN
    SELECT fieldname INTO col
      FROM fieldmap
     WHERE mirrors = 'project'
       AND is_active = true
       AND fieldname ~ '^field_text_[0-9]+$'
     LIMIT 1;

    IF col IS NULL THEN
        RETURN;
    END IF;

    -- Both tables, because a row keeps its columns when it is posted and a
    -- ledger that disagreed with staging about the project would be worse than
    -- one that shows nothing.
    FOREACH tbl IN ARRAY ARRAY['temp_trans', 'transactions'] LOOP
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema()
               AND table_name = tbl
               AND column_name = col
        ) THEN
            EXECUTE format(
                'UPDATE %I t SET %I = p.name '
                '  FROM projects p '
                ' WHERE p.id = t.project_id '
                '   AND (t.%I IS NULL OR btrim(t.%I) = '''')',
                tbl, col, col, col
            );
        END IF;
    END LOOP;
END $$;
