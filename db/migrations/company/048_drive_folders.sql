-- Extra Drive folders a person can import from, alongside the one folder
-- drive_settings (046) holds.
--
-- drive_settings.folder_id is the Gmail Apps Script's own target: the script
-- writes statements into it, this backend reads from it, and the two are
-- kept in step by /imports/drive-settings posting every change to the
-- script. That relationship is exactly why a second folder cannot just
-- overwrite it -- pointing the setting at a one-off folder of statements
-- would silently redirect the script too, and the automatic collection
-- would start filling the wrong place.
--
-- So an ad-hoc folder lives here instead: known to this backend, never sent
-- to the script, and usable as an import source without disturbing the
-- automatic pipeline. The UNIQUE on folder_id is what makes adding the same
-- folder twice a no-op rather than a duplicate row in the source dropdown.
CREATE TABLE IF NOT EXISTS drive_folders (
    id         bigserial   PRIMARY KEY,
    folder_id  text        NOT NULL UNIQUE,
    label      text        NOT NULL,
    added_by   text        NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
