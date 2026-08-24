-- =============================================================================
-- 004_username_case_insensitive.sql
-- Applied to the admin schema by:  python -m db.migrate upgrade
--
-- Make "the same username" mean the same thing everywhere.
--
-- services/accounts.py has always refused a name that collides case-insensitively
-- (lower(username) = lower($1)), so the app already believed 'Admin' and 'admin'
-- were one account. The database did not: users_username_key is a plain UNIQUE
-- on the column, which happily holds both. And login compared exactly, so an
-- account created as 'DPL-Admin' could not be logged into as 'dpl-admin'.
--
-- This index is what makes the app's belief true. It also backs the lower()
-- comparison login now uses -- without it that lookup cannot use an index at
-- all and degrades to a sequential scan of every user on every sign-in.
--
-- Checked before writing: no two accounts on this install differ only by case,
-- so the index builds without anything needing to be renamed first. It would
-- have failed loudly rather than picking a winner, which is correct, but is
-- worth knowing in advance.
--
-- users_username_key is deliberately LEFT IN PLACE. It is now implied by this
-- one -- anything this index rejects, that index would too -- so it costs a
-- little write time and no correctness. Dropping a constraint that predates
-- this file is a bigger change than the bug being fixed warrants.
-- =============================================================================

CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower_key
    ON admin.users (lower(username));
