-- =============================================================================
-- 032_rule_single_master.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Undoes 031_rule_multi_master.sql and puts the grid back on one table.
--
-- WHAT 031 DID AND WHY IT IS BEING REVERSED
--
-- 031 split `rule` into rule_head / rule_rera_head / rule_idw_head, one per
-- master, unified by a view `rule_v`, and taught services/rules.py to pick a
-- table from the account type. The motivation was real -- a MASTER account
-- carries head_id, not rera_head_id, so a grid of RERA heads cannot judge one
-- -- but the implementation went against two things this project holds to:
--
--   1. It hardcoded the account types. TARGET_BY_TYPE and _RULE_TABLES_BY_TYPE
--      listed MASTER, RERA, IDW, TCP and FREE by name, with everything else
--      falling back to the RERA master. Account types come from each company's
--      own account_type_master -- company_028 alone also has AMB, DPL and MERA
--      -- so any type not in that list silently got RERA's heads offered for it
--      and RERA's column written to. The standing rule here is that nothing is
--      keyed to a particular company's words.
--
--   2. It was a second design for a feature that had just been built to a
--      different one. 028 made the grid deliberately single-master, and said so
--      out loud: rera_head_id is the column Check Rules writes, so a head from
--      another master could be shown and then not saved. If a second master
--      ever earns rules, the plan of record is a per-rule target column -- one
--      table, one more field -- not three tables, a view, and a name-keyed map.
--
-- The split also cost every company its grid. 031 copies `rule` into
-- rule_rera_head and then drops `rule`, which is correct exactly once; it was
-- re-run by a hand-written script that dropped rule_rera_head first, so the
-- second pass found no `rule` to copy from and left every schema with empty
-- tables. That is a migration runner's job precisely because it refuses to
-- apply a file twice. Nothing outside `python -m db.migrate upgrade` should
-- ever write admin.schema_migrations.
--
-- WHAT THIS FILE DOES
--
--   1. refuses to run if rule_head or rule_idw_head holds anything, so a
--      schema that did put data there is never silently emptied,
--   2. recreates `rule` exactly as 028 and 029 left it,
--   3. copies rule_rera_head back into it,
--   4. drops rule_v and the three tables,
--   5. puts rule_condition_head back to a real foreign key on rera_head_master,
--   6. re-seeds the three rows 028 seeded, for the companies whose grid was
--      emptied. ON CONFLICT DO NOTHING, so a company that still has them keeps
--      exactly what it has.
--
-- Cells entered by hand since 028 cannot be recovered from here -- they were
-- data, not schema. They are restored separately, per company, from a record of
-- what those companies held before the wipe.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1.  Refuse rather than discard.
--
-- These two tables have no home in a single-master grid: `rule.head_id`
-- references rera_head_master, so a row pointing at head_master or
-- idw_head_master cannot be carried across. Every schema was checked before
-- this was written and all three tables were empty everywhere, which is why a
-- hard stop is safe to ask for -- if it ever fires, somebody has data that
-- needs a decision, not a DROP.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    stray bigint;
BEGIN
    IF to_regclass(current_schema() || '.rule_head') IS NOT NULL THEN
        EXECUTE 'SELECT count(*) FROM rule_head' INTO stray;
        IF stray > 0 THEN
            RAISE EXCEPTION
                'rule_head holds % row(s) in %. A single-master grid cannot '
                'carry heads from head_master. Decide what happens to them '
                'before running this migration.', stray, current_schema();
        END IF;
    END IF;

    IF to_regclass(current_schema() || '.rule_idw_head') IS NOT NULL THEN
        EXECUTE 'SELECT count(*) FROM rule_idw_head' INTO stray;
        IF stray > 0 THEN
            RAISE EXCEPTION
                'rule_idw_head holds % row(s) in %. A single-master grid cannot '
                'carry heads from idw_head_master. Decide what happens to them '
                'before running this migration.', stray, current_schema();
        END IF;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2.  `rule`, exactly as 028 created it and 029 tightened it.
--
-- IF NOT EXISTS so this is a no-op on a schema that somehow still has it. The
-- comments 028 carried are not repeated here; that file is still the one to
-- read for what a row means.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS rule (
    id           bigserial PRIMARY KEY,

    head_id      bigint NOT NULL
                 REFERENCES rera_head_master(id) ON DELETE CASCADE,

    account_type text NOT NULL,

    -- CR or DR. 029 dropped BOTH; this is that constraint, not 028's.
    direction    text NOT NULL,

    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT rule_direction_check CHECK (direction IN ('CR', 'DR')),
    CONSTRAINT rule_account_type_upper
        CHECK (account_type = upper(btrim(account_type))),
    CONSTRAINT rule_account_type_filled CHECK (btrim(account_type) <> ''),
    CONSTRAINT rule_head_type_unique UNIQUE (head_id, account_type)
);

CREATE INDEX IF NOT EXISTS rule_account_type_idx ON rule (account_type);

-- ---------------------------------------------------------------------------
-- 3.  Carry rule_rera_head back.
--
-- Empty in every schema as of this migration, but written anyway: this file
-- has to be correct for a schema where 031 landed cleanly and was never
-- re-run, which is the state it was designed to produce.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass(current_schema() || '.rule_rera_head') IS NOT NULL THEN
        INSERT INTO rule (head_id, account_type, direction, created_at, updated_at)
        SELECT head_id, account_type, direction, created_at, updated_at
          FROM rule_rera_head
        ON CONFLICT (head_id, account_type) DO NOTHING;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4.  Drop the split.
--
-- The view first, then the tables it reads -- CASCADE would do it either way,
-- but naming the order says the view is a reader and not something with data
-- of its own.
-- ---------------------------------------------------------------------------

DROP VIEW  IF EXISTS rule_v;
DROP TABLE IF EXISTS rule_head;
DROP TABLE IF EXISTS rule_rera_head;
DROP TABLE IF EXISTS rule_idw_head;

-- ---------------------------------------------------------------------------
-- 5.  rule_condition_head goes back to a real foreign key.
--
-- 031 dropped the FK and added master_kind so a condition could name a head
-- from any of the three masters. With one master there is one place a head can
-- come from, and the database can enforce it again -- which is better than
-- Python enforcing it, because CASCADE then keeps the table honest when a head
-- is deleted in Master Data.
--
-- Orphans are cleared first. There are none (the table is empty everywhere),
-- but ADD CONSTRAINT validates existing rows and a migration that can fail on
-- data it did not check is a migration that fails at the worst moment.
-- ---------------------------------------------------------------------------

ALTER TABLE rule_condition_head DROP COLUMN IF EXISTS master_kind;

DELETE FROM rule_condition_head ch
 WHERE NOT EXISTS (SELECT 1 FROM rera_head_master h WHERE h.id = ch.head_id);

ALTER TABLE rule_condition_head
    DROP CONSTRAINT IF EXISTS rule_condition_head_head_id_fkey;

ALTER TABLE rule_condition_head
    ADD CONSTRAINT rule_condition_head_head_id_fkey
    FOREIGN KEY (head_id) REFERENCES rera_head_master(id) ON DELETE CASCADE;

-- =============================================================================
-- 6.  Re-seed the three rows 028 seeded.
--
-- Verbatim from 028, including the reason the join word is matched as (2|to)
-- rather than rewritten: a backreference in the replacement is read as U+0001
-- here, which silently folds 'Master 2 RERA' to something that matches nothing.
--
-- ON CONFLICT DO NOTHING throughout, so this restores a grid that was emptied
-- and leaves alone one that was not. A company whose rera_head_master lacks a
-- head gets no row for it, exactly as on day one.
-- =============================================================================

INSERT INTO rule (head_id, account_type, direction)
SELECT h.id, 'RERA', 'CR'
  FROM rera_head_master h
 WHERE regexp_replace(lower(h.name), '[^a-z0-9]+', ' ', 'g')
       ~ '^ *master +(2|to) +rera *$'
ON CONFLICT (head_id, account_type) DO NOTHING;

INSERT INTO rule (head_id, account_type, direction)
SELECT h.id, 'RERA', 'DR'
  FROM rera_head_master h
 WHERE regexp_replace(lower(h.name), '[^a-z0-9]+', ' ', 'g')
       ~ '^ *rera +(2|to) +idw *$'
ON CONFLICT (head_id, account_type) DO NOTHING;

INSERT INTO rule (head_id, account_type, direction)
SELECT h.id, 'RERA', 'DR'
  FROM rera_head_master h
 WHERE regexp_replace(lower(h.name), '[^a-z0-9]+', ' ', 'g')
       ~ '^ *(cust|customer) +cancellation *$'
ON CONFLICT (head_id, account_type) DO NOTHING;
