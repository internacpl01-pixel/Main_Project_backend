"""
Spreadsheet and CSV bank-statement import.

Both formats reduce to the same thing -- a grid of strings -- so they share one
pipeline from there: the same fieldmap, the same header detection, the same row
assembly, the same normalization and the same batch write as the PDF path.

That sharing is the point. DPL's /upload had a separate CSV branch built on
csv.DictReader, which meant CSV bypassed alias matching entirely: a column
titled "Withdrawal Amt." became a dict key of that literal string and never
mapped to the withdrawal field. Feeding CSV through _detect_header_row instead
means a statement imports identically whether the bank sent it as PDF, XLSX or
CSV.

Replaces services/excel_import.py.
"""
from __future__ import annotations

import csv
import io
import logging
import time
from datetime import date, datetime

import openpyxl

from import_helpers import compute_fill_rates, normalize_parsed_rows
from parsers import (_assemble_rows, _build_alias_map, _detect_header_row,
                     _extract_document_level_fields)
from services.fieldmap import get_field_mappings, live_col_types
from services.staging import stage_batch

logger = logging.getLogger(__name__)


def _read_excel_rows(file_bytes: bytes) -> list:
    """Read the first sheet as a grid of strings.

    Date and datetime cells become ISO date strings. Left as datetimes, str()
    renders "2026-08-04 00:00:00", which the date detectors in parsers.py
    reject -- every row would look undated and be skipped.
    """
    wb = openpyxl.load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    rows = []
    for row in ws.iter_rows(values_only=True):
        cells = []
        for c in row:
            if c is None:
                cells.append("")
            elif isinstance(c, (datetime, date)):
                cells.append(c.strftime("%Y-%m-%d"))
            else:
                cells.append(str(c).strip())
        if any(cells):
            rows.append(cells)
    wb.close()
    return rows


def _read_csv_rows(file_bytes: bytes) -> list:
    """Read a CSV as a grid of strings.

    utf-8-sig strips the BOM Excel writes, which would otherwise glue itself to
    the first header cell and stop "Date" matching its alias. The delimiter is
    sniffed because Indian bank exports use comma, semicolon and tab about
    equally; a failed sniff falls back to comma rather than raising.
    """
    text = file_bytes.decode("utf-8-sig", errors="replace")

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    rows = []
    for row in csv.reader(io.StringIO(text, newline=""), dialect):
        cells = [(c or "").strip() for c in row]
        if any(cells):
            rows.append(cells)
    return rows


READERS = {
    "excel": (_read_excel_rows, (".xlsx", ".xls")),
    "csv": (_read_csv_rows, (".csv",)),
}


async def process_tabular_import(
    schema: str,
    file_bytes: bytes,
    filename: str,
    username: str,
    kind: str,
    bank_id: int | None = None,
    save: bool = False,
) -> dict:
    """Parse a spreadsheet or CSV and, when save=True, stage it into temp_trans.

    kind is "excel" or "csv" and selects only the reader; everything after the
    grid is identical for both.
    """
    t_start = time.perf_counter()

    fieldmap_rows = await get_field_mappings(schema)
    if not fieldmap_rows:
        raise RuntimeError(
            "This company has no fieldmap rows, so no column header can be "
            "recognised. Run: python -m db.migrate upgrade"
        )
    alias_map = _build_alias_map(fieldmap_rows)
    col_types = live_col_types(fieldmap_rows)

    reader, _ = READERS[kind]
    try:
        grid = reader(file_bytes)
    except Exception as e:
        raise RuntimeError(f"Failed to read {kind} file: {e}")

    if not grid:
        raise RuntimeError("The file has no rows.")

    header_idx, col_mapping = _detect_header_row(grid, alias_map)
    if header_idx < 0 or not col_mapping:
        raise RuntimeError(
            "Could not find a header row. None of the column titles in this "
            "file match a fieldmap alias -- add the bank's spelling to "
            "fieldmap.mapfields and retry."
        )

    headers_detected = {
        fieldname: grid[header_idx][col_idx]
        for col_idx, fieldname in col_mapping.items()
        if col_idx < len(grid[header_idx])
    }
    unmapped_headers = [
        str(cell).strip()
        for col_idx, cell in enumerate(grid[header_idx])
        if str(cell).strip() and col_idx not in col_mapping
    ]

    assembled, carry = _assemble_rows(grid, header_idx, col_mapping, col_types)
    if carry:
        assembled.append(carry)

    # Values printed above the table (account number, statement period) belong
    # to every row in the file, so they are back-filled onto each one.
    doc_fields = {}
    if assembled and header_idx > 0:
        header_text = "\n".join(" ".join(r) for r in grid[:header_idx])
        filled_keys = set()
        for r in assembled:
            filled_keys.update(r.keys())
        doc_fields = _extract_document_level_fields(header_text, fieldmap_rows, filled_keys)
        for r in assembled:
            for fn, val in doc_fields.items():
                r.setdefault(fn, val)

    normalized, norm_stats = normalize_parsed_rows(assembled, fieldmap_rows)
    t_parse = (time.perf_counter() - t_start) * 1000
    logger.info(
        "[%s] parse %.0fms, assembled=%d usable=%d",
        kind, t_parse, len(assembled), len(normalized),
    )

    parse_stats = {
        **norm_stats,
        "parse_ms": round(t_parse),
        "headers_detected": headers_detected,
        "unmapped_headers": unmapped_headers,
        "source": kind,
    }

    payload = {
        "saved": False,
        "batch_id": None,
        "row_count": len(normalized),
        "rows": normalized[:200],
        "truncated": len(normalized) > 200,
        "headers_detected": headers_detected,
        "unmapped_headers": unmapped_headers,
        "document_fields": doc_fields,
        "fill_rates": compute_fill_rates(assembled),
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
