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


async def record_drive_folder_history(schema: str, setting: str, folder_id: str,
                                      folder_name: str | None, changed_by: str) -> None:
    """Log one successful save of the export or import folder.

    Purely a trail for the Settings screen's "previously used" links -- see
    055_drive_folder_history.sql. Called after the save itself succeeds, so a
    folder that failed verification never gets a row.
    """
    async with company_connection(schema) as conn:
        await conn.execute(
            "INSERT INTO drive_folder_history "
            "(setting, folder_id, folder_name, changed_by) VALUES ($1, $2, $3, $4)",
            setting, folder_id, folder_name, changed_by,
        )


async def get_drive_folder_history(schema: str, setting: str, *,
                                   exclude_folder_id: str | None = None,
                                   limit: int = 3) -> list[dict]:
    """Up to `limit` distinct folders this setting previously held, newest
    change first -- excluding whichever folder is current, so the list reads
    as "previously used" rather than repeating the link already shown above
    it.

    Distinct on folder_id: saving the same folder twice (e.g. clearing the
    import folder and pasting the export folder's own link back) should not
    push it to the top of its own history as though it were a different one.
    """
    async with company_connection(schema) as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (folder_id) folder_id, folder_name, changed_at
              FROM drive_folder_history
             WHERE setting = $1
               AND ($2::text IS NULL OR folder_id <> $2)
             ORDER BY folder_id, changed_at DESC
            """,
            setting, exclude_folder_id,
        )
    ordered = sorted(rows, key=lambda r: r["changed_at"], reverse=True)[:limit]
    return [
        {"folder_id": r["folder_id"], "folder_name": r["folder_name"],
         "changed_at": r["changed_at"].isoformat()}
        for r in ordered
    ]


