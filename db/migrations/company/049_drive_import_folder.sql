-- Replaces 048's list of extra folders with a single import folder.
--
-- The right model, confirmed with the user, is two independent settings
-- rather than one folder plus a library of alternatives:
--
--   export folder  -- where the Gmail Apps Script SAVES attachments. This
--                     backend pushes every change to the script itself, so
--                     the two always agree on where collection lands.
--   import folder  -- where this software READS from. May be the same
--                     folder as the export one (the usual setup: collect
--                     and import in one place) or a different one entirely
--                     (a folder of statements that never came through
--                     Gmail). Nothing about it is sent to the script.
--
-- NULL means "the same folder as the export one", so an existing company
-- keeps behaving exactly as it did before this column existed -- there is
-- no row to backfill and no moment where imports point nowhere.
ALTER TABLE drive_settings ADD COLUMN IF NOT EXISTS import_folder_id text;

-- 048's drive_folders never held a row in any company -- the feature it
-- backed was replaced before anyone used it -- so this drops rather than
-- migrating anything out of it.
DROP TABLE IF EXISTS drive_folders;
