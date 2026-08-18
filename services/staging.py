"""
The write half of an import, shared by the PDF and Excel paths.

Both importers produce the same thing -- a list of normalized rows plus parse
statistics -- so the batch record, the duplicate checks and the temp_trans
insert are written once here. Keeping them together is what guarantees a PDF
and an Excel file of the same statement are staged identically.
"""
from __future__ import annotations

import hashlib
import json
import logging

from database import company_connection
from import_helpers import count_duplicate_rows, insert_temp_rows

logger = logging.getLogger(__name__)


class DuplicateFileError(RuntimeError):
    """This exact file has already been uploaded for this company."""

    def __init__(self, batch: dict):
        self.batch = batch
        super().__init__(
            f"This file was already uploaded on "
            f"{batch['uploaded_at']:%Y-%m-%d %H:%M} as batch {batch['id']} "
            f"({batch['filename']}, status={batch['status']})."
        )


async def stage_batch(
    schema: str,
    file_bytes: bytes,
    filename: str,
    username: str,
    bank_id: int | None,
    normalized: list,
    parse_stats: dict,
) -> dict:
    """Create the batch row and stage its lines into temp_trans.

    One transaction, because company_connection wraps one: either the batch and
    every row land, or nothing does. A batch that exists with half its rows
    would look complete in the UI and quietly under-report the month.

    The file_hash check is the hard stop against re-uploading the same
    statement. It is checked explicitly rather than left to the UNIQUE
    constraint so the caller can report *which* batch it collided with.
    """
    file_hash = hashlib.sha256(file_bytes).hexdigest()

    async with company_connection(schema) as conn:
        existing = await conn.fetchrow(
            "SELECT id, filename, uploaded_at, status FROM import_batches WHERE file_hash = $1",
            file_hash,
        )
        if existing is not None:
            raise DuplicateFileError(dict(existing))

        if bank_id is not None:
            known = await conn.fetchval("SELECT 1 FROM bank_master WHERE id = $1", bank_id)
            if not known:
                raise RuntimeError(f"bank_id {bank_id} does not exist in bank_master.")

        batch_id = await conn.fetchval(
            """
            INSERT INTO import_batches
                (filename, file_hash, bank_id, uploaded_by, row_count, status, parse_stats)
            VALUES ($1, $2, $3, $4, $5, 'uploaded', $6::jsonb)
            RETURNING id
            """,
            filename,
            file_hash,
            bank_id,
            username,
            len(normalized),
            json.dumps(parse_stats, default=str),
        )

        inserted = await insert_temp_rows(conn, batch_id, normalized)
        duplicates = await count_duplicate_rows(conn, batch_id)

    logger.info("[stage] batch %s: %d rows, %d duplicate rows", batch_id, inserted, duplicates)
    return {"batch_id": batch_id, "inserted": inserted, "duplicate_rows": duplicates}
