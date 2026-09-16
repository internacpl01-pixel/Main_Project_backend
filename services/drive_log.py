"""
Persistent history of what /imports/from-drive has done to every file it has
ever touched. services/jobs.py's in-memory registry is the live view of a
run in progress -- it is pruned 15 minutes after the job finishes and lost
entirely on a restart, so it cannot answer "what happened to that statement
last Tuesday". This table is that answer.
"""
from __future__ import annotations

import datetime

from database import company_connection

# How long an entry is worth keeping -- confirmed with the user. Pruned on
# write rather than on a timer, the same reasoning as services/jobs.py's own
# _prune_locked: there is no scheduler in this app, and a table that only
# grows is the one way a permanent log like this turns into an unbounded one.
RETENTION_DAYS = 30


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
        await conn.execute(
            "DELETE FROM drive_import_log WHERE created_at < now() - "
            f"interval '{RETENTION_DAYS} days'")


async def list_drive_log(
    schema: str, *, status: str | None, date_from: str | None,
    date_to: str | None, limit: int, offset: int,
) -> tuple[list[dict], int]:
    clauses, params = [], []
    if status:
        params.append(status)
        clauses.append(f"l.status = ${len(params)}")
    if date_from:
        # asyncpg infers the bind's type from the query itself; since the SQL
        # casts it to date, the value handed in has to already be a
        # datetime.date, not the "yyyy-mm-dd" string the query param arrives
        # as -- passing the string raises DataError deep in the driver.
        params.append(datetime.date.fromisoformat(date_from))
        clauses.append(f"l.created_at >= ${len(params)}::date")
    if date_to:
        # Inclusive of the whole "to" day, not just its midnight. Cast
        # needed on both sides of the "+" -- left bare, Postgres can't tell
        # $2's type from the bind alone and the interval arithmetic fails.
        params.append(datetime.date.fromisoformat(date_to))
        clauses.append(f"l.created_at < (${len(params)}::date + interval '1 day')")
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

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
