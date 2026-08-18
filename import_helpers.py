"""
Bridge between the DPL parser and Main_Project's temp_trans table.

parsers.py is copied byte-for-byte from DPL_project and emits rows keyed by
fieldmap fieldname -- txn_date, description, withdrawal, deposits, balance,
reference_no. DPL wrote those straight into a wide, user-defined `master`
table. Here the target is temp_trans, which has five fixed columns and models
an amount as (amount, credit_debit) rather than as two opposing columns.

Everything that reconciles those two shapes lives in this file, so parsers.py
itself never needs a local edit and stays a clean copy of DPL's.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date as _date
from decimal import Decimal, InvalidOperation

from parsers import (_build_alias_map, _category_map_from_aliases,
                     _category_of)
from services.custom_fields import table_structure

logger = logging.getLogger(__name__)


def fields_by_category(fieldmap_rows: list) -> dict:
    """{category: fieldname} for one company, resolved live from its fieldmap.

    This is the whole reason nothing below names a field literally. parsers.py
    already decides a column's semantic role from the fieldmap -- by the row's
    own fieldname when that is already a concept ("withdrawal"), and otherwise
    by any alias mapped onto it, so a row called `debit_amt` whose mapfields
    contain "debit" IS the withdrawal column. This reads that same decision back
    out, so the row keys the parser emits are found by role rather than by name.

    Renaming a fieldmap row used to silently drop every transaction on that
    column: the parser kept matching the header and emitting the new key, while
    this module still asked for the old one, got None, and counted the row as
    having no amount. No error, just a short ledger.
    """
    alias_map = _build_alias_map(fieldmap_rows or [])
    cat_by_field = _category_map_from_aliases(alias_map)

    by_cat: dict[str, str] = {}
    for row in (fieldmap_rows or []):
        fieldname = row.get("fieldname") or ""
        cat = _category_of(fieldname, cat_by_field)
        # First fieldmap row to claim a category wins. Rows come back ordered by
        # id, so the seeded core fields take precedence over anything added
        # later that happens to carry an overlapping alias.
        if cat and cat not in by_cat:
            by_cat[cat] = fieldname
    return by_cat


def _parse_date_to_date(val) -> _date | None:
    """Parse various date string formats into a datetime.date object.

    Copied unchanged from DPL_project/backend/import_helpers.py -- the same
    formats appear in the same statements, and divergence here would mean PDFs
    that import in one project and not the other.
    """
    s = str(val).strip() if val else ""
    if not s:
        return None
    # YYYY-MM-DD
    if len(s) == 10 and s[4] == "-":
        try:
            return _date.fromisoformat(s)
        except ValueError:
            pass
    # DD/MM/YYYY or DD-MM-YYYY
    m = re.match(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", s)
    if m:
        try:
            return _date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        except ValueError:
            pass
    # DD-Mon-YYYY
    m = re.match(r"(\d{1,2})-([A-Za-z]{3,})-(\d{2,4})", s)
    if m:
        month_map = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
                     "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
        mon = month_map.get(m.group(2).lower()[:3])
        if mon:
            try:
                year = m.group(3)
                if len(year) == 2:
                    year = "20" + year
                return _date(int(year), mon, int(m.group(1)))
            except ValueError:
                pass
    return None


def _to_amount(val) -> Decimal | None:
    """Coerce a parsed cell to numeric(18,2), or None if it isn't a number.

    Statements print amounts as "1,23,456.78", sometimes with a trailing Cr/Dr
    marker or a currency symbol. Anything that survives stripping those and
    still parses as a Decimal is an amount; anything else is text that landed
    in an amount column and is discarded rather than guessed at.
    """
    if val is None:
        return None
    s = str(val).strip()
    if not s:
        return None
    s = re.sub(r"(?i)\b(cr|dr)\b\.?$", "", s).strip()
    s = s.replace(",", "").replace("₹", "").replace("INR", "").strip()
    if not s or s in {"-", "--", "."}:
        return None
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


def row_content_hash(txn_date, description, amount, credit_debit) -> str:
    """Content fingerprint of one statement line, independent of which file it
    came from.

    Used for the soft duplicate warning: the same line appearing in two
    different statements (overlapping periods) is worth flagging but must not
    block the import, which is why 002 dropped the UNIQUE constraint that used
    to sit on this value. The hard block against re-uploading an identical file
    is import_batches.file_hash.
    """
    parts = [
        txn_date.isoformat() if txn_date else "",
        (description or "").strip().lower(),
        str(amount) if amount is not None else "",
        credit_debit or "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def normalize_parsed_rows(rows: list, fieldmap_rows: list) -> tuple[list, dict]:
    """Turn parser output into rows shaped like temp_trans.

    Returns (normalized, stats). Each normalized row is a dict with exactly the
    keys temp_trans needs, plus raw_data holding the parser's original output
    for that line.

    Which parser key holds the date, the narration and the two amount columns is
    read from this company's fieldmap through fields_by_category() -- nothing
    here assumes a column is called "withdrawal". temp_trans's own columns are
    fixed, but the route from a bank's spelling to those columns is entirely
    the fieldmap's to decide.

    Two-column withdrawal/deposits collapses into (amount, credit_debit):
    a value under withdrawal is a DR, a value under deposits is a CR. A line
    with neither is not a transaction -- it is a carried-forward balance, a page
    header, or a subtotal the parser could not classify -- and is skipped rather
    than posted as a zero.
    """
    by_cat = fields_by_category(fieldmap_rows)
    f_date = by_cat.get("date", "txn_date")
    f_desc = by_cat.get("description", "description")
    f_out = by_cat.get("withdrawal", "withdrawal")
    f_in = by_cat.get("deposits", "deposits")
    f_bal = by_cat.get("balance", "balance")

    normalized = []
    skipped_no_amount = 0
    skipped_no_date = 0
    ambiguous = 0

    for raw in rows:
        txn_date = _parse_date_to_date(raw.get(f_date))
        withdrawal = _to_amount(raw.get(f_out))
        deposits = _to_amount(raw.get(f_in))

        # Zero is not an amount -- statements print 0.00 in the unused column.
        if withdrawal is not None and withdrawal == 0:
            withdrawal = None
        if deposits is not None and deposits == 0:
            deposits = None

        if withdrawal is not None and deposits is not None:
            # Both columns populated is a column-alignment failure upstream.
            # Withdrawal wins so the row is still reviewable, and the anomaly is
            # visible in raw_data rather than silently averaged away.
            ambiguous += 1
            deposits = None

        if withdrawal is None and deposits is None:
            skipped_no_amount += 1
            continue
        if txn_date is None:
            skipped_no_date += 1
            continue

        amount = withdrawal if withdrawal is not None else deposits
        credit_debit = "DR" if withdrawal is not None else "CR"

        description = (raw.get(f_desc) or "").strip() or None

        # Keys here are temp_trans column names, which are fixed -- unlike the
        # keys read out of `raw` above, which are whatever the fieldmap says.
        normalized.append({
            "txn_date": txn_date,
            "description": description,
            "amount": amount,
            "credit_debit": credit_debit,
            "balance": _to_amount(raw.get(f_bal)),
            "row_hash": row_content_hash(txn_date, description, amount, credit_debit),
            "raw_data": raw,
        })

    stats = {
        "parsed": len(rows),
        "usable": len(normalized),
        "skipped_no_amount": skipped_no_amount,
        "skipped_no_date": skipped_no_date,
        "ambiguous_both_columns": ambiguous,
        # Which fieldmap row filled each role for this import. Without it, a
        # miscategorised column looks identical to a bank that omitted it.
        "resolved_fields": {
            "date": f_date, "description": f_desc, "withdrawal": f_out,
            "deposits": f_in, "balance": f_bal,
        },
    }
    return normalized, stats


def _coerce(value, data_type: str):
    """Cast one parsed value to what its column expects, or None if it can't.

    Same three-way split DPL used in append_rows_to_master: dates through the
    date parser, numerics through Decimal, everything else trimmed text. A value
    that fails its column's type is dropped rather than guessed at -- it is
    still in raw_data if anyone needs to see what the bank actually printed.
    """
    if value is None or value == "":
        return None
    t = (data_type or "").lower()
    if t in ("date", "timestamp without time zone", "timestamp"):
        return _parse_date_to_date(value)
    if t in ("numeric", "real", "double precision", "integer", "bigint"):
        return _to_amount(value)
    return str(value).strip() or None


async def insert_temp_rows(conn, batch_id: int, normalized: list) -> int:
    """Insert normalized rows into temp_trans for one batch.

    row_number is the row's position in the file, 1-based. Together with
    batch_id it is the row's identity (UNIQUE (batch_id, row_number) from 002),
    which is what makes a retry of a half-finished insert fail loudly instead of
    duplicating lines.

    The column list is built from the live table, not written out here. Custom
    fields add real columns to temp_trans, and a fixed INSERT would leave every
    one of them NULL forever -- the field would appear on the Custom Fields page,
    match a header during parsing, show up in raw_data, and still never reach
    its own column. DPL had the same requirement and solved it the same way, by
    intersecting the row's keys with the table's live columns.
    """
    if not normalized:
        return 0

    live = {c["column_name"]: c["data_type"] for c in await table_structure(conn)}

    # DERIVED are the values the importer computes rather than reads: the batch
    # link, the row's position and fingerprint, and the (amount, credit_debit)
    # pair the two-column collapse produces. Intersected with the live table,
    # never asserted -- every field is deletable, so a column may simply not be
    # there any more, and DPL took the same precaution in append_rows_to_master.
    # It is the difference between "that field stopped being recorded" and
    # "every import 500s".
    derived = [c for c in ("batch_id", "row_number", "row_hash", "txn_date",
                           "description", "amount", "credit_debit", "balance",
                           "raw_data")
               if c in live]

    # And this is DPL's rule verbatim: every remaining key the parser produced
    # that is also a real column gets written to it. Custom fields need no
    # special case -- a column exists and the parser found a value for it, so it
    # is stored, whatever it is called.
    seen_keys = set()
    for r in normalized:
        seen_keys.update((r["raw_data"] or {}).keys())
    extra = sorted(k for k in seen_keys if k in live and k not in derived)

    columns = derived + extra

    missing = {"txn_date", "description", "balance"} - set(derived)
    if missing:
        logger.warning(
            "[import] %s no longer exist on temp_trans; those values stay in "
            "raw_data only", ", ".join(sorted(missing)),
        )

    # raw_data is the only column needing a cast; the rest are passed as native
    # Python values and asyncpg maps them.
    placeholders = ", ".join(
        f"${i}::jsonb" if col == "raw_data" else f"${i}"
        for i, col in enumerate(columns, start=1)
    )

    records = []
    for i, r in enumerate(normalized, start=1):
        raw = r["raw_data"] or {}
        source = {
            "batch_id": batch_id, "row_number": i, "row_hash": r["row_hash"],
            "txn_date": r["txn_date"], "description": r["description"],
            "amount": r["amount"], "credit_debit": r["credit_debit"],
            "balance": r["balance"], "raw_data": json.dumps(raw, default=str),
        }
        record = [source[c] for c in derived]
        # The parser keys its output by fieldname and a field's fieldname IS its
        # column name, so the lookup is direct. Coerced to the column's declared
        # type, exactly as DPL did from live_cols.
        record.extend(_coerce(raw.get(name), live[name]) for name in extra)
        records.append(tuple(record))

    await conn.executemany(
        f'INSERT INTO temp_trans ({", ".join(columns)}) VALUES ({placeholders})',
        records,
    )
    return len(records)


async def count_duplicate_rows(conn, batch_id: int) -> int:
    """How many rows in this batch already appear in an earlier batch.

    Soft signal only. The caller surfaces it so a user re-importing an
    overlapping statement period can see the overlap before finalizing, without
    the import itself being refused.
    """
    return await conn.fetchval(
        """
        SELECT count(*)
        FROM temp_trans t
        WHERE t.batch_id = $1
          AND EXISTS (
              SELECT 1 FROM temp_trans o
              WHERE o.row_hash = t.row_hash AND o.batch_id <> t.batch_id
          )
        """,
        batch_id,
    )


def compute_fill_rates(rows: list) -> dict:
    """Per-field fill rate: how many of the assembled rows have a value for each
    key. Copied from DPL_project -- shared by the PDF and Excel import paths.

    This is the number that tells you a mapping is wrong: a statement where
    'balance' is filled on 3 of 180 rows means the balance column was not
    matched, not that the bank left it blank.
    """
    total_rows = len(rows)
    if not total_rows:
        return {}
    all_keys = set()
    for r in rows:
        all_keys.update(r.keys())
    return {
        key: {"filled": sum(1 for r in rows if r.get(key)), "total": total_rows}
        for key in sorted(all_keys)
    }
