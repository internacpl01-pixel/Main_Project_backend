-- Single-row table holding the Drive folder ID the Gmail Apps Script drops
-- statements into (see routers/imports.py's /imports/from-drive) so it can
-- be changed from the app's UI instead of editing config.DRIVE_FOLDER_ID /
-- Render's env var by hand. folder_id starts NULL -- until someone saves one
-- through the UI, services/settings.py falls back to config.DRIVE_FOLDER_ID,
-- so an existing working setup keeps working unchanged after this migration.
CREATE TABLE IF NOT EXISTS drive_settings (
    id         smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    folder_id  text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO drive_settings (id, folder_id)
VALUES (1, NULL)
ON CONFLICT (id) DO NOTHING;
