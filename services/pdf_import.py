"""
PDF bank-statement import.

Parsing is DPL's parsers.py, unmodified. This module does three things around
it: feed it the company's fieldmap, collapse its withdrawal/deposits output
into temp_trans's (amount, credit_debit), and record the batch.

Connection handling is deliberate. The parser can run for up to three minutes
on a large scanned-ish statement, and it does no database work. Holding a
pooled connection -- and an open transaction, since company_connection wraps
one -- across that would pin a Supabase connection and keep a transaction idle
for minutes. So the fieldmap read, the parse, and the write are three separate
steps, and no connection is held while the CPU work happens.
"""
from __future__ import annotations

import asyncio
import logging
import time

from import_helpers import compute_fill_rates, normalize_parsed_rows
from services.fieldmap import get_field_mappings, live_col_types
from services.staging import stage_batch

logger = logging.getLogger(__name__)

PARSE_TIMEOUT_SECONDS = 180.0


async def process_pdf_import(
    schema: str,
    file_bytes: bytes,
    filename: str,
    username: str,
    bank_id: int | None = None,
    password: str = "",
    save: bool = False,
) -> dict:
    """Parse a statement and, when save=True, stage it into temp_trans.

    save=False is a dry run: it returns exactly what would be staged, so the
    user can check the column mapping and fill rates before committing a batch.
    Nothing is written and no batch is created.
    """
    t_start = time.perf_counter()

    fieldmap_rows = await get_field_mappings(schema)
    if not fieldmap_rows:
        raise RuntimeError(
            "This company has no fieldmap rows, so no column header can be "
            "recognised. Run: python -m db.migrate upgrade"
        )
    col_types = live_col_types(fieldmap_rows)

    # --- Parse (no database connection held) ---------------------------------
    from parsers import _parse_sync

    loop = asyncio.get_running_loop()
    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None, _parse_sync, file_bytes, password, fieldmap_rows, col_types
            ),
            timeout=PARSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.error("[PDF] parser timed out after %ss", PARSE_TIMEOUT_SECONDS)
        raise RuntimeError(
            f"PDF parsing timed out (>{PARSE_TIMEOUT_SECONDS:.0f}s). "
            "Try a smaller file, or check the PDF is text-based and not a scan."
        )
    t_parse = (time.perf_counter() - t0) * 1000

    parsed_rows = result.get("rows", [])
    normalized, norm_stats = normalize_parsed_rows(parsed_rows, fieldmap_rows)
    logger.info(
        "[PDF] parse %.0fms, parsed=%d usable=%d", t_parse, len(parsed_rows), len(normalized)
    )

    parse_stats = {
        **result.get("stats", {}),
        **norm_stats,
        "parse_ms": round(t_parse),
        "headers_detected": result.get("headers_detected", {}),
        "unmapped_headers": result.get("unmapped_headers", []),
    }

    payload = {
        "saved": False,
        "batch_id": None,
        "row_count": len(normalized),
        "rows": normalized[:200],          # preview cap; the batch holds them all
        "truncated": len(normalized) > 200,
        "headers_detected": result.get("headers_detected", {}),
        "unmapped_headers": result.get("unmapped_headers", []),
        "document_fields": result.get("document_fields", {}),
        "fill_rates": compute_fill_rates(parsed_rows),
        "stats": parse_stats,
        "duplicate_rows": 0,
    }

    if not save:
        payload["total_ms"] = round((time.perf_counter() - t_start) * 1000)
        return payload

    if not normalized:
        raise RuntimeError(
            "No usable transaction rows were found. Check headers_detected in "
            "the dry run -- if it is empty, this bank's column names are not in "
            "the fieldmap yet."
        )

    # --- Write ----------------------------------------------------------------
    staged = await stage_batch(
        schema, file_bytes, filename, username, bank_id, normalized, parse_stats
    )

    payload.update(
        saved=True,
        batch_id=staged["batch_id"],
        row_count=staged["inserted"],
        duplicate_rows=staged["duplicate_rows"],
        total_ms=round((time.perf_counter() - t_start) * 1000),
    )
    return payload
