"""Bulk-load the Farvision chart of accounts from a spreadsheet.

Same two-step shape as beneficiary_import: a preview that writes nothing, then
a commit. Simpler than that one because nothing here is resolved against
another master table -- every column is free text, copied from the sheet
exactly as Farvision itself will read it back.

One sheet, two tables: farvision_account_master_dpl and _amb each hold one
company's chart of accounts, split by the sheet's own Company column so DPL
and AMB rows never collide even when they happen to share an Account Head
name. A row whose Company isn't recognised as DPL or AMB is reported as an
error rather than guessed into either table.

Only Account Head and Parent Account Head are stored -- confirmed with the
user that these are the only two columns genuinely tied to a specific
Account Head; the sheet's other columns (Document Type, Financial Year, Bank
Name, Deduction Type, Description, EntryTypes, Debit/Credit, Payment Mode,
Payee Name, Docno, Invoice No, Business Unit) were near-empty across the real
data and are reference/format values now hardcoded in services.farvision
instead. If the sheet has those columns they simply show up as unmapped
headers -- harmless, not an error.

No duplicate checking: the user's real sheet legitimately repeats the same
Account Head more than once per company, and every row is wanted as its own
row rather than collapsed onto whatever's already in the table (confirmed
with the user; the tables no longer have a UNIQUE(account_head) constraint
for exactly this reason). Every valid row is a plain INSERT.
"""
from __future__ import annotations

import re

MAX_ROWS = 20000
PREVIEW_ROWS = 25

_TABLES = {"DPL": "farvision_account_master_dpl", "AMB": "farvision_account_master_amb"}

_COLUMNS = ["account_head", "parent_account_head"]

_ALIASES: dict[str, list[str]] = {
    "company": ["company"],
    "account_head": ["account head", "accounthead"],
    "parent_account_head": ["parent account head", "parent acc head", "parent head"],
}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _header_map(header_row: list[str]) -> dict[int, str]:
    alias_to_col = {}
    for col, aliases in _ALIASES.items():
        for a in aliases:
            alias_to_col[a] = col
    out = {}
    for i, cell in enumerate(header_row):
        col = alias_to_col.get(_norm(cell))
        if col:
            out[i] = col
    return out


async def analyse(conn, grid: list[list[str]]) -> dict:
    if not grid:
        raise RuntimeError("That file has no rows.")
    header_row, *data_rows = grid
    col_by_index = _header_map(header_row)
    if "account_head" not in col_by_index.values():
        raise RuntimeError(
            'No "Account Head" column found. Check the sheet has a header row.')

    if len(data_rows) > MAX_ROWS:
        raise RuntimeError(
            f"That sheet has {len(data_rows)} rows; {MAX_ROWS} is the most "
            f"this can import at once. Split it and import in parts.")

    unmapped = sorted({
        header_row[i] for i in range(len(header_row))
        if i not in col_by_index and (header_row[i] or "").strip()
    })

    errors = []
    parsed = []
    for row_num, raw in enumerate(data_rows, start=2):
        values = {col: (raw[i] if i < len(raw) else None) or None
                  for i, col in col_by_index.items()}
        account_head = (values.get("account_head") or "").strip()
        if not account_head:
            continue  # A wholly blank row, or one with no Account Head at all — not worth reporting as an error.

        raw_company = (values.get("company") or "").strip()
        company = raw_company.upper()

        problems = []
        if len(account_head) > 500:
            problems.append("Account Head is implausibly long")
        if company not in _TABLES:
            problems.append(
                f'Company must be DPL or AMB, got "{raw_company or "(blank)"}"')
        if problems:
            errors.append({"row": row_num, "name": account_head, "problems": problems})
            continue

        entry = {"row": row_num, **{c: (values.get(c) or "").strip() or None for c in _COLUMNS}}
        entry["account_head"] = account_head
        entry["company"] = company
        parsed.append(entry)

    return {
        "total_rows": len(data_rows),
        "importable": len(parsed),
        "duplicate_count": 0,
        "cross_company_count": 0,
        "sheet_duplicate_count": 0,
        "error_count": len(errors),
        "unmapped_headers": unmapped,
        "preview": parsed[:PREVIEW_ROWS],
        "errors": errors[:PREVIEW_ROWS],
        "errors_truncated": len(errors) > PREVIEW_ROWS,
        "duplicates": [],
        "duplicates_truncated": False,
        "_parsed": parsed,
    }


async def commit(conn, analysis: dict, on_duplicate: str | None = None) -> dict:
    parsed = analysis["_parsed"]
    cols = _COLUMNS

    # executemany pipelines every row over one prepared statement instead of
    # one round trip to the database per row -- the difference between a
    # sheet with thousands of rows finishing in a couple of seconds and it
    # timing out before the last row is even sent. A 15,885-row sheet is
    # exactly the case that surfaced this: one execute() per row never
    # finished inside any reasonable request deadline.
    async with conn.transaction():
        for company, table in _TABLES.items():
            rows = [e for e in parsed if e["company"] == company]
            if rows:
                placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
                await conn.executemany(
                    f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})",
                    [[entry.get(c) for c in cols] for entry in rows],
                )

    return {"inserted": len(parsed), "updated": 0, "skipped": 0}
