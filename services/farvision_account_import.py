"""Bulk-load the Farvision chart of accounts from a spreadsheet.

Same two-step shape as beneficiary_import: a preview that writes nothing, then
a commit. Simpler than that one because nothing here is resolved against
another master table -- every column is free text, copied from the sheet
exactly as Farvision itself will read it back.

'company' + 'account_head' is the natural key: a company keeps one ledger name
per party, and importing a corrected sheet should update that party's row
rather than add a second one next to it.
"""
from __future__ import annotations

import re

MAX_ROWS = 20000
PREVIEW_ROWS = 25

_COLUMNS = [
    "company", "account_head", "parent_account_head", "document_type",
    "financial_year", "bank_name", "deduction_type", "description",
    "entry_types", "debit_credit", "payment_mode", "payee_name", "docno",
    "invoice_no", "business_unit",
]

_ALIASES: dict[str, list[str]] = {
    "company": ["company"],
    "account_head": ["account head", "accounthead"],
    "parent_account_head": ["parent account head", "parent acc head", "parent head"],
    "document_type": ["document type", "doc type"],
    "financial_year": ["financial year", "fy"],
    "bank_name": ["bank name", "bankname", "bank"],
    "deduction_type": ["deduction type"],
    "description": ["description"],
    "entry_types": ["entrytypes", "entry types", "entry type"],
    "debit_credit": ["debit/credit", "debit credit", "dr/cr"],
    "payment_mode": ["payment mode"],
    "payee_name": ["payee name"],
    "docno": ["docno", "doc no"],
    "invoice_no": ["invoice no", "invoiceno"],
    "business_unit": ["business unit"],
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

    existing = {(r["company"] or "", r["account_head"] or ""): r["id"]
                for r in await conn.fetch(
                    "SELECT id, company, account_head FROM farvision_account_master")}

    parsed, errors, duplicates = [], [], []
    for row_num, raw in enumerate(data_rows, start=2):
        values = {col: (raw[i] if i < len(raw) else None) or None
                  for i, col in col_by_index.items()}
        account_head = (values.get("account_head") or "").strip()
        if not account_head:
            continue  # A wholly blank row, or one with no Account Head at all — not worth reporting as an error.

        problems = []
        if len(account_head) > 500:
            problems.append("Account Head is implausibly long")
        if problems:
            errors.append({"row": row_num, "name": account_head, "problems": problems})
            continue

        company = (values.get("company") or "").strip()
        key = (company, account_head)
        entry = {"row": row_num, **{c: (values.get(c) or "").strip() or None for c in _COLUMNS}}
        entry["account_head"] = account_head
        entry["company"] = company or None

        if key in existing:
            duplicates.append(entry)
        else:
            parsed.append(entry)

    return {
        "total_rows": len(data_rows),
        "importable": len(parsed),
        "duplicate_count": len(duplicates),
        "cross_company_count": 0,
        "error_count": len(errors),
        "unmapped_headers": unmapped,
        "preview": (parsed + duplicates)[:PREVIEW_ROWS],
        "errors": errors[:PREVIEW_ROWS],
        "errors_truncated": len(errors) > PREVIEW_ROWS,
        "duplicates": [{"row": d["row"], "name": d["account_head"],
                        "account_number": d["company"]} for d in duplicates[:PREVIEW_ROWS]],
        "duplicates_truncated": len(duplicates) > PREVIEW_ROWS,
        "_parsed": parsed,
        "_duplicates": duplicates,
    }


async def commit(conn, analysis: dict, on_duplicate: str) -> dict:
    parsed = analysis["_parsed"]
    duplicates = analysis["_duplicates"] if on_duplicate == "overwrite" else []
    skipped = len(analysis["_duplicates"]) if on_duplicate != "overwrite" else 0

    cols = ["company", "account_head"] + [c for c in _COLUMNS if c not in ("company", "account_head")]
    inserted = updated = 0

    async with conn.transaction():
        for entry in parsed:
            values = [entry.get(c) for c in cols]
            placeholders = ", ".join(f"${i+1}" for i in range(len(cols)))
            await conn.execute(
                f"INSERT INTO farvision_account_master ({', '.join(cols)}) "
                f"VALUES ({placeholders})",
                *values,
            )
            inserted += 1

        for entry in duplicates:
            set_clause = ", ".join(f"{c} = ${i+3}" for i, c in enumerate(
                c for c in cols if c not in ("company", "account_head")))
            values = [entry.get(c) for c in cols if c not in ("company", "account_head")]
            await conn.execute(
                f"UPDATE farvision_account_master SET {set_clause}, updated_at = now() "
                f"WHERE company IS NOT DISTINCT FROM $1 AND account_head = $2",
                entry.get("company"), entry.get("account_head"), *values,
            )
            updated += 1

    return {"inserted": inserted, "updated": updated, "skipped": skipped}
