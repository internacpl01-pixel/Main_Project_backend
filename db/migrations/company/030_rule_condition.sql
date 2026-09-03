-- =============================================================================
-- 030_rule_condition.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- The exception to the grid: "when this is also true, it must be that head".
--
-- 028 made the rule a grid -- one cell per head per account type, holding CR,
-- DR or nothing -- and that grid answers the general question well: what may a
-- credit on a RERA account be, what may a debit be. What it cannot say is the
-- thing every accounts department actually knows:
--
--     a RERA debit whose narration mentions REFUND is a Cust Cancellation,
--     not just "one of the two heads debits are allowed to be"
--
-- One row here is that sentence. It names the account type and the direction
-- the grid already speaks in, plus ONE further test on ONE column of the
-- statement, and the head or heads that are the answer when the test passes.
--
-- How the two fit together, decided with the user on 2026-09-03:
--
--   * Conditions are read FIRST. The first active condition matching a row --
--     same account type, same direction, and its test passing -- decides that
--     row on its own. Nothing else is consulted for it.
--   * A row no condition matches falls back to the grid, exactly as before.
--   * A condition may name ANY active head, including one left blank on the
--     grid. That is the point of it: 'Bank Charges' can stay blank in the RERA
--     column -- never offered on an ordinary debit -- while a condition admits
--     it for the debits whose narration says so. Narrowing the grid down is
--     just the same mechanism used the other way.
--
-- So the grid is what a row may be by default, and a condition is a smaller,
-- louder statement that outranks it. Both are still one source: whichever of
-- the two judged a row is also the one whose heads the Replace dropdown offers,
-- which is the property 028 was built around and this must not break.
--
-- Order matters, because two conditions can match one row. The first by
-- sort_order wins, and the Rules page moves them with up/down arrows -- so a
-- surprising verdict is always traceable to one sentence the user can read.
--
-- Nothing is seeded. Nobody has written a condition yet, and a condition
-- invented here would change what Check Rules reports the day this lands.
-- =============================================================================

CREATE TABLE IF NOT EXISTS rule_condition (
    id            bigserial PRIMARY KEY,

    -- Same two words the grid is keyed on, and the same spelling: the name
    -- upper-cased, matched against bank_master.account_type, and one of CR/DR.
    -- Deliberately NOT nullable-for-either: 029 took that idea out of the grid
    -- an hour ago, and a condition that applies to both directions would put it
    -- straight back in a second table where nobody would look for it.
    account_type  text NOT NULL,
    direction     text NOT NULL,

    -- The one further test. subject_field names a live column of temp_trans,
    -- validated on write against services/custom_fields.data_columns -- the
    -- same list the staging table draws its own headers from, so a custom field
    -- added yesterday can be tested today under the name the company gave it.
    --
    -- Stored as the column NAME rather than a fieldmap id for the same reason
    -- account_type is stored as a name: it is compared against the thing it
    -- names, and an id would need resolving on a code path that must not fail.
    -- A condition whose column has since been dropped refuses to run and says
    -- so; it is not skipped, because a skipped rule reads as a clean row.
    subject_field text NOT NULL,

    -- One of services/rules.OPERATORS. NOT a fragment of SQL: the check reads
    -- the value out of the row in Python and compares it there, so nothing a
    -- user types on the Rules page is ever parsed, interpolated or executed as
    -- SQL. value2 is used only by the two-sided operators (between).
    operator      text NOT NULL,
    value1        text,
    value2        text,

    -- First match wins, so this is the whole tie-break between two sentences
    -- that both describe a row. Scoped per (account_type, direction) on screen,
    -- which is the only grouping in which two conditions ever compete.
    sort_order    int NOT NULL DEFAULT 0,

    -- Switched off rather than deleted, so a condition can be taken out of the
    -- next check and put back without retyping it.
    is_active     boolean NOT NULL DEFAULT true,

    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT rule_condition_direction_check CHECK (direction IN ('CR', 'DR')),
    CONSTRAINT rule_condition_type_upper
        CHECK (account_type = upper(btrim(account_type))),
    CONSTRAINT rule_condition_type_filled CHECK (btrim(account_type) <> ''),
    CONSTRAINT rule_condition_subject_filled CHECK (btrim(subject_field) <> ''),
    CONSTRAINT rule_condition_operator_filled CHECK (btrim(operator) <> '')
);

-- The read every check makes: one type's conditions, in the order they decide.
CREATE INDEX IF NOT EXISTS rule_condition_lookup_idx
    ON rule_condition (account_type, direction, sort_order, id);

-- The answer, when the test passes. A list rather than one column because a
-- condition can legitimately narrow two heads to two -- "a debit mentioning
-- REFUND is a cancellation or a chargeback, never a transfer" -- and the first
-- of them is what Replace preselects, the same standing the grid's first head
-- has today.
--
-- REFERENCES rera_head_master for the same reason rule.head_id does: rera_head_id
-- is the column Check Rules writes, so a head from any other master could be
-- offered and then not saved. CASCADE, so deleting a head in Master Data does
-- not leave a condition pointing at nothing -- a condition left with no heads
-- at all is reported on the Rules page and refuses to run.
CREATE TABLE IF NOT EXISTS rule_condition_head (
    condition_id bigint NOT NULL
                 REFERENCES rule_condition(id) ON DELETE CASCADE,
    head_id      bigint NOT NULL
                 REFERENCES rera_head_master(id) ON DELETE CASCADE,
    sort_order   int NOT NULL DEFAULT 0,

    PRIMARY KEY (condition_id, head_id)
);
