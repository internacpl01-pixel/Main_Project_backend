-- =============================================================================
-- 055_drive_folder_history.sql
-- Applied to every company_NNN schema by:  python -m db.migrate upgrade
--
-- A running log of every Drive folder ever saved into drive_settings (046),
-- one row per successful save. Purely informational -- unlike 048's
-- drive_folders (dropped by 049), nothing here is a usable alternate import
-- source; it only backs a "previously used" list of clickable links on the
-- Settings screen, next to the current folder.
--
-- Two settings share this one table (`setting` says which): the export
-- folder and the import folder each get their own history, since they are
-- independent (see services/settings.py's own note on why there are two).
-- =============================================================================

CREATE TABLE IF NOT EXISTS drive_folder_history (
    id          bigserial   PRIMARY KEY,
    setting     text        NOT NULL CHECK (setting IN ('export', 'import')),
    folder_id   text        NOT NULL,
    folder_name text,
    changed_by  text        NOT NULL,
    changed_at  timestamptz NOT NULL DEFAULT now()
);

-- The read every "previously used" list makes: one setting's history, newest
-- first.
CREATE INDEX IF NOT EXISTS drive_folder_history_lookup_idx
    ON drive_folder_history (setting, changed_at DESC);
