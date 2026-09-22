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
import random
import re
import string
from datetime import date as _date
from decimal import Decimal, InvalidOperation

from parsers import (_build_alias_map, _category_map_from_aliases,
                     _category_of, _normalize_for_matching)
from services.custom_fields import date_column, description_column, table_structure

logger = logging.getLogger(__name__)


# Some banks print one Amount column and a DR/CR marker instead of separate
# Debit and Credit columns (Axis does). These two vocabularies recognise that
# pair from the fieldmap — by displayname or alias, normalized exactly the way
# alias matching normalizes, so 'Amount(INR)' and 'amount inr' both land.
#
# They live HERE and not in parsers._CATEGORY_VOCABULARY on purpose. That
# vocabulary decides semantic roles inside the parser (chain scoring, doc-field
# gating) and is byte-shared with DPL; these two categories exist only for this
# module's split below, and matching them here — and only for fieldmap rows the
# parser left uncategorised — means no existing column can lose its real role
# to them.
_AMOUNT_TERMS = {"amount", "amountinr", "amount inr", "transaction amount",
                 "txn amount", "amt"}
_DRCR_TERMS = {"drcr", "dr cr", "debitcredit", "debit credit",
               "crdr", "cr dr", "creditdebit", "credit debit"}


def _marker_direction(val) -> str | None:
    """'DR'/'CR' from a direction-marker cell, or None when it says neither.

    Letters only, so 'Dr.', ' CR ' and 'debit' all resolve; anything else —
    blank, a number, some third word — is None and the row is left exactly as
    the two-column path would have left it.
    """
    letters = re.sub(r"[^A-Za-z]", "", str(val or "")).upper()
    if letters in ("DR", "D", "DEBIT", "WITHDRAWAL"):
        return "DR"
    if letters in ("CR", "C", "CREDIT", "DEPOSIT"):
        return "CR"
    return None


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

    # Second pass, only over rows the parser's own vocabulary left without a
    # role: a single-Amount column and a DR/CR marker column, recognised so
    # normalize_parsed_rows can split them into withdrawal/deposits. Scoped to
    # uncategorised rows so no column can ever lose its real role to this.
    for row in (fieldmap_rows or []):
        fieldname = row.get("fieldname") or ""
        if not fieldname or _category_of(fieldname, cat_by_field):
            continue
        terms = {_normalize_for_matching(row.get("displayname") or "")}
        terms.update(_normalize_for_matching(a)
                     for a in (row.get("mapfields") or "").split(","))
        if "amount" not in by_cat and terms & _AMOUNT_TERMS:
            by_cat["amount"] = fieldname
        elif "drcr" not in by_cat and terms & _DRCR_TERMS:
            by_cat["drcr"] = fieldname
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


def row_content_hash(txn_date, description, amount, credit_debit,
                     bank_id=None, reference_no=None) -> str:
    """Content fingerprint of one statement line, independent of which file it
    came from.

    Used for the soft duplicate warning: the same line appearing in two
    different statements (overlapping periods) is worth flagging but must not
    block the import, which is why 002 dropped the UNIQUE constraint that used
    to sit on this value. The hard block against re-uploading an identical file
    is import_batches.file_hash.

    bank_id scopes the fingerprint to one account (confirmed with the user):
    without it, a same-date/same-amount/same-description transaction on two
    DIFFERENT bank accounts read as duplicates of each other, which they are
    not. reference_no (a bank's own UTR/cheque/reference number, when the
    fieldmap has one mapped) replaces the description entirely rather than
    supplementing it, also confirmed with the user -- it is the one field a
    bank guarantees is unique per transaction, where the printed description
    is free text that can legitimately repeat (two rent payments, same
    tenant, same amount, different months, worded identically).
    """
    reference = (reference_no or "").strip()
    if reference:
        parts = [
            str(bank_id) if bank_id is not None else "",
            txn_date.isoformat() if txn_date else "",
            reference.upper(),
            str(amount) if amount is not None else "",
            credit_debit or "",
        ]
    else:
        parts = [
            str(bank_id) if bank_id is not None else "",
            txn_date.isoformat() if txn_date else "",
            (description or "").strip().lower(),
            str(amount) if amount is not None else "",
            credit_debit or "",
        ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def normalize_parsed_rows(rows: list, fieldmap_rows: list, bank_id=None) -> tuple[list, dict]:
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
    # Set only when the fieldmap maps a single-Amount column and a DR/CR
    # marker column — the statement shape where debit and credit share one
    # printed column and a flag says which one each row is.
    f_amt = by_cat.get("amount")
    f_dir = by_cat.get("drcr")
    # A bank's own UTR/cheque/reference number, when the fieldmap has one
    # mapped -- see row_content_hash for why this replaces description in
    # the fingerprint rather than joining it.
    f_ref = by_cat.get("reference_no")

    normalized = []
    skipped_no_amount = 0
    skipped_no_date = 0
    ambiguous = 0
    split_rows = 0

    for raw in rows:
        txn_date = _parse_date_to_date(raw.get(f_date))
        withdrawal = _to_amount(raw.get(f_out))
        deposits = _to_amount(raw.get(f_in))

        # The single-amount split. Fires per row, and only as a fallback: a
        # row that already carries a value in a real debit or credit column is
        # left entirely alone, so statements with two amount columns cannot be
        # touched by this even in a fieldmap that also maps Amount and DR/CR.
        if withdrawal is None and deposits is None and f_amt and f_dir:
            amt = _to_amount(raw.get(f_amt))
            direction = _marker_direction(raw.get(f_dir))
            if amt is not None and amt != 0 and direction:
                if direction == "DR":
                    withdrawal = amt
                else:
                    deposits = amt
                # Mirror the value under the mapped debit/credit fieldname so
                # the staging table's own columns fill, exactly as if the bank
                # had printed two columns. Written next to the original
                # Amount and marker values, never over anything — raw_data
                # keeps all of them.
                target = f_out if direction == "DR" else f_in
                if target not in raw or raw.get(target) in (None, ""):
                    raw[target] = raw.get(f_amt)
                split_rows += 1

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
        if txn_date is None:
            skipped_no_date += 1

        # A row missing an amount or a date is still staged. DPL's rule, from
        # append_rows_to_master:
        #
        #     if any(v is not None for v in row_vals):
        #
        # keep the row when it carries any value at all, and let the person
        # reviewing it decide. This used to `continue` on either condition,
        # which is where "No usable transaction rows were found" came from: a
        # fieldmap whose amount column was claimed by the wrong field left every
        # row amountless, so every row was dropped and the import reported
        # nothing usable — with no way to see the rows it had actually read.
        #
        # The counts above are kept as statistics. They are worth surfacing;
        # they were never worth discarding data over.
        if not any(v is not None for v in (txn_date, withdrawal, deposits,
                                           raw.get(f_desc), raw.get(f_bal))) \
                and not any(v not in (None, "") for v in (raw or {}).values()):
            continue

        amount = withdrawal if withdrawal is not None else deposits
        # No amount means no direction to report. Guessing "CR" here would post
        # a blank line as a credit.
        credit_debit = None if amount is None else ("DR" if withdrawal is not None else "CR")

        description = (raw.get(f_desc) or "").strip() or None
        reference_no = (raw.get(f_ref) or "").strip() if f_ref else ""

        # Keys here are temp_trans column names, which are fixed -- unlike the
        # keys read out of `raw` above, which are whatever the fieldmap says.
        normalized.append({
            "txn_date": txn_date,
            "description": description,
            "amount": amount,
            "credit_debit": credit_debit,
            "balance": _to_amount(raw.get(f_bal)),
            "txn_ft": row_content_hash(txn_date, description, amount, credit_debit,
                                       bank_id=bank_id, reference_no=reference_no),
            "raw_data": raw,
        })

    stats = {
        "parsed": len(rows),
        "usable": len(normalized),
        "skipped_no_amount": skipped_no_amount,
        "skipped_no_date": skipped_no_date,
        "ambiguous_both_columns": ambiguous,
        # Rows whose amount arrived as one column plus a DR/CR marker and was
        # routed into debit or credit accordingly. Zero on two-column banks.
        "amount_split_by_drcr": split_rows,
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


# The one custom field this module knows to auto-fill rather than leave to
# the usual "found it in raw_data" rule below -- confirmed with the user.
# Looked up by display name rather than a hardcoded field_text_N: the number
# depends on creation order and can differ per company schema, and the field
# could be deleted, in which case import behaves exactly as it did before it
# existed.
_EXPORT_UID_FIELD_NAME = "Export UID"
_EXPORT_STATUS_FIELD_NAME = "Export Status"
_EXPORT_STATUS_DEFAULT = "No"


def _generate_export_uid() -> str:
    """"EXFV" + one random uppercase letter + 11 random digits, e.g.
    "EXFVE62590067860" -- confirmed with the user as a UTR-style id,
    generated once per transaction at import time. Purely random, not
    derived from the row's own bank UTR/date/anything else, so two
    unrelated transactions can never look connected by a shared id.
    """
    letter = random.choice(string.ascii_uppercase)
    digits = "".join(random.choices(string.digits, k=11))
    return f"EXFV{letter}{digits}"


async def _column_by_display_name(conn, live: dict, displayname: str) -> str | None:
    """The real column backing a custom field found by display name, if it
    still exists, is active, and its column wasn't dropped straight from
    Postgres without deleting the fieldmap row too.
    """
    row = await conn.fetchrow(
        "SELECT fieldname FROM fieldmap WHERE displayname = $1 AND is_active", displayname,
    )
    if row is None or row["fieldname"] not in live:
        return None
    return row["fieldname"]


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
    derived = [c for c in ("batch_id", "row_number", "txn_ft", "txn_date",
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

    # Neither "Export UID" nor "Export Status" has a header alias on an
    # ordinary bank statement -- but re-importing a file that was itself
    # exported earlier (its header row literally reads "Export UID" /
    # "Export Status") DOES match one by display name, landing it in `extra`
    # already. In that case the column must not be appended a second time
    # here, and -- the part that was missed -- the per-row loop below must
    # not append a SECOND value for it either, or every record ends up one
    # (or two) items longer than `columns`, exactly the "N+1 arguments"
    # asyncpg error this produced on such a file.
    export_uid_col = await _column_by_display_name(conn, live, _EXPORT_UID_FIELD_NAME)
    export_uid_needs_fill = bool(export_uid_col) and export_uid_col not in columns
    if export_uid_needs_fill:
        columns = columns + [export_uid_col]
    export_status_col = await _column_by_display_name(conn, live, _EXPORT_STATUS_FIELD_NAME)
    # A row whose own "Export Status" cell already reads "Yes" (the
    # re-imported-export case above) must keep that value -- but a BLANK
    # cell in that same column, on that same file, still gets the ordinary
    # "No" default rather than staying empty. So this column needs the
    # per-row blank-to-"No" fallback below whether or not it was already in
    # `extra` -- the two only differ in whether a NEW column has to be added.
    export_status_in_extra = bool(export_status_col) and export_status_col in columns
    export_status_needs_fill = bool(export_status_col) and not export_status_in_extra
    if export_status_needs_fill:
        columns = columns + [export_status_col]

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
    seen_uids = set()
    for i, r in enumerate(normalized, start=1):
        raw = r["raw_data"] or {}
        source = {
            "batch_id": batch_id, "row_number": i, "txn_ft": r["txn_ft"],
            "txn_date": r["txn_date"], "description": r["description"],
            "amount": r["amount"], "credit_debit": r["credit_debit"],
            "balance": r["balance"], "raw_data": json.dumps(raw, default=str),
        }
        record = [source[c] for c in derived]
        # The parser keys its output by fieldname and a field's fieldname IS its
        # column name, so the lookup is direct. Coerced to the column's declared
        # type, exactly as DPL did from live_cols. Export Status gets one
        # extra step here: a blank cell in a re-imported export's own status
        # column falls back to "No", same as a fresh statement that never had
        # the column at all -- only a real "Yes" (or any other non-blank
        # value already there) is left untouched.
        def _extra_value(name):
            value = _coerce(raw.get(name), live[name])
            if name == export_status_col and export_status_in_extra:
                return value or _EXPORT_STATUS_DEFAULT
            return value
        record.extend(_extra_value(name) for name in extra)
        if export_uid_needs_fill:
            uid = _generate_export_uid()
            while uid in seen_uids:            # astronomically rare; guards
                uid = _generate_export_uid()   # only against this one batch
            seen_uids.add(uid)
            record.append(uid)
        if export_status_needs_fill:
            record.append(_EXPORT_STATUS_DEFAULT)
        records.append(tuple(record))

    await conn.executemany(
        f'INSERT INTO temp_trans ({", ".join(columns)}) VALUES ({placeholders})',
        records,
    )
    return len(records)


async def find_duplicate_rows(conn, batch_id: int) -> list[dict]:
    """Which of this batch's own rows already appear in an earlier batch, with
    enough detail (id, date, description, amount, direction) for a person to
    look at each one and decide whether to keep or discard it.

    date/description are NOT fixed column names on temp_trans -- unlike
    amount/credit_debit, they live wherever this company's own fieldmap
    mapped them (a field_date_N / field_text_N column, or none at all), the
    same reason routers/transactions.py's _judged_rows resolves them through
    custom_fields.date_column/description_column rather than naming a
    column directly. Doing the same here is what keeps this from crashing on
    any company whose date/description fields aren't literally called that.

    Soft signal only -- nothing here blocks or skips an insert. The caller
    surfaces this so a user re-importing an overlapping statement period can
    see the overlap and act on it (see routers/imports.py's single-file
    import flow, which lets that person delete the ones they don't want via
    the ordinary per-row delete endpoint) without the import itself ever
    being refused.
    """
    date_col = await date_column(conn)
    desc_col = await description_column(conn)
    date_sel = f"t.{date_col} AS txn_date" if date_col else "NULL AS txn_date"
    desc_sel = f"t.{desc_col} AS description" if desc_col else "NULL AS description"

    rows = await conn.fetch(
        f"""
        SELECT t.id, {date_sel}, {desc_sel}, t.amount, t.credit_debit
        FROM temp_trans t
        WHERE t.batch_id = $1
          AND EXISTS (
              SELECT 1 FROM temp_trans o
              WHERE o.txn_ft = t.txn_ft AND o.batch_id <> t.batch_id
          )
        ORDER BY t.row_number
        """,
        batch_id,
    )
    return [dict(r) for r in rows]


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
