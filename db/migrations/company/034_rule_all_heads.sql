-- =============================================================================
-- 034_rule_all_heads.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Let a rule point at any of the three head masters, not only the RERA one.
--
-- A staged row carries four classifications, and three of them are heads:
--
--     head_id      -> head_master        (Internal Head, ~97 rows)
--     rera_head_id -> rera_head_master   (RERA Head,      ~22 rows)
--     idw_head_id  -> idw_head_master    (TCP Head,       ~17 rows)
--
-- 028 wrote the rule against rera_head_master alone, because rera_head_id was
-- the one column Check Rules wrote and a head offered from another master could
-- have been shown and then not saved. That was true and is no longer wanted:
-- the user asked for the whole grid, "all heads here not only rera head".
--
-- HOW THIS IS NOT 031. The one that had to be reversed (see
-- 032_rule_single_master.sql) split `rule` into one table per master and chose
-- between them with a dict keyed on the words MASTER / RERA / IDW / TCP / FREE
-- -- account TYPE names, which are each company's own rows in
-- account_type_master. Every type nobody thought to list silently fell back to
-- the RERA master. This migration keys on nothing of the sort. It stays ONE
-- table, and the thing it adds is a per-rule TARGET naming a master TABLE --
-- part of the schema every company shares, not data any company enters. Adding
-- an account type still adds a grid column with no migration, exactly as before.
--
-- SHAPE. Three nullable columns, one FK each, and exactly one filled:
--
--     CHECK (num_nonnulls(head_id, rera_head_id, idw_head_id) = 1)
--
-- rather than one head_id plus a target string. A single polymorphic id column
-- cannot carry a foreign key, so deleting a head in Master Data would leave a
-- rule pointing at nothing -- and since every read JOINs the master, that rule
-- would not error, it would silently vanish from the grid. Losing a cell
-- quietly is the exact failure 031 is remembered for. With three real FKs the
-- database removes those rows itself, on the CASCADE 028 already chose.
--
-- `target` is then a STORED GENERATED column derived from which one is filled,
-- so it is queryable and indexable and yet cannot disagree with the data. There
-- is no writable copy of it to drift.
--
-- The columns are named for the temp_trans columns they decide -- head_id,
-- rera_head_id, idw_head_id -- so the same three names mean the same three
-- things on the row, on the rule and in _EDITABLE_PICKERS. That does mean
-- `rule.head_id` changes meaning: it used to hold a rera_head_master id and now
-- holds a head_master one. It is renamed first, in this file, so no row is ever
-- read under the wrong name and no later migration can be written against the
-- old sense of it.
--
-- Nothing is seeded and nothing is deleted. Every existing rule and condition
-- is a RERA-head one, keeps pointing at exactly the head it pointed at, and
-- reads back as target = 'rera_head'.
-- =============================================================================

-- -----------------------------------------------------------------------------
-- rule -- the grid.
-- -----------------------------------------------------------------------------

-- Rename before adding, so the new head_id cannot be confused with the old one
-- for even one statement. The two dependent constraint names travel with it;
-- Postgres does not rename them itself, and a constraint called
-- rule_head_id_fkey that actually guards rera_head_id is a trap for whoever
-- reads pg_constraint next.
ALTER TABLE rule RENAME COLUMN head_id TO rera_head_id;
ALTER TABLE rule RENAME CONSTRAINT rule_head_id_fkey     TO rule_rera_head_id_fkey;
ALTER TABLE rule RENAME CONSTRAINT rule_head_type_unique TO rule_rera_head_type_unique;

-- NOT NULL has to go: a rule about an Internal Head leaves this one empty.
-- The exactly-one CHECK below is what keeps "a rule with no head" impossible,
-- and it says it about all three columns at once, which NOT NULL cannot.
ALTER TABLE rule ALTER COLUMN rera_head_id DROP NOT NULL;

ALTER TABLE rule
    ADD COLUMN head_id     bigint REFERENCES head_master(id)     ON DELETE CASCADE,
    ADD COLUMN idw_head_id bigint REFERENCES idw_head_master(id) ON DELETE CASCADE;

ALTER TABLE rule ADD COLUMN target text
    GENERATED ALWAYS AS (
        CASE WHEN head_id      IS NOT NULL THEN 'head'
             WHEN rera_head_id IS NOT NULL THEN 'rera_head'
             WHEN idw_head_id  IS NOT NULL THEN 'idw_head' END) STORED;

ALTER TABLE rule ADD CONSTRAINT rule_one_head
    CHECK (num_nonnulls(head_id, rera_head_id, idw_head_id) = 1);

-- One answer per head per account type, per master. Three indexes rather than
-- one on (target, ...) because the null in the other two columns is what makes
-- them independent: a unique index ignores rows whose indexed column is NULL,
-- so each of these constrains only the rules that name its own master.
-- rule_rera_head_type_unique, renamed above, is the third.
CREATE UNIQUE INDEX rule_head_type_unique     ON rule (head_id,     account_type);
CREATE UNIQUE INDEX rule_idw_head_type_unique ON rule (idw_head_id, account_type);

-- The read every check makes, now that a check names one master: that master's
-- rules for one account type. Replaces rule_account_type_idx, which is its
-- prefix and so is left in place for nothing.
CREATE INDEX rule_target_type_idx ON rule (target, account_type);
DROP INDEX IF EXISTS rule_account_type_idx;

-- -----------------------------------------------------------------------------
-- rule_condition -- the exception to the grid.
-- -----------------------------------------------------------------------------

-- A real column here, not a generated one: a condition names its master before
-- it names a head, and its heads are stored in another table. The DEFAULT is
-- how the existing rows are backfilled -- every condition written so far is a
-- RERA-head one -- and it is dropped immediately afterwards so that an INSERT
-- which forgets to say which master it means fails loudly instead of quietly
-- becoming a RERA rule. A silent fallback to the RERA master is the specific
-- mistake 031 made.
ALTER TABLE rule_condition ADD COLUMN target text NOT NULL DEFAULT 'rera_head';
ALTER TABLE rule_condition ALTER COLUMN target DROP DEFAULT;

ALTER TABLE rule_condition ADD CONSTRAINT rule_condition_target_check
    CHECK (target IN ('head', 'rera_head', 'idw_head'));

-- Not useful on its own -- id is already the primary key. It exists so
-- rule_condition_head below can carry a composite foreign key against
-- (id, target), which is what makes "a condition's heads all come from the
-- master that condition names" a fact the database keeps rather than a rule the
-- API is trusted to remember.
ALTER TABLE rule_condition ADD CONSTRAINT rule_condition_id_target_key
    UNIQUE (id, target);

CREATE INDEX rule_condition_target_lookup_idx
    ON rule_condition (target, account_type, direction, sort_order, id);
DROP INDEX IF EXISTS rule_condition_lookup_idx;

-- -----------------------------------------------------------------------------
-- rule_condition_head -- the answer when a condition matches.
-- -----------------------------------------------------------------------------

-- The primary key was (condition_id, head_id), and head_id is about to become
-- nullable, which a primary key forbids. Replaced by a surrogate id plus three
-- unique indexes -- one per master, each ignoring the rows that left its column
-- null -- which restores exactly what the old key enforced: a condition cannot
-- name the same head twice.
ALTER TABLE rule_condition_head DROP CONSTRAINT rule_condition_head_pkey;

ALTER TABLE rule_condition_head RENAME COLUMN head_id TO rera_head_id;
ALTER TABLE rule_condition_head
    RENAME CONSTRAINT rule_condition_head_head_id_fkey
                   TO rule_condition_head_rera_head_id_fkey;
ALTER TABLE rule_condition_head ALTER COLUMN rera_head_id DROP NOT NULL;

ALTER TABLE rule_condition_head
    ADD COLUMN id          bigserial PRIMARY KEY,
    ADD COLUMN head_id     bigint REFERENCES head_master(id)     ON DELETE CASCADE,
    ADD COLUMN idw_head_id bigint REFERENCES idw_head_master(id) ON DELETE CASCADE;

ALTER TABLE rule_condition_head ADD COLUMN target text
    GENERATED ALWAYS AS (
        CASE WHEN head_id      IS NOT NULL THEN 'head'
             WHEN rera_head_id IS NOT NULL THEN 'rera_head'
             WHEN idw_head_id  IS NOT NULL THEN 'idw_head' END) STORED;

ALTER TABLE rule_condition_head ADD CONSTRAINT rule_condition_head_one_head
    CHECK (num_nonnulls(head_id, rera_head_id, idw_head_id) = 1);

CREATE UNIQUE INDEX rule_condition_head_head_unique
    ON rule_condition_head (condition_id, head_id);
CREATE UNIQUE INDEX rule_condition_head_rera_head_unique
    ON rule_condition_head (condition_id, rera_head_id);
CREATE UNIQUE INDEX rule_condition_head_idw_head_unique
    ON rule_condition_head (condition_id, idw_head_id);

-- The guarantee this whole shape was chosen for: a head row cannot belong to a
-- condition that names a different master. Postgres accepts a STORED GENERATED
-- column as the referencing side of a foreign key, so `target` here is derived
-- from which column was filled and then checked against the condition's own
-- target -- meaning a bug in the API that wrote an Internal Head under a RERA
-- condition is refused by the database rather than discovered later, on the
-- grid, as a head that will not save.
--
-- It also carries the ON DELETE CASCADE that used to be on the plain
-- condition_id key, which is dropped as redundant: this one covers it.
ALTER TABLE rule_condition_head
    DROP CONSTRAINT rule_condition_head_condition_id_fkey;
ALTER TABLE rule_condition_head ADD CONSTRAINT rule_condition_head_target_fkey
    FOREIGN KEY (condition_id, target) REFERENCES rule_condition (id, target)
    ON DELETE CASCADE;
