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


async def _bank_check(conn, bank_id: int | None) -> None:
    """Raise unless bank_id names a row in this company's bank_master.

    The message names what IS available, because the id is chosen from a
    dropdown the caller may be out of step with — and because the most common
    case, a company that has not entered its banks yet, is not a mistyped id at
    all and needs a different instruction.
    """
    if bank_id is None:
        return
    if await conn.fetchval("SELECT 1 FROM bank_master WHERE id = $1", bank_id):
        return

    available = await conn.fetch(
        "SELECT id, bank_name FROM bank_master WHERE is_active = true ORDER BY id"
    )
    if available:
        options = ", ".join(f"{r['id']} ({r['bank_name']})" for r in available)
        raise RuntimeError(
            f"bank_id {bank_id} does not exist in bank_master. Available: {options}."
        )
    raise RuntimeError(
        f"bank_id {bank_id} does not exist: this company has no banks yet. "
        f"Leave the bank empty, or add one on the Master Data page first."
    )


async def assert_bank_exists(schema: str, bank_id: int | None) -> None:
    """The same check, run before the file is parsed.

    stage_batch checks this too and has to — it owns the transaction that
    writes the batch. But that check happens after parsing, so an unusable bank
    id on a long statement is reported only once the parse has finished, which
    on a 65-page file is minutes of work thrown away over a value that could be
    rejected instantly. One indexed lookup, and only when a bank was named.
    """
    if bank_id is None:
        return
    async with company_connection(schema) as conn:
        await _bank_check(conn, bank_id)


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
    hash_scope: str = "",
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
    if hash_scope:
        # A partial import is not the same upload as the whole file, nor as a
        # different part of it. Folding the scope in keeps each part distinct
        # while leaving a whole-file import hashing exactly as it always has —
        # so every batch already recorded still matches itself.
        file_hash = hashlib.sha256(f"{file_hash}:{hash_scope}".encode()).hexdigest()

    async with company_connection(schema) as conn:
        existing = await conn.fetchrow(
            "SELECT id, filename, uploaded_at, status FROM import_batches WHERE file_hash = $1",
            file_hash,
        )
        if existing is not None:
            raise DuplicateFileError(dict(existing))

        await _bank_check(conn, bank_id)

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
