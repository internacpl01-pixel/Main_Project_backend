"""
Small per-company settings that need to be changed from the UI rather than
by editing an env var and redeploying.

Two Drive folders live here, and the difference between them is the whole
point of having two:

  EXPORT folder  -- where the Gmail Apps Script SAVES statement attachments
                    it finds. Changing it here also rewrites the script's
                    own copy (see PUT /imports/drive-settings), because if
                    the two ever disagreed the script would keep filing
                    statements somewhere nothing reads from, silently.

  IMPORT folder  -- where this software READS statements from. Usually the
                    same folder as the export one, which is why NULL means
                    exactly that. Set it to a different folder to import a
                    batch of statements that never came through Gmail,
                    without disturbing the collection at all -- nothing
                    about this value is ever sent to the Apps Script.

See routers/imports.py's /imports/drive-settings and /imports/from-drive.
"""
from __future__ import annotations

import config
from database import company_connection


async def get_drive_folder_id(schema: str) -> str:
    """The export folder: DB value if saved, else the env var it replaces."""
    async with company_connection(schema) as conn:
        value = await conn.fetchval(
            "SELECT folder_id FROM drive_settings WHERE id = 1")
    return value or config.DRIVE_FOLDER_ID


async def set_drive_folder_id(schema: str, folder_id: str) -> None:
    async with company_connection(schema) as conn:
        await conn.execute(
            "UPDATE drive_settings SET folder_id = $1, updated_at = now() "
            "WHERE id = 1",
            folder_id,
        )


async def get_raw_import_folder_id(schema: str) -> str | None:
    """The import folder AS STORED -- None when it follows the export folder.

    Separate from get_import_folder_id below because the settings screen has
    to be able to say "same as the export folder" rather than repeating the
    export folder's id back as though someone had typed it there.
    """
    async with company_connection(schema) as conn:
        return await conn.fetchval(
            "SELECT import_folder_id FROM drive_settings WHERE id = 1")


async def get_import_folder_id(schema: str) -> str:
    """The folder every Drive import actually reads, export folder if unset."""
    return (await get_raw_import_folder_id(schema)
            or await get_drive_folder_id(schema))


async def set_import_folder_id(schema: str, folder_id: str | None) -> None:
    """None/blank puts it back to following the export folder."""
    async with company_connection(schema) as conn:
        await conn.execute(
            "UPDATE drive_settings SET import_folder_id = $1, "
            "updated_at = now() WHERE id = 1",
            folder_id or None,
        )


