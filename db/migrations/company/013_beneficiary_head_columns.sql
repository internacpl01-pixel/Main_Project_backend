-- =============================================================================
-- 013_beneficiary_head_columns.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- Give a beneficiary three heads from EACH of the three head tables, instead
-- of three unlabelled ones from nowhere.
--
-- 009 created head1/2/3 as deliberately free text -- notes about how a payee is
-- usually booked, with the reasoning that constraining them would turn every
-- typo into a constraint violation. The dropdowns added alongside this file
-- remove that objection: a value can no longer be typed at all, only chosen
-- from the company's own master table. What stays true from 009 is that these
-- are DEFAULTS for classification, not classification itself -- the ledger's
-- head_id / rera_head_id / idw_head_id columns on transactions are unaffected.
--
-- Column naming follows the app's existing split between table names and
-- labels: the table is idw_head_master and the ledger column is idw_head_id,
-- while the UI has said "TCP Head" since that rename. So the columns here are
-- idw_head1..3 and the labels are "TCP Head 1..3" -- same pair of names, same
-- reason as routers/master.py's note on 'idw_head'.
--
-- head1/2/3 are KEPT and re-used as the Internal Head columns rather than
-- dropped and re-added: they match head_master the way idw_head1..3 match
-- idw_head_master, and every company on this install has zero beneficiary rows
-- carrying a head value (checked before writing this: 2 beneficiary rows exist
-- in total, across all 7 schemas, and none has head1, head2 or head3 set), so
-- nothing is being reinterpreted underneath live data.
--
-- Values are stored as the head's NAME, not its id. These are advisory
-- defaults, so a dangling reference is not a correctness problem the way it
-- would be in the ledger, and storing text keeps the generic master router
-- reading and writing one table with no join -- the list view shows the head
-- rather than an integer. The cost is that renaming a head in its master table
-- does not follow through to beneficiaries already pointing at the old name.
-- =============================================================================

ALTER TABLE beneficiary_master
    ADD COLUMN IF NOT EXISTS rera_head1 text,
    ADD COLUMN IF NOT EXISTS rera_head2 text,
    ADD COLUMN IF NOT EXISTS rera_head3 text,
    ADD COLUMN IF NOT EXISTS idw_head1  text,
    ADD COLUMN IF NOT EXISTS idw_head2  text,
    ADD COLUMN IF NOT EXISTS idw_head3  text;

-- The three heads within one group must differ: picking "Land Payment" as both
-- RERA Head 1 and RERA Head 2 says nothing that picking it once does not.
--
-- Dropped first because Postgres has no ADD CONSTRAINT IF NOT EXISTS, and this
-- file has to stay re-runnable like every other one in this directory.
--
-- Each pair reads (a IS NULL OR a IS DISTINCT FROM b): two NULLs pass, one NULL
-- passes, two equal non-NULLs fail. A plain a <> b would evaluate to NULL when
-- either side is NULL, and a CHECK that evaluates to NULL passes -- so the
-- constraint would hold only for rows where all three were already filled in.
--
-- Groups are constrained separately on purpose. The same name appearing in two
-- DIFFERENT tables is normal -- 'Internal', 'Reversed' and 'Credit- no effect'
-- are each present in more than one head master -- so cross-group comparison
-- would reject a legitimate combination.
ALTER TABLE beneficiary_master
    DROP CONSTRAINT IF EXISTS beneficiary_rera_heads_distinct,
    DROP CONSTRAINT IF EXISTS beneficiary_idw_heads_distinct,
    DROP CONSTRAINT IF EXISTS beneficiary_heads_distinct;

ALTER TABLE beneficiary_master
    ADD CONSTRAINT beneficiary_rera_heads_distinct CHECK (
        (rera_head1 IS NULL OR rera_head1 IS DISTINCT FROM rera_head2)
    AND (rera_head1 IS NULL OR rera_head1 IS DISTINCT FROM rera_head3)
    AND (rera_head2 IS NULL OR rera_head2 IS DISTINCT FROM rera_head3)
    ),
    ADD CONSTRAINT beneficiary_idw_heads_distinct CHECK (
        (idw_head1 IS NULL OR idw_head1 IS DISTINCT FROM idw_head2)
    AND (idw_head1 IS NULL OR idw_head1 IS DISTINCT FROM idw_head3)
    AND (idw_head2 IS NULL OR idw_head2 IS DISTINCT FROM idw_head3)
    ),
    ADD CONSTRAINT beneficiary_heads_distinct CHECK (
        (head1 IS NULL OR head1 IS DISTINCT FROM head2)
    AND (head1 IS NULL OR head1 IS DISTINCT FROM head3)
    AND (head2 IS NULL OR head2 IS DISTINCT FROM head3)
    );
