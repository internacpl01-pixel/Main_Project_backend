"""Bulk-load beneficiaries from a spreadsheet.

Two steps, like every other import in this app: a preview that writes nothing
and tells you exactly what would happen, then a commit. The preview is what
makes the duplicate question answerable -- you cannot sensibly choose between
overwriting and skipping until you know how many rows it affects and which.

Every value that names a master row -- a head, a company -- is resolved against
this company's own master tables. A name that is not there is a rejected row,
not a new master entry: "Vender" is a typo, and a bulk import that quietly
creates it puts a head nobody chose into the list everyone picks from.

Structured so future sheet columns need no code change here beyond an alias:
RERA Head 1..3 and TCP Head 1..3 are already mapped and validated, so the day
those columns appear in the sheet they import without anything being edited.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# A sheet is a hand-maintained file; this is a sanity bound, not a policy.
MAX_ROWS = 5000

# How many resolved rows the preview returns. The counts above it describe the
# whole sheet -- this is only what gets drawn.
PREVIEW_ROWS = 25


def _norm(text: str) -> str:
    """Fold a header to its comparable form: 'RERA Head 1' -> 'rera head 1'.

    Everything that is not a letter or digit becomes a single space, so
    'A/C No.', 'A_C_No' and 'a c no' all arrive at the same string. Sheets are
    typed by people and the punctuation varies more than the words do.
    """
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# Sheet header -> beneficiary_master column. Written as {column: [aliases]} for
# readability and inverted below, because the aliases are what changes.
_ALIASES: dict[str, list[str]] = {
    "name": ["beneficiary name", "beneficiary", "name", "payee", "payee name",
             "party name", "vendor name"],
    "account_number": ["account number", "account no", "account num", "acct no",
                       "a c no", "a c number", "bank account number",
                       "account numbe"],
    "ifsc_code": ["ifsc code", "ifsc", "ifsc no"],
    "bank_name": ["bank name", "bank"],
    "company": ["company", "company name", "group company"],
    # The sheet's plain "Head 1" is the INTERNAL head — confirmed against the
    # file being loaded. The explicit "internal head 1" spelling is accepted too
    # so a later sheet can disambiguate without breaking this one.
    "head1": ["head 1", "head1", "internal head 1", "internal head1"],
    "head2": ["head 2", "head2", "internal head 2", "internal head2"],
    "head3": ["head 3", "head3", "internal head 3", "internal head3"],
    "rera_head1": ["rera head 1", "rera head1", "rera 1"],
    "rera_head2": ["rera head 2", "rera head2", "rera 2"],
    "rera_head3": ["rera head 3", "rera head3", "rera 3"],
    # Stored as idw_* because the table is idw_head_master; "TCP" is the label
    # that rename left in place. Both spellings are accepted.
    "idw_head1": ["tcp head 1", "tcp head1", "idw head 1", "idw head1"],
    "idw_head2": ["tcp head 2", "tcp head2", "idw head 2", "idw head2"],
    "idw_head3": ["tcp head 3", "tcp head3", "idw head 3", "idw head3"],
}

HEADER_MAP: dict[str, str] = {
    _norm(alias): column for column, aliases in _ALIASES.items() for alias in aliases
}

# Which master table each head column is checked against, and what to call it
# when the check fails.
_HEAD_SOURCES = {
    "head1": ("head_master", "Internal Head"),
    "head2": ("head_master", "Internal Head"),
    "head3": ("head_master", "Internal Head"),
    "rera_head1": ("rera_head_master", "RERA Head"),
    "rera_head2": ("rera_head_master", "RERA Head"),
    "rera_head3": ("rera_head_master", "RERA Head"),
    "idw_head1": ("idw_head_master", "TCP Head"),
    "idw_head2": ("idw_head_master", "TCP Head"),
    "idw_head3": ("idw_head_master", "TCP Head"),
}

# Groups whose members must differ, mirroring beneficiary_master's CHECKs.
_DISTINCT_GROUPS = (
    ("head1", "head2", "head3"),
    ("rera_head1", "rera_head2", "rera_head3"),
    ("idw_head1", "idw_head2", "idw_head3"),
)

COLUMNS = ["name", "account_number", "ifsc_code", "bank_name", "company",
           "rera_head1", "rera_head2", "rera_head3",
           "idw_head1", "idw_head2", "idw_head3",
           "head1", "head2", "head3"]


def find_header(grid: list[list[str]]) -> tuple[int, dict[int, str], list[str]]:
    """Locate the header row and map its cells to columns.

    Searched rather than assumed to be row 1, because exported sheets carry a
    title row, a blank, or a company name above the table often enough that
    assuming would fail on the first real file.

    The winner is the row mapping the most known columns; two matches is the
    floor, so a data row that happens to contain the word "Bank" cannot win.
    """
    best: tuple[int, dict[int, str]] | None = None
    for index, row in enumerate(grid[:20]):
        mapping = {}
        for position, cell in enumerate(row):
            column = HEADER_MAP.get(_norm(cell))
            # First spelling wins if a sheet repeats a column, rather than the
            # last silently replacing it.
            if column and column not in mapping.values():
                mapping[position] = column
        if len(mapping) >= 2 and (best is None or len(mapping) > len(best[1])):
            best = (index, mapping)

    if best is None:
        raise RuntimeError(
            "No header row was recognised. The sheet needs a row naming its "
            "columns — at least a beneficiary name and one other, such as "
            "'Beneficiary Name' and 'Account Number'."
        )

    index, mapping = best
    unmapped = [
        cell for position, cell in enumerate(grid[index])
        if cell and position not in mapping
    ]
    return index, mapping, unmapped


def read_records(grid: list[list[str]]) -> tuple[list[dict], dict, list[str]]:
    """Turn the grid into one dict per data row, keyed by column name.

    Row numbers are 1-based over the sheet, not the data, so a message about
    "row 14" points at what row 14 says when the file is opened.
    """
    header_index, mapping, unmapped = find_header(grid)

    records = []
    for offset, row in enumerate(grid[header_index + 1:], start=header_index + 2):
        record = {"_row": offset}
        for position, column in mapping.items():
            record[column] = (row[position] if position < len(row) else "").strip()
        if any(record.get(c) for c in COLUMNS):
            records.append(record)

    if len(records) > MAX_ROWS:
        raise RuntimeError(
            f"This sheet has {len(records)} rows and the importer accepts "
            f"{MAX_ROWS} at a time. Split it and import the parts."
        )

    detected = {grid[header_index][p]: c for p, c in mapping.items()}
    return records, detected, unmapped


async def _master_lookups(conn) -> dict[str, dict[str, str]]:
    """Every master name this import can resolve against, folded for matching.

    One query per table up front rather than a lookup per cell: a 500-row sheet
    with nine head columns would otherwise be 4,500 round trips.
    """
    lookups: dict[str, dict[str, str]] = {}
    for table in ("head_master", "rera_head_master", "idw_head_master"):
        rows = await conn.fetch(
            f"SELECT name FROM {table} WHERE is_active = true")
        lookups[table] = {r["name"].strip().lower(): r["name"] for r in rows}

    # A company is matched on its name OR its abbreviation, because a sheet
    # writes "DPL" where the master holds "DWARKADHIS PROJECTS PRIVATE LIMITED".
    # Either way the NAME is stored — that is what the dropdown shows, so an
    # imported row and a hand-entered one must not differ.
    companies = await conn.fetch(
        "SELECT name, abbreviation FROM company_master WHERE is_active = true")
    company_map: dict[str, str] = {}
    for row in companies:
        company_map[row["name"].strip().lower()] = row["name"]
        if row["abbreviation"]:
            company_map.setdefault(row["abbreviation"].strip().lower(), row["name"])
    lookups["company_master"] = company_map
    return lookups


def _resolve_row(record: dict, lookups: dict) -> tuple[dict | None, list[str]]:
    """Turn one sheet row into a beneficiary, or into the reasons it cannot be.

    Every problem with the row is collected rather than the first one raised:
    fixing a sheet one message per re-upload is the slowest possible way to do
    it, and the caller shows these all at once.
    """
    problems: list[str] = []
    resolved: dict = {}

    name = (record.get("name") or "").strip()
    if not name:
        problems.append("no beneficiary name")
    resolved["name"] = name

    for column in ("account_number", "ifsc_code", "bank_name"):
        resolved[column] = (record.get(column) or "").strip() or None

    company = (record.get("company") or "").strip()
    if company:
        match = lookups["company_master"].get(company.lower())
        if match is None:
            problems.append(f'company "{company}" is not in the Company master')
        resolved["company"] = match
    else:
        resolved["company"] = None

    for column, (table, label) in _HEAD_SOURCES.items():
        value = (record.get(column) or "").strip()
        if not value:
            resolved[column] = None
            continue
        match = lookups[table].get(value.lower())
        if match is None:
            problems.append(f'{label} "{value}" is not in the {label} master')
        resolved[column] = match

    # Checked here as well as in the database so the message names the sheet's
    # columns instead of quoting a constraint at someone reading a spreadsheet.
    for group in _DISTINCT_GROUPS:
        seen: dict[str, str] = {}
        for column in group:
            value = resolved.get(column)
            if not value:
                continue
            if value in seen:
                label = _HEAD_SOURCES[column][1]
                problems.append(
                    f'{label} "{value}" is given twice - the three must differ')
            seen[value] = column

    return (None if problems else resolved), problems


async def analyse(conn, grid: list[list[str]]) -> dict:
    """Work out what importing this sheet would do. Writes nothing.

    Returns the same shape the commit reports back, plus the detail needed to
    answer the duplicate question: which rows collide, and with what.
    """
    records, detected, unmapped = read_records(grid)
    lookups = await _master_lookups(conn)

    existing = {
        (r["account_number"] or "").strip().lower(): r["id"]
        for r in await conn.fetch(
            "SELECT id, account_number FROM beneficiary_master "
            "WHERE account_number IS NOT NULL AND account_number <> ''")
    }

    # Account number is the match, as asked. It leaves a gap: a beneficiary paid
    # in cash has none, so it can never match anything, and re-importing the same
    # sheet would add a second copy of every such row — silently, since nothing
    # about it looks like a duplicate. Those rows fall back to the name.
    #
    # Only those rows. Two beneficiaries may legitimately share a name (the same
    # payee recorded once per project), but both of those carry account numbers
    # and so never reach this.
    existing_by_name = {}
    for r in await conn.fetch(
        "SELECT id, name FROM beneficiary_master "
        "WHERE account_number IS NULL OR account_number = ''"
    ):
        existing_by_name.setdefault((r["name"] or "").strip().lower(), r["id"])

    ok: list[dict] = []
    errors: list[dict] = []
    duplicates: list[dict] = []
    seen_in_sheet: dict[str, int] = {}

    for record in records:
        resolved, problems = _resolve_row(record, lookups)
        if problems:
            errors.append({
                "row": record["_row"],
                "name": record.get("name") or "",
                "problems": problems,
            })
            continue

        account = (resolved["account_number"] or "").strip().lower()
        resolved["_row"] = record["_row"]

        # What this row is identified by: its account number, or its name when
        # it has none. The key is prefixed so a name can never collide with an
        # account number that happens to read the same.
        key = f"a:{account}" if account else f"n:{resolved['name'].strip().lower()}"

        # The same key twice in one sheet is the sheet's own duplicate, reported
        # rather than silently letting the later row win.
        if key in seen_in_sheet:
            what = "account number" if account else "name"
            errors.append({
                "row": record["_row"],
                "name": resolved["name"],
                "problems": [f"{what} repeats row {seen_in_sheet[key]} in this sheet"],
            })
            continue
        seen_in_sheet[key] = record["_row"]

        existing_id = (existing.get(account) if account
                       else existing_by_name.get(resolved["name"].strip().lower()))
        if existing_id is not None:
            resolved["_existing_id"] = existing_id
            duplicates.append({
                "row": record["_row"],
                "name": resolved["name"],
                "account_number": resolved["account_number"],
                "matched_on": "account number" if account else "name",
                "existing_id": existing_id,
            })
        ok.append(resolved)

    return {
        "total_rows": len(records),
        "importable": len(ok) - len(duplicates),
        "duplicate_count": len(duplicates),
        "error_count": len(errors),
        "duplicates": duplicates[:PREVIEW_ROWS],
        "errors": errors[:PREVIEW_ROWS],
        "errors_truncated": len(errors) > PREVIEW_ROWS,
        "duplicates_truncated": len(duplicates) > PREVIEW_ROWS,
        "columns_detected": detected,
        "unmapped_headers": unmapped,
        "preview": [
            {k: v for k, v in r.items() if not k.startswith("_")}
            for r in ok[:PREVIEW_ROWS]
        ],
        "_rows": ok,
    }


async def commit(conn, analysis: dict, on_duplicate: str) -> dict:
    """Write the rows the analysis found acceptable.

    on_duplicate is 'skip' or 'overwrite' and only applies to rows matched on
    account number. Rows carrying no account number cannot be matched at all and
    are always inserted — there is nothing to compare them against.

    One transaction, because a half-applied sheet is worse than a refused one:
    you cannot tell by looking which half went in.
    """
    if on_duplicate not in ("skip", "overwrite"):
        raise RuntimeError("on_duplicate must be 'skip' or 'overwrite'.")

    inserted = updated = skipped = 0
    columns = ", ".join(COLUMNS)
    placeholders = ", ".join(f"${i + 1}" for i in range(len(COLUMNS)))
    assignments = ", ".join(f"{c} = ${i + 1}" for i, c in enumerate(COLUMNS))

    async with conn.transaction():
        for row in analysis["_rows"]:
            values = [row.get(c) for c in COLUMNS]
            existing_id = row.get("_existing_id")

            if existing_id is None:
                await conn.execute(
                    f"INSERT INTO beneficiary_master ({columns}) "
                    f"VALUES ({placeholders})", *values)
                inserted += 1
            elif on_duplicate == "overwrite":
                await conn.execute(
                    f"UPDATE beneficiary_master SET {assignments} "
                    f"WHERE id = ${len(COLUMNS) + 1}", *values, existing_id)
                updated += 1
            else:
                skipped += 1

    logger.info("[beneficiary import] +%d ~%d skipped=%d rejected=%d",
                inserted, updated, skipped, analysis["error_count"])
    return {
        "inserted": inserted,
        "updated": updated,
        "skipped": skipped,
        "rejected": analysis["error_count"],
    }
