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
import re

from database import company_connection
from import_helpers import count_duplicate_rows, insert_temp_rows

logger = logging.getLogger(__name__)


# ── Company, filled from the bank the account belongs to ────────────────────
#
# A statement row carries the account number it was printed under. bank_master
# knows which company owns that account. So the company of a staged row is not
# something anyone should have to type -- it follows from the account number.
#
# Both columns are custom fields, so their physical names differ per company
# (field_text_17 and field_text_20 in company_028) and are found by matching the
# fieldmap's display names rather than being hardcoded.

def _norm(text: str) -> str:
    """Fold a display name for matching: 'A/C No.' and 'a c no' both -> 'a c no'."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


_ACCOUNT_ALIASES = {
    "account num", "account number", "account no", "acct no", "acct num",
    "a c no", "a c number", "bank account number", "account",
}
_COMPANY_ALIASES = {"company", "company name", "group company"}
# The one free-text column the Imported Rows editor lets people type into. The
# other four editable fields are found through fieldmap.mirrors, which names
# their master table; narration has no master, so it is matched by display name
# the same way the two above are. "acc remarks" and "crm remarks" deliberately
# do not match — they are separate columns this company fills in elsewhere.
_NARRATION_ALIASES = {"narration", "narrations", "remark", "remarks", "note", "notes"}

# A fieldmap row names a physical column and that name is interpolated into the
# UPDATE below -- Postgres has no placeholder for an identifier. The fieldmap is
# server-side data, but a pattern that cannot express anything except a custom
# field is a stronger guarantee than "nobody edits that table by hand".
_CUSTOM_FIELD_RE = re.compile(r"^field_(text|num|date)_\d+$")


async def _resolve_field(conn, aliases: set[str]) -> str | None:
    """The column whose display name matches one of `aliases`, or None."""
    for row in await conn.fetch(
        "SELECT fieldname, displayname FROM fieldmap WHERE is_active = true"
    ):
        if _norm(row["displayname"]) in aliases:
            name = row["fieldname"]
            return name if _CUSTOM_FIELD_RE.match(name or "") else None
    return None


def account_digits(expr: str) -> str:
    """SQL reducing an account number to what every spelling of it shares.

    Non-digits go first, so '0455 6320 0000 264' and '045563200000264' agree.
    Then leading zeros, and that second step is the one that matters: a
    spreadsheet stores an account number as a NUMBER, so the account this
    company keeps in Master Data as '045563200000264' arrives from its own Excel
    export as '45563200000264'. The zero is formatting Excel never kept.

    Comparing on digits alone therefore failed on precisely the accounts that
    were entered carefully -- and failed silently, leaving Company blank with no
    error to explain it. Two accounts differing only by leading zeros are the
    same account; no bank issues both.

    Used by the Company fill and by the Account Number filter, from here, so the
    two can never disagree about whether a row belongs to an account.
    """
    return f"ltrim(regexp_replace(coalesce({expr}, ''), '\\D', '', 'g'), '0')"


def normalise_account(value: str) -> str:
    """account_digits, in Python. The same rule, applied to a filter value.

    Kept beside the SQL version rather than derived from it, because the two run
    on different sides of the wire and the only thing that keeps them honest is
    sitting next to each other: digits only, then leading zeros.
    """
    return "".join(ch for ch in (value or "") if ch.isdigit()).lstrip("0")


async def account_column(conn) -> str | None:
    """The column holding the account number a row was printed under, or None.

    Public because the Account Number filter on the staging and ledger screens
    has to mean the same column this module fills Company from. Two independent
    answers to "which column is the account number" would drift the first time
    a company renamed the field, and the symptom would be a filter that silently
    matches nothing.
    """
    return await _resolve_field(conn, _ACCOUNT_ALIASES)


async def company_column(conn) -> str | None:
    """The column this module writes the owning company into, or None.

    None is the normal answer for a company that has not added the field --
    company_001 has an account number column and no Company column -- so callers
    have to handle it rather than assume the column exists.
    """
    return await _resolve_field(conn, _COMPANY_ALIASES)


async def narration_column(conn) -> str | None:
    """The free-text column the row editor lets people type into, or None."""
    return await _resolve_field(conn, _NARRATION_ALIASES)


# ── Financial year, derived from the row's own date ──────────────────────────

_FY_ALIASES = {"fy", "financial year", "fin year", "f y", "fiscal year"}


def financial_year_expr(date_expr: str) -> str:
    """SQL turning a date into 'FY 25-26'.

    The Indian financial year runs 1 April to 31 March, so the label needs the
    year it started and the year it ends. Shifting the date back three months
    lands on the first, forward nine months on the second: 2026-03-31 gives 2025
    and 2026, and the next day gives 2026 and 2027.

    Arithmetic rather than a CASE on the month, which means it is right for
    every year without being told about any of them -- including across a
    century, where 2000-03-31 is FY 99-00.
    """
    return (f"'FY ' || to_char({date_expr} - interval '3 months', 'YY') || '-' || "
            f"to_char({date_expr} + interval '9 months', 'YY')")


async def financial_year_column(conn) -> str | None:
    """The column holding the financial year label, or None."""
    return await _resolve_field(conn, _FY_ALIASES)


async def fill_financial_year(conn, *, table: str = "temp_trans",
                              batch_id: int | None = None) -> dict:
    """Set each row's FY from the date it carries.

    Derived on write rather than computed on read, so the column can be sorted,
    filtered and exported like any other -- which is the whole reason it is a
    column and not a formatting rule in the browser.

    Only writes where the value would change, so it is safe to run repeatedly.
    Rows with no date are left alone: there is no year to derive and blanking
    the cell would lose a value someone may have put there by hand.
    """
    from services import custom_fields

    fy_col = await financial_year_column(conn)
    date_col = await custom_fields.date_column(conn)

    if not fy_col or not date_col:
        missing = []
        if not fy_col:
            missing.append("an FY column")
        if not date_col:
            missing.append("a date column")
        logger.info("[FY fill] skipped on %s: fieldmap has no %s",
                    table, " and no ".join(missing))
        return {"updated": 0, "skipped": True,
                "reason": f"This company's fieldmap has no {' and no '.join(missing)}."}

    params: list = []
    where_batch = ""
    if batch_id is not None:
        params.append(batch_id)
        where_batch = f" AND t.batch_id = ${len(params)}"

    expr = financial_year_expr(f"t.{date_col}")
    tag = await conn.execute(
        f"""
        UPDATE {table} t
           SET {fy_col} = {expr}
         WHERE t.{date_col} IS NOT NULL
           AND t.{fy_col} IS DISTINCT FROM ({expr})
           {where_batch}
        """,
        *params,
    )
    updated = int(tag.split()[-1])

    undated = await conn.fetchval(
        f"SELECT count(*) FROM {table} t WHERE t.{date_col} IS NULL"
    )

    logger.info("[FY fill] %s: %d rows set, %d rows have no date",
                table, updated, undated)
    return {
        "updated": updated,
        "skipped": False,
        "fy_column": fy_col,
        "date_column": date_col,
        # The only reason a row can be left without an FY.
        "undated_rows": undated,
    }


async def fill_company_from_bank(conn, *, table: str = "temp_trans",
                                 batch_id: int | None = None) -> dict:
    """Set each row's Company from the bank that owns its account number.

    Matched on digits only, so '045563200000264', '0455 6320 0000 264' and a
    trailing space all agree. Deliberately not a "last four digits" match: two
    banks' accounts ending 2195 are not the same account, and a wrong company on
    a financial row is worse than a blank one.

    batch_id=None covers every row, which is what makes this a backfill: adding
    a bank account tomorrow fills in the rows imported before it existed. Rows
    whose account matches nothing are left alone rather than blanked -- a
    company set by hand should survive a bank being renamed.

    Returns what it did, including why it did nothing, because "0 rows" has
    several causes and they need different fixes.
    """
    account_col = await _resolve_field(conn, _ACCOUNT_ALIASES)
    company_col = await _resolve_field(conn, _COMPANY_ALIASES)

    if not account_col or not company_col:
        missing = []
        if not account_col:
            missing.append("an account number column")
        if not company_col:
            missing.append("a Company column")
        logger.info("[company fill] skipped on %s: fieldmap has no %s",
                    table, " and no ".join(missing))
        return {"updated": 0, "skipped": True,
                "reason": f"This company's fieldmap has no {' and no '.join(missing)}."}

    params: list = []
    where_batch = ""
    if batch_id is not None:
        params.append(batch_id)
        where_batch = f" AND t.batch_id = ${len(params)}"

    bank_acct = account_digits("b.account_number")
    row_acct = account_digits(f"t.{account_col}")

    tag = await conn.execute(
        f"""
        UPDATE {table} t
           SET {company_col} = b.company
          FROM bank_master b
         WHERE b.company IS NOT NULL
           AND b.account_number IS NOT NULL
           AND t.{account_col} IS NOT NULL
           AND {bank_acct} <> ''
           AND {bank_acct} = {row_acct}
           AND t.{company_col} IS DISTINCT FROM b.company
           {where_batch}
        """,
        *params,
    )
    updated = int(tag.split()[-1])

    # Counted so the caller can say "0 updated, and here is why": no bank
    # carries the account these rows were printed under.
    unmatched = await conn.fetchval(
        f"""
        SELECT count(DISTINCT t.{account_col}) FROM {table} t
         WHERE t.{account_col} IS NOT NULL
           AND NOT EXISTS (
               SELECT 1 FROM bank_master b
                WHERE b.account_number IS NOT NULL
                  AND {bank_acct} <> ''
                  AND {bank_acct} = {row_acct}
           )
        """
    )

    logger.info("[company fill] %s: %d rows set, %d account numbers match no bank",
                table, updated, unmatched)
    return {
        "updated": updated,
        "skipped": False,
        "account_column": account_col,
        "company_column": company_col,
        "unmatched_accounts": unmatched,
    }


async def fill_account_from_bank(conn, *, bank_id: int, table: str = "temp_trans",
                                 batch_id: int | None = None) -> dict:
    """Set a row's account number from the bank picked on the import screen.

    Only for rows the statement itself left blank there -- a bank statement
    that never prints its own account number on each line is common enough
    (see 'CR Coll' style sheets), and until now those rows simply had no
    account number for fill_company_from_bank or the Account Number filter to
    match on, even though the user had just told the import screen exactly
    which account this file was.

    Scoped to batch_id, not a backfill like the company/FY fills above: the
    bank picked on this screen describes only the file being staged right now,
    not every row ever staged without one.
    """
    account_col = await _resolve_field(conn, _ACCOUNT_ALIASES)
    if not account_col:
        logger.info("[account fill] skipped on %s: fieldmap has no account number column", table)
        return {"updated": 0, "skipped": True,
                "reason": "This company's fieldmap has no account number column."}

    bank_acct = await conn.fetchval(
        "SELECT account_number FROM bank_master WHERE id = $1", bank_id)
    if not bank_acct:
        logger.info("[account fill] skipped on %s: bank %s has no account number", table, bank_id)
        return {"updated": 0, "skipped": True,
                "reason": "The chosen bank has no account number in Master Data."}

    params: list = [bank_acct]
    where_batch = ""
    if batch_id is not None:
        params.append(batch_id)
        where_batch = f" AND batch_id = ${len(params)}"

    tag = await conn.execute(
        f"""
        UPDATE {table}
           SET {account_col} = $1
         WHERE (btrim(coalesce({account_col}, '')) = '')
           {where_batch}
        """,
        *params,
    )
    updated = int(tag.split()[-1])

    logger.info("[account fill] %s: %d rows set from bank %s", table, updated, bank_id)
    return {"updated": updated, "skipped": False, "account_column": account_col}


async def _bank_check(conn, bank_id: int | None) -> None:
    """Raise unless bank_id names a row in this company's bank_master.

    The message names what IS available, because the id is chosen from a
    dropdown the caller may be out of step with — and because the most common
    case, a company that has not entered its banks yet, is not a mistyped id at
    all and needs a different instruction.
    """
    if bank_id is None:
        return
    if await conn.fetchval(
        "SELECT 1 FROM bank_master WHERE id = $1 AND is_active = true", bank_id
    ):
        return

    available = await conn.fetch(
        "SELECT id, bank_name FROM bank_master WHERE is_active = true ORDER BY id"
    )
    if available:
        options = ", ".join(f"{r['id']} ({r['bank_name']})" for r in available)
        raise RuntimeError(
            f"bank_id {bank_id} does not exist or is deactivated. Available: {options}."
        )
    raise RuntimeError(
        f"bank_id {bank_id} does not exist: this company has no active banks yet. "
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

        # A row whose statement never printed its own account number gets it
        # from the bank picked on the import screen -- before the company fill
        # below, so that fill can then match on it too. Only runs when a bank
        # was actually chosen; "Not specified" leaves every row exactly as the
        # statement wrote it.
        #
        # Not fatal, same as the two fills after it: a company whose fieldmap
        # has no account number column still gets its statement staged.
        acct_filled = {"updated": 0}
        if bank_id is not None:
            try:
                acct_filled = await fill_account_from_bank(
                    conn, bank_id=bank_id, batch_id=batch_id)
            except Exception:
                logger.exception("[stage] account fill failed for batch %s", batch_id)

        # Company follows from the account number the statement was printed
        # under, so it is filled here rather than left for someone to type on
        # every row. Scoped to this batch: the rest of the table has already had
        # its turn, and re-checking 500 rows on every import is work for nothing.
        #
        # Not fatal. A company whose bank list is incomplete still gets its
        # statement staged, with the column blank and the backfill available
        # once the account is added.
        try:
            filled = await fill_company_from_bank(conn, batch_id=batch_id)
        except Exception:
            logger.exception("[stage] company fill failed for batch %s", batch_id)
            filled = {"updated": 0}

        # The financial year follows from the date the same way the company
        # follows from the account number, and is filled here for the same
        # reason: it is knowable, so nobody should have to type it. Also not
        # fatal — a statement still stages with the column blank.
        try:
            fy = await fill_financial_year(conn, batch_id=batch_id)
        except Exception:
            logger.exception("[stage] FY fill failed for batch %s", batch_id)
            fy = {"updated": 0}

    logger.info("[stage] batch %s: %d rows, %d duplicate rows, %d account set, "
                "%d company set, %d FY set",
                batch_id, inserted, duplicates, acct_filled.get("updated", 0),
                filled.get("updated", 0), fy.get("updated", 0))
    return {
        "batch_id": batch_id,
        "inserted": inserted,
        "duplicate_rows": duplicates,
        "account_number_filled": acct_filled.get("updated", 0),
        "company_filled": filled.get("updated", 0),
        "financial_year_filled": fy.get("updated", 0),
    }
