-- =============================================================================
-- 008_fieldchange_log.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- An audit trail for the fieldmap, ported from DPL's fieldchange_log.
--
-- Why the fieldmap specifically: it is the one table where a small edit has
-- large, silent consequences. Renaming a display name is cosmetic, but
-- narrowing `mapfields` stops a bank's column matching, and the only symptom is
-- a column that quietly imports empty from then on. When someone asks "this was
-- filling last month, what changed?", this table is the answer.
--
-- Three things are recorded that DPL's version did not:
--
--   old_value / new_value  DPL logged that a field changed, never what it
--                          changed to. That tells you where to look and nothing
--                          more — you still cannot see the alias that was
--                          dropped, which is the whole question being asked.
--
--   changed_by             DPL was single-tenant with one shared admin login.
--                          Here a company has managers and staff, so "who" is a
--                          real question. Stored as the username text rather
--                          than a users FK on purpose: the log has to stay
--                          readable after a user is deleted.
--
--   action                 created / updated / deleted, so a field's whole life
--                          is one filtered read instead of an absence of rows.
--
-- fieldmap_id is deliberately NOT a foreign key. The most valuable row in here
-- is the one recording a deletion, and an FK would either block that delete or
-- cascade the evidence away with it. It is a breadcrumb for grouping, not a
-- live reference.
--
-- One row per changed column, not one per request: a PATCH that rewrites both
-- displayname and mapfields writes two rows, so each can state its own before
-- and after without the reader parsing a blob.
-- =============================================================================

CREATE TABLE IF NOT EXISTS fieldchange_log (
    id            bigserial   PRIMARY KEY,
    fieldmap_id   integer,
    fieldname     text        NOT NULL,
    action        text        NOT NULL,
    field_changed text,
    old_value     text,
    new_value     text,
    changed_by    text        NOT NULL DEFAULT '',
    changed_at    timestamptz NOT NULL DEFAULT now()
);

-- The page reads this newest-first and nothing else, so this one index serves
-- every query it makes.
CREATE INDEX IF NOT EXISTS idx_fieldchange_log_changed_at
    ON fieldchange_log (changed_at DESC, id DESC);

-- Filtering to one field's history is the second thing anyone does after
-- reading the list.
CREATE INDEX IF NOT EXISTS idx_fieldchange_log_fieldname
    ON fieldchange_log (fieldname);
