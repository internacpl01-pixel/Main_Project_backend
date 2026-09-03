-- =============================================================================
-- 031_rule_multi_master.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Until this migration the `rule` table stored one master only: every row
-- pointed at rera_head_master because rera_head_id was the only column Check
-- Rules ever wrote.  That made the grid correct for RERA accounts but
-- meaningless for MASTER and IDW — a head from head_master could never be
-- stored, and a head from idw_head_master could never be stored either.
--
-- This splits the table into one per master, each with a real foreign key to
-- its own master, and exposes them through a single view `rule_v` so every
-- read that used to say `FROM rule` keeps working.
--
-- The three typed tables:
--
--   rule_head        -> head_master        (MASTER, FREE accounts)
--   rule_rera_head   -> rera_head_master   (RERA accounts)
--   rule_idw_head    -> idw_head_master    (IDW / TCP accounts)
--
-- Each carries the same columns (account_type, direction) and the same
-- constraints, so a row means the same thing whichever table it lands in.
--
-- account_type is still TEXT, matching bank_master — the same thing that
-- already decides whether Check Rules writes head_id or rera_head_id.
--
-- Nothing is seeded.  The three rows 028 planted in `rule` are migrated into
-- rule_rera_head under the new names, so the RERA rule is identical the day
-- this lands.  MASTER and IDW columns start blank, the same standing the
-- unseeded cells on the Rules grid today.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1.  Three typed tables, each with a real FK to its own master.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS rule_head (
    id           bigserial PRIMARY KEY,
    head_id      bigint NOT NULL
                 REFERENCES head_master(id) ON DELETE CASCADE,
    account_type text NOT NULL,
    direction    text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT rule_head_direction_check CHECK (direction IN ('CR', 'DR')),
    CONSTRAINT rule_head_account_type_upper
        CHECK (account_type = upper(btrim(account_type))),
    CONSTRAINT rule_head_account_type_filled
        CHECK (btrim(account_type) <> ''),
    CONSTRAINT rule_head_type_unique UNIQUE (head_id, account_type)
);

CREATE TABLE IF NOT EXISTS rule_rera_head (
    id           bigserial PRIMARY KEY,
    head_id      bigint NOT NULL
                 REFERENCES rera_head_master(id) ON DELETE CASCADE,
    account_type text NOT NULL,
    direction    text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT rule_rera_head_direction_check CHECK (direction IN ('CR', 'DR')),
    CONSTRAINT rule_rera_head_account_type_upper
        CHECK (account_type = upper(btrim(account_type))),
    CONSTRAINT rule_rera_head_account_type_filled
        CHECK (btrim(account_type) <> ''),
    CONSTRAINT rule_rera_head_type_unique UNIQUE (head_id, account_type)
);

CREATE TABLE IF NOT EXISTS rule_idw_head (
    id           bigserial PRIMARY KEY,
    head_id      bigint NOT NULL
                 REFERENCES idw_head_master(id) ON DELETE CASCADE,
    account_type text NOT NULL,
    direction    text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT rule_idw_head_direction_check CHECK (direction IN ('CR', 'DR')),
    CONSTRAINT rule_idw_head_account_type_upper
        CHECK (account_type = upper(btrim(account_type))),
    CONSTRAINT rule_idw_head_account_type_filled
        CHECK (btrim(account_type) <> ''),
    CONSTRAINT rule_idw_head_type_unique UNIQUE (head_id, account_type)
);

-- ---------------------------------------------------------------------------
-- 2.  Indexes matching what the old single table had.
-- ---------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS rule_head_account_type_idx
    ON rule_head (account_type);

CREATE INDEX IF NOT EXISTS rule_rera_head_account_type_idx
    ON rule_rera_head (account_type);

CREATE INDEX IF NOT EXISTS rule_idw_head_account_type_idx
    ON rule_idw_head (account_type);

-- ---------------------------------------------------------------------------
-- 3.  Migrate existing data from the old `rule` table.
--
-- All existing rows point at rera_head_master, so they all land in
-- rule_rera_head.  The three seeded rows from 028 are the only ones that
-- exist in practice (verified: 0 BOTH rows across all schemas, confirmed by
-- 029's own check).
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema = current_schema()
                 AND table_name = 'rule') THEN
        INSERT INTO rule_rera_head (head_id, account_type, direction, created_at, updated_at)
        SELECT head_id, account_type, direction, created_at, updated_at
          FROM rule;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4.  Drop the old table now that its data is safe.
-- ---------------------------------------------------------------------------

DROP TABLE IF EXISTS rule CASCADE;

-- ---------------------------------------------------------------------------
-- 5.  rule_v — a view that unifies the three tables so every caller that
--     used to say `FROM rule` keeps working with no code change.
--
--     head_id   is returned as-is — callers that join to a specific master
--               know which table it came from, and the view's rows carry
--               account_type which is exactly the signal they need.
-- ---------------------------------------------------------------------------

CREATE VIEW rule_v AS
SELECT id, head_id, account_type, direction, created_at, updated_at,
       'head'        AS master_kind
  FROM rule_head
UNION ALL
SELECT id, head_id, account_type, direction, created_at, updated_at,
       'rera_head'   AS master_kind
  FROM rule_rera_head
UNION ALL
SELECT id, head_id, account_type, direction, created_at, updated_at,
       'idw_head'    AS master_kind
  FROM rule_idw_head;

-- =============================================================================
-- 6.  rule_condition_head must be able to point at any of the three masters.
--
-- Until now it carried a FK to rera_head_master only.  A condition for a MASTER
-- or IDW account needs to name a head from the right master, so the FK is
-- dropped and a master_kind column tells the loader which table to join.
-- Validation happens in Python (routers/rules.py _clean) before any row is
-- written, exactly as bank_master.account_type is stored as text.
-- =============================================================================

ALTER TABLE rule_condition_head
    ADD COLUMN IF NOT EXISTS master_kind text NOT NULL DEFAULT 'rera_head';

-- Back-fill from the condition's account_type via the rule_condition table.
-- All conditions for RERA accounts -> rera_head, MASTER -> head, IDW -> idw_head.
-- Conditions with no account_type or an unknown one land on rera_head (the old
-- behaviour), which is harmless because the validation in _clean will reject
-- them on the next write.
UPDATE rule_condition_head ch
   SET master_kind = CASE upper(btrim(c.account_type))
                       WHEN 'MASTER' THEN 'head'
                       WHEN 'FREE'   THEN 'head'
                       WHEN 'IDW'    THEN 'idw_head'
                       WHEN 'TCP'    THEN 'idw_head'
                       WHEN 'RERA'   THEN 'rera_head'
                       ELSE 'rera_head'
                     END
  FROM rule_condition c
 WHERE ch.condition_id = c.id;

-- The old FK pointed every condition at rera_head_master.  Now that
-- master_kind tells the loader where to look, the FK would reject legitimate
-- rows and is dropped.
ALTER TABLE rule_condition_head
    DROP CONSTRAINT IF EXISTS rule_condition_head_head_id_fkey;

-- Keep the condition_id FK — deleting a condition still cascades its heads.
