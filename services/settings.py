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
