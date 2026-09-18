"""
Small per-company settings that need to be changed from the UI rather than
by editing an env var and redeploying. Currently just the Drive folder ID
(see routers/imports.py's /imports/drive-settings and /imports/from-drive).
"""
from __future__ import annotations

import config
from database import company_connection


async def get_drive_folder_id(schema: str) -> str:
    """DB value if one has ever been saved, else the env var it replaces."""
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


# --- extra folders (drive_folders, migration 048) ---------------------------
#
# Folders someone has added by pasting a Drive URL, to import a batch of
# statements that never came through Gmail. Deliberately separate from the
# single folder above: that one is shared with the Apps Script and changing
# it redirects the automatic collection, which is exactly what an ad-hoc
# import must not do.


async def list_extra_drive_folders(schema: str) -> list[dict]:
    async with company_connection(schema) as conn:
        rows = await conn.fetch(
            "SELECT id, folder_id, label, added_by, created_at "
            "FROM drive_folders ORDER BY created_at DESC")
    return [dict(r) for r in rows]


async def add_extra_drive_folder(schema: str, folder_id: str, label: str,
                                 added_by: str) -> dict:
    """Adds the folder, or returns the existing row if it is already saved.

    Re-adding the same folder is a person pasting a URL they had already
    added -- treated as "you already have this one" rather than an error,
    which is why the insert is ON CONFLICT DO NOTHING followed by a read
    rather than a plain INSERT ... RETURNING.
    """
    async with company_connection(schema) as conn:
        await conn.execute(
            "INSERT INTO drive_folders (folder_id, label, added_by) "
            "VALUES ($1, $2, $3) ON CONFLICT (folder_id) DO NOTHING",
            folder_id, label, added_by)
        row = await conn.fetchrow(
            "SELECT id, folder_id, label, added_by, created_at "
            "FROM drive_folders WHERE folder_id = $1", folder_id)
    return dict(row)


async def delete_extra_drive_folder(schema: str, row_id: int) -> bool:
    """Forgets a saved folder. Nothing in Drive itself is touched."""
    async with company_connection(schema) as conn:
        result = await conn.execute(
            "DELETE FROM drive_folders WHERE id = $1", row_id)
    return result.endswith(" 1")


class UnknownDriveFolder(Exception):
    """A folder id that is neither the configured one nor a saved extra."""


async def resolve_drive_folder(schema: str, folder_id: str | None) -> str:
    """Turn a caller-supplied folder id into one this company may read.

    Blank/None means "the configured folder", which is what every Drive
    endpoint did before extra folders existed and stays the default.

    A supplied id is checked against the configured folder and the saved
    extras rather than used as given. Without that check any authenticated
    user could pass an arbitrary folder id and have the server -- which
    holds a broad Drive grant of its own -- list, download, rename or trash
    files in any folder that account can reach, including ones belonging to
    a different company on this same deployment. Saving a folder first is
    manager+; using one is not, so this is where the two meet.
    """
    configured = await get_drive_folder_id(schema)
    if not folder_id:
        return configured
    folder_id = folder_id.strip()
    if folder_id == configured:
        return folder_id
    async with company_connection(schema) as conn:
        known = await conn.fetchval(
            "SELECT 1 FROM drive_folders WHERE folder_id = $1", folder_id)
    if not known:
        raise UnknownDriveFolder(folder_id)
    return folder_id
