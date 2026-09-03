-- =============================================================================
-- 032_rule_single_master.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Undoes 031_rule_multi_master.sql and puts the grid back on one table.
--
-- WHY 031 IS BEING REVERSED
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
--      and RERA's column written to. Nothing here is keyed to one company's
--      words, and this was.
--
--   2. It was a second design for a feature that had just been built to a
--      different one. 028 made the grid deliberately single-master and said so
--      out loud: rera_head_id is the column Check Rules writes, so a head from
--      another master could be shown and then not saved. If a second master
--      ever earns rules, the plan of record is a per-rule target column -- one
--      table, one more field, resolved from the fieldmap -- not three tables, a
--      view, and a name-keyed map.
--
-- HOW THIS ONE IS WRITTEN, AND WHY IT MATTERS
--
-- By RENAMING rule_rera_head back to `rule`, not by creating a new table and
-- copying into it. Three reasons, and the middle one is the whole lesson of the
-- incident this migration cleans up:
--
--   * Renaming cannot lose rows. A create-and-copy has an order to get wrong,
--     and getting it wrong is exactly how every company's grid was emptied: a
--     hand-written re-apply script dropped `rule` before the copy that was
--     supposed to read from it, so the copy found nothing and reported success.
--   * rule_rera_head is already structurally identical to 028's `rule` -- same
--     columns, same CHECKs, same UNIQUE, same FK to rera_head_master. Only the
--     names differ, so renaming is the honest inverse of what 031 did.
--   * A fresh CREATE TABLE cannot even run here. 031 gave rule_head the
--     constraint name `rule_head_type_unique`, which is the name 028 gave the
--     UNIQUE on `rule`. Index names are unique per schema, so creating `rule`
--     while rule_head still exists fails with a duplicate relation -- which is
--     the same collision that stopped 031 mid-run in the first place.
--
-- Everything below is guarded, so this is safe on a schema where 031 landed
-- cleanly, on one where it was re-run, and on one that somehow never got it.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1.  Refuse rather than discard.
--
-- rule.head_id references rera_head_master, so a row pointing at head_master or
-- idw_head_master has no home in a single-master grid. Every schema was checked
-- before this was written and both tables were empty everywhere, which is why a
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
-- 2.  The view goes first -- it reads all three tables and would block the
--     renames below. It owns no data of its own.
-- ---------------------------------------------------------------------------

DROP VIEW IF EXISTS rule_v;

-- ---------------------------------------------------------------------------
-- 3.  The other two masters' tables go next, BEFORE the rename below.
--
-- Order is load-bearing. 031 named rule_head's unique constraint
-- `rule_head_type_unique`, which is the name 028 gave the one on `rule` -- so
-- renaming rule_rera_head's constraint to it while rule_head still exists
-- fails with a duplicate relation, index names being unique per schema. The
-- guard above has already established both tables are empty, so there is
-- nothing here to weigh against getting the order right.
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS rule_head;
DROP TABLE IF EXISTS rule_idw_head;

-- ---------------------------------------------------------------------------
-- 4.  rule_rera_head becomes `rule` again, rows and all.
--
-- Every name 031 coined is put back to the one 028 and 029 used, so a schema
-- that has been through 031 and 032 is indistinguishable from one that never
-- left. RENAME CONSTRAINT also renames the index behind a UNIQUE or PRIMARY
-- KEY, so the two index names come along without being touched separately; the
-- plain index and the sequence do have to be named.
--
-- Guarded on `rule` not already existing, so this is a no-op on a schema where
-- 031 never ran.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass(current_schema() || '.rule_rera_head') IS NOT NULL
       AND to_regclass(current_schema() || '.rule') IS NULL THEN

        ALTER TABLE rule_rera_head RENAME TO rule;

        ALTER TABLE rule RENAME CONSTRAINT rule_rera_head_pkey
                                        TO rule_pkey;
        ALTER TABLE rule RENAME CONSTRAINT rule_rera_head_type_unique
                                        TO rule_head_type_unique;
        ALTER TABLE rule RENAME CONSTRAINT rule_rera_head_direction_check
                                        TO rule_direction_check;
        ALTER TABLE rule RENAME CONSTRAINT rule_rera_head_account_type_upper
                                        TO rule_account_type_upper;
        ALTER TABLE rule RENAME CONSTRAINT rule_rera_head_account_type_filled
                                        TO rule_account_type_filled;
        ALTER TABLE rule RENAME CONSTRAINT rule_rera_head_head_id_fkey
                                        TO rule_head_id_fkey;

        ALTER SEQUENCE rule_rera_head_id_seq RENAME TO rule_id_seq;

        IF to_regclass(current_schema() || '.rule_rera_head_account_type_idx')
           IS NOT NULL THEN
            ALTER INDEX rule_rera_head_account_type_idx
                RENAME TO rule_account_type_idx;
        END IF;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 5.  Create `rule` from scratch only if there was nothing to rename -- a
--     schema provisioned in some order this file cannot see. Same shape 028
--     created and 029 tightened.
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
-- 6.  rule_condition_head goes back to a real foreign key.
--
-- 031 dropped the FK and added master_kind so a condition could name a head
-- from any of the three masters. With one master there is one place a head can
-- come from, and the database can enforce it again -- which beats Python
-- enforcing it, because CASCADE then keeps the table honest when a head is
-- deleted in Master Data.
--
-- Orphans are cleared first. There are none, but ADD CONSTRAINT validates the
-- rows already there, and a migration that can fail on data it never checked is
-- one that fails at the worst possible moment.
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
-- 7.  Re-seed the three rows 028 seeded.
--
-- Verbatim from 028, including the reason the join word is matched as (2|to)
-- rather than rewritten: a backreference in the replacement is read as U+0001
-- here, which silently folds 'Master 2 RERA' to something that matches nothing.
--
-- ON CONFLICT DO NOTHING throughout, so this restores a grid that was emptied
-- and leaves alone one that was not. A company whose rera_head_master lacks a
-- head gets no row for it, exactly as on day one.
--
-- Cells entered by hand since 028 are NOT recoverable from here -- they were
-- data, not schema, and no migration can know them. They are restored per
-- company from a record of what those companies held before the wipe.
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
