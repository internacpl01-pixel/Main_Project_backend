-- =============================================================================
-- 052_narration_rule.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- An exception to the auto-generated NARRATION text, the same shape as
-- rule_condition (030) but with a different THEN.
--
-- build_narration's Internal Transfer branch composes a parenthetical from the
-- statement itself -- "(From YES IDW 0490 to x1234)" -- by pulling the last 4
-- digits out of the description. That guess is sometimes wrong or simply not
-- how the company wants the leg named. A narration rule overrides ONLY that
-- parenthetical, and only when it matches: it names an account type and a
-- direction (same two words the grid and rule_condition already use), one or
-- more tests on a column of the statement, and the exact From/To text to use
-- instead when every test passes.
--
-- Unlike rule_condition this has no head to point at and so no third table:
-- the answer is two plain strings, not a master lookup. Direction is stored
-- explicitly rather than derived from credit/debit and auto-swapped, so an
-- admin who wants both directions covered writes two rules -- one CR, one DR
-- -- each with From/To already in the order they want printed, exactly the
-- same way two directions of rule_condition are two separate rows today.
--
-- Nothing is seeded: nobody has written one of these yet, and inventing one
-- here would change generated narration the day this migration lands.
-- =============================================================================

CREATE TABLE IF NOT EXISTS narration_rule (
    id            bigserial PRIMARY KEY,

    -- Same two words rule_condition is keyed on: the account type upper-cased
    -- to match bank_master.account_type, and CR or DR.
    account_type  text NOT NULL,
    direction     text NOT NULL,

    -- The exact text for each side of "(From {from_label} to {to_label})".
    -- Free text, not derived from anything -- the whole point is an admin can
    -- name the leg however the company actually refers to it.
    from_label    text NOT NULL,
    to_label      text NOT NULL,

    -- First match wins, same tie-break as rule_condition, scoped the same way:
    -- per (account_type, direction), the only grouping two of these compete in.
    sort_order    int NOT NULL DEFAULT 0,

    -- Switched off rather than deleted, so a rule can be taken out of
    -- generation and put back without retyping it.
    is_active     boolean NOT NULL DEFAULT true,

    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT narration_rule_direction_check CHECK (direction IN ('CR', 'DR')),
    CONSTRAINT narration_rule_type_upper
        CHECK (account_type = upper(btrim(account_type))),
    CONSTRAINT narration_rule_type_filled CHECK (btrim(account_type) <> ''),
    CONSTRAINT narration_rule_from_filled CHECK (btrim(from_label) <> ''),
    CONSTRAINT narration_rule_to_filled CHECK (btrim(to_label) <> '')
);

-- The read every narration generation makes: one type/direction's rules, in
-- the order they decide.
CREATE INDEX IF NOT EXISTS narration_rule_lookup_idx
    ON narration_rule (account_type, direction, sort_order, id);

DROP TRIGGER IF EXISTS narration_rule_set_updated_at ON narration_rule;
CREATE TRIGGER narration_rule_set_updated_at
    BEFORE UPDATE ON narration_rule
    FOR EACH ROW EXECUTE FUNCTION admin.set_updated_at();

-- One or more tests, identical shape to rule_condition_test -- same columns,
-- same meaning, same engine (services/rules.py's OPERATORS and match()).
CREATE TABLE IF NOT EXISTS narration_rule_test (
    id            bigserial PRIMARY KEY,
    rule_id       bigint NOT NULL
                  REFERENCES narration_rule(id) ON DELETE CASCADE,
    sort_order    int NOT NULL DEFAULT 0,
    combinator    text,
    subject_field text NOT NULL,
    operator      text NOT NULL,
    value1        text,
    value2        text,

    CONSTRAINT narration_rule_test_combinator_check
        CHECK (combinator IS NULL OR combinator IN ('AND', 'OR')),
    CONSTRAINT narration_rule_test_subject_filled CHECK (btrim(subject_field) <> ''),
    CONSTRAINT narration_rule_test_operator_filled CHECK (btrim(operator) <> '')
);

CREATE INDEX IF NOT EXISTS narration_rule_test_lookup_idx
    ON narration_rule_test (rule_id, sort_order);
