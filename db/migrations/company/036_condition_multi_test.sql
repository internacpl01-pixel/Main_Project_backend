-- =============================================================================
-- 036_condition_multi_test.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Lets one condition carry more than one test, combined with AND and OR --
-- "a debit whose DESC contains 'CHRGS' or contains 'GST'" -- instead of the
-- exactly-one test 030 gave it.
--
-- 030 put subject_field/operator/value1/value2 directly on rule_condition,
-- which is right for one test and has nowhere to put a second. Those four
-- columns move to a new child table, rule_condition_test, the same shape
-- rule_condition_head already uses for a condition's other one-to-many side:
-- one row per test, sort_order for its place in the sentence, and a
-- `combinator` saying how it joins the test before it.
--
-- combinator is NULL exactly on the first test (sort_order = 0) and 'AND' or
-- 'OR' on every one after -- there is nothing before the first test to join it
-- to, and nothing after the last one may be left unjoined. Evaluated with AND
-- binding tighter than OR, the ordinary reading of a chain with no brackets:
-- split the tests into runs at each OR, AND the tests within a run, OR the
-- runs together. That is exactly what the user asked for when shown the
-- alternative (bracketed groups picked from a dropdown) and answered with a
-- flat example instead: "DESC contains X AND/OR Y AND/OR Z".
--
-- Nothing about how a condition is chosen changes: still first match wins,
-- still one sentence per row, still read before the grid.
-- =============================================================================

CREATE TABLE rule_condition_test (
    id            bigserial PRIMARY KEY,
    condition_id  bigint NOT NULL REFERENCES rule_condition(id) ON DELETE CASCADE,

    sort_order    int NOT NULL DEFAULT 0,
    -- NULL only for the first test in its condition. Checked below rather than
    -- left to the API alone, because a gap here is a test the sentence-builder
    -- and the check would silently disagree about how to join.
    combinator    text CHECK (combinator IN ('AND', 'OR')),

    subject_field text NOT NULL,
    operator      text NOT NULL,
    value1        text,
    value2        text,

    CONSTRAINT rule_condition_test_combinator_matches_order
        CHECK ((sort_order = 0) = (combinator IS NULL)),
    CONSTRAINT rule_condition_test_subject_filled CHECK (btrim(subject_field) <> ''),
    CONSTRAINT rule_condition_test_operator_filled CHECK (btrim(operator) <> '')
);

CREATE UNIQUE INDEX rule_condition_test_order_unique
    ON rule_condition_test (condition_id, sort_order);

-- The read every check makes: one condition's tests, in the order the
-- sentence -- and the AND/OR grouping -- read them.
CREATE INDEX rule_condition_test_lookup_idx
    ON rule_condition_test (condition_id, sort_order);

-- Backfill: every condition written under 030 has exactly one test, sitting
-- first and joined to nothing.
INSERT INTO rule_condition_test
    (condition_id, sort_order, combinator, subject_field, operator, value1, value2)
SELECT id, 0, NULL, subject_field, operator, value1, value2
  FROM rule_condition;

ALTER TABLE rule_condition DROP CONSTRAINT rule_condition_subject_filled;
ALTER TABLE rule_condition DROP CONSTRAINT rule_condition_operator_filled;
ALTER TABLE rule_condition DROP COLUMN subject_field;
ALTER TABLE rule_condition DROP COLUMN operator;
ALTER TABLE rule_condition DROP COLUMN value1;
ALTER TABLE rule_condition DROP COLUMN value2;
