-- =============================================================================
-- 010_temp_trans_lock.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- A per-row lock on staged rows. A locked row cannot be edited or deleted
-- until someone unlocks it — the Imported Rows screen shows a padlock per row
-- and the API refuses writes while it is set.
--
-- Why staging and not the ledger: the ledger already has an immutability
-- story (posted rows are guarded by temp_trans_id RESTRICT and reversal flows).
-- Staging is where rows sit while several people review them, and "I have
-- checked this line, leave it alone" needs to be sayable before posting.
--
-- A boolean, not a locked_by/locked_at pair. The lock is a flag anyone with
-- write access may set or clear — it is protection against accident, not
-- against each other. If per-user locks are ever wanted, that is a new column
-- alongside this one, not a reshape of it.
-- =============================================================================

ALTER TABLE temp_trans
    ADD COLUMN IF NOT EXISTS is_locked boolean NOT NULL DEFAULT false;
