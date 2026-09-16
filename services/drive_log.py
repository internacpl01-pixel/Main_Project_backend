"""
Persistent history of what /imports/from-drive has done to every file it has
ever touched. services/jobs.py's in-memory registry is the live view of a
run in progress -- it is pruned 15 minutes after the job finishes and lost
entirely on a restart, so it cannot answer "what happened to that statement
last Tuesday". This table is that answer.
"""
from __future__ import annotations

from database import company_connection


async def log_drive_result(
    schema: str, *, file_name: str, status: str, imported_by: str,
    error: str | None = None, row_count: int | None = None,
    bank_id: int | None = None,
) -> None:
    async with company_connection(schema) as conn:
        await conn.execute(
            """
            INSERT INTO drive_import_log
                (file_name, status, error, row_count, bank_id, imported_by)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            file_name, status, error, row_count, bank_id, imported_by,
        )


async def list_drive_log(
    schema: str, *, status: str | None, limit: int, offset: int,
) -> tuple[list[dict], int]:
    where, params = "", []
    if status:
        where, params = "WHERE l.status = $1", [status]

    async with company_connection(schema) as conn:
        total = await conn.fetchval(
            f"SELECT count(*) FROM drive_import_log l {where}", *params)
        rows = await conn.fetch(
            f"""
            SELECT l.id, l.file_name, l.status, l.error, l.row_count,
                   l.bank_id, bm.bank_name, l.imported_by, l.created_at
            FROM drive_import_log l
            LEFT JOIN bank_master bm ON bm.id = l.bank_id
            {where}
            ORDER BY l.created_at DESC, l.id DESC
            LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}
            """,
            *params, limit, offset,
        )
    return [dict(r) for r in rows], total
