"""Build the Farvision export sheet from temp_trans rows.

One row in, one row out: every temp_trans row the caller's filter matches
becomes exactly one row here, in the fixed Farvision column order. Nothing
here decides which rows are included -- that is the caller's WHERE, built the
same way the Imported Rows table itself is (routers.transactions._temp_filters)
so "what you're looking at" and "what you export" can never disagree.

Field mapping is company_028 (DPL)'s own fieldmap, given directly rather than
resolved through custom_fields' label lookup: this feature is built for one
company's Farvision layout, not a generic one, and the PDF spec named these
physical columns already.

The row's head (used for Document Type, EntryTypes, Deduction Type and
Description) prefers the real head_master/rera_head_master/idw_head_master
join when head_id/rera_head_id/idw_head_id is set, but falls back to
field_text_5 (fieldmap label "HEAD") when none is -- confirmed against real
data where a batch had a HEAD value on every row ("Internal", "Professional",
"Salary HO", ...) but none of the three head_id columns actually set. Always
check the head field present in temp_trans, not just the resolved head_id.

Account Head and Parent Account Head come from farvision_account_master_dpl or
_amb, one table per company, matched against the row's own Company -- DPL and
AMB share this company_028 schema and each have their own ledger names, so
matching against the wrong table could pick the wrong company's party of the
same name. A row whose Company doesn't resolve to either table gets no match
at all, rather than guessing.

The search text is temp_trans's own DESC field (field_text_1) -- the raw
bank-statement text (e.g. "YIB-NEFT-YESME62170041087-INDIA PRIDE COM-
CNRB0001565-VENDOR- CANARA BANK"), confirmed against the user's own
screenshot of it. NARRATION (field_text_11), used here previously, turned
out to be a synthetic field, often just the literal placeholder text
"Remarks Compulsory For Narration" rather than real bank text -- confirmed
live. DESC has no "To:"/"Purpose:" structure to isolate a party segment
from, so it is searched whole. Both the search text and every candidate
Account Head are normalized first (corporate suffixes LIMITED/PRIVATE folded
to LTD/PVT, a trailing plural S dropped) so "Gamut Infosystem Ltd" matches
DESC text reading "GAMUT INFOSYSTEMS LIMITED" -- confirmed against that
exact case. Parent Account Head and Payee Name simply come along with
whichever Account Head matched. No match found means both stay blank rather
than guessed.

An Internal-head row (Head is "Internal") skips the Account Head master
entirely -- it is a transfer between the company's own bank accounts, not a
payment to any party, and whole-DESC matching against the Account Head
master started false-matching the company's own legal name in transfer
routing text ("...vide YIB-TPT-DWARKADHIS PROJECTS PRIVATE LIMITED...")
against an unrelated Account Head master row of the same name. Account Head
for these rows is instead the Farvision Bank Name on the other side of the
transfer -- see _match_internal_account_head.

Only Account Head and Parent Account Head are genuinely tied to a specific
row in that master -- confirmed with the user. Everything else the original
sheet carried (Financial Year, Document Type, Deduction Type, Description,
EntryTypes, Debit/Credit, Payment Mode, Docno, Invoice No, Business Unit) was
near-empty across the real data and isn't stored in the master at all; it
lives here as REFERENCE_VALUES, a plain lookup of the format/examples found
in that original sheet, shown to the user as read-only reference material
rather than joined into the export automatically.

BankName is the one exception treated specially: bank_master's own bank_name
is a short generic label ("BOM", "YES", "KVB") shared by several accounts,
but farvision_bank_name_master holds Farvision's own full bank-name strings,
each of which embeds one account's actual number. The row's account number
(resolved the same way as the temp_trans-derived bank_name lookup already
was) is matched against those account numbers to fill BankName with the
exact Farvision text; a row whose account number matches nothing there falls
back to the old temp_trans-derived value.

The same real Account Head sometimes got typed twice with different
formatting ("India Pride Com" vs "INDIA PRIDE.COM") -- confirmed live across
~16,000 rows, hundreds of such pairs exist. Rather than guessing which
spelling is "right", a match that lands on one of these is left ambiguous
(see _duplicate_options_map / _match_internal_account_head's own dropdown
cases). Resolving it no longer happens in Excel -- confirmed with the user
after an in-Excel dropdown proved easy to miss -- but on a "Farvision
Verify" page in the app (routers.transactions's /farvision-verify
endpoints), which writes the chosen text back onto that temp_trans row
(farvision_account_head_override / farvision_parent_account_head_override,
company/037_farvision_account_master.sql's sibling migration 044). Once set,
an override always wins over re-matching -- see fetch_rows.
"""
import asyncio
import decimal
import re
import time

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from io import BytesIO

from services import staging

# Candidate lookups (_bank_name_candidates, _bank_short_codes,
# _account_head_candidates) are read-only reference data that rarely changes
# but was being re-queried from scratch on every single Verify/export
# request -- two clicks a few seconds apart re-ran the same four queries.
# This is a plain in-process dict, the same shape as services.jobs' registry:
# one web process serves this app, the key set is tiny and bounded (one
# company's worth of bank names/codes, and one Account Head list per
# company -- a handful of keys total, never one per row or per filter), so
# there's nothing here a dict can't hold. Keyed by (schema, kind[, company])
# rather than by company alone, since two companies' schemas could otherwise
# collide on the same bank_master/farvision_bank_name_master data.
#
# The real defense against stale data is invalidate_cache() below, called
# from every write path in routers.master -- a save is visible on the very
# next request regardless of this TTL. This is only the fallback for a write
# that reaches these tables some other way (a direct SQL edit, a future write
# path that forgets to invalidate), so it can be generous: an hour, not a
# minute, confirmed with the user as an acceptable worst-case staleness
# window given how rarely this data changes.
_CACHE_TTL_SECONDS = 3600
_candidate_cache: dict[tuple, tuple[float, object]] = {}


async def _cached(key: tuple, fetch_fn):
    now = time.monotonic()
    hit = _candidate_cache.get(key)
    if hit is not None and now - hit[0] < _CACHE_TTL_SECONDS:
        return hit[1]
    value = await fetch_fn()
    _candidate_cache[key] = (now, value)
    return value


def invalidate_cache(schema: str) -> None:
    """Drop every cached candidate list for one company's schema.

    Called after a write to any table these lookups read from
    (farvision_bank_name_master, bank_master, farvision_account_master_dpl/
    amb) -- see routers.master's generic CRUD router -- so a save is visible
    on the very next request instead of waiting out the TTL.
    """
    for key in [k for k in _candidate_cache if k[0] == schema]:
        _candidate_cache.pop(key, None)

# The real Farvision workbook is 6 sheets, not one flat one -- confirmed with
# the user from a screenshot of the real tab bar (ReceiptPayment,
# ReceiptPaymentDetail, LedgerDetails, ImportTaxInfo, AdjustmentDetails,
# Info). Info is deferred -- its columns haven't been given yet -- so only
# the first 5 are built here. Link Ref Code / Detail Link Ref Code are the
# join keys tying one logical row's sheets back together; both are just the
# row's own sequence number (see fetch_rows), so every sheet's Nth data row
# is the same source row. A handful of fields (Business Unit, Document Type,
# Narration) legitimately appear on more than one sheet -- confirmed against
# the user's own column lists for each sheet, not an accidental duplicate.
SHEETS: dict[str, list[str]] = {
    "ReceiptPayment": [
        "Link Ref Code", "Business Unit", "Financial Year", "Document Type",
        "Document Date", "Document No", "Narration", "BankName", "EntryTypes",
    ],
    "ReceiptPaymentDetail": ["Link Ref Code", "Detail Link Ref Code"],
    "LedgerDetails": [
        "Link Ref Code", "Detail Link Ref Code", "Business Unit", "Document Type",
        "Debit/Credit", "Account Head", "Parent Account Head", "Debit Amount",
        "Credit Amount", "Narration", "Payment Mode", "Cheque No", "Cheque Date",
        "Cheque Type", "Payee Name", "Beneficiary", "Card Type", "Print Cheque",
        "Sub Project", "Budget", "Zone", "Department", "Order", "Milestone",
        "Tower", "Segment", "Employee", "Employee Name", "Department Name",
        "Cost Center", "Purpose Of Payment",
    ],
    "ImportTaxInfo": ["Link Ref Code", "Detail Link Ref Code", "Deduction Type", "Description"],
    "AdjustmentDetails": [
        "Link Ref Code", "Detail Link Ref Code", "Docno", "Date", "Invoice No",
        "Invoice Date", "Bill Amount", "Balance Amount", "Adjustment Amount",
    ],
}

# The Deposit Withdrawal export -- a separate workbook, not another tab in
# the Receipt Payment one, confirmed with the user: same underlying rows,
# but only the ones whose Document Type is "Deposit/withdrawal" (Internal
# transfers, see filter_deposit_withdrawal), with their own 3-sheet shape and
# some of their own column headers (DepositWithdrawal Business Unit /
# DepositWithdrawal Narration) even though the value behind them is exactly
# the same Business Unit / Narration a Receipt Payment row would show --
# _COLUMN_ALIASES is what makes a renamed header still pull the right field.
DW_SHEETS: dict[str, list[str]] = {
    "DepositWithdrawal": [
        "Link Ref Code", "DepositWithdrawal Business Unit",
        "DepositWithdrawal Narration", "Financial Year", "Document Type",
        "Document Date", "Document No", "BankName", "EntryTypes",
    ],
    "DepositWithdrawalDetails": ["Link Ref Code"],
    "LedgerDetails": [
        "Link Ref Code", "Debit/Credit", "Account Head", "Parent Account Head",
        "Debit Amount", "Credit Amount", "Payment Mode", "Cheque No",
        "Cheque Date", "Cheque Type", "Payee Name", "Card Type", "Narration",
        "Print Cheque",
    ],
}

# Sheet-header text -> the row field it actually reads, for the handful of
# Deposit Withdrawal headers that are relabeled rather than renamed data.
# Anything not listed here reads its own name, in both workbooks.
_COLUMN_ALIASES = {
    "DepositWithdrawal Business Unit": "Business Unit",
    "DepositWithdrawal Narration": "Narration",
}

# The Receipt Payment workbook's 6th tab: a flat, static reference table
# (Sheet Name, Column Name, Property(ies)) describing every column across the
# other 5 sheets, given verbatim by the user from the real Farvision Info
# sheet -- confirmed as documentation only, not something the export checks
# rows against (a follow-up question about enforcing these rules was
# dismissed twice; the only instruction given was to build this exact
# 3-column shape). Not derived from SHEETS -- the real sheet's own row order
# doesn't match this file's, and a handful of columns here (BankName,
# EntryTypes, ...) carry no properties at all.
INFO_SHEET_ROWS: list[tuple[str, str, str]] = [
    ("ReceiptPayment", "Link Ref Code", "Is Required : True"),
    ("ReceiptPayment", "Business Unit", "Is Required : False;Min Length : 10;Max Length : 500"),
    ("ReceiptPayment", "Financial Year", "Is Required : True;Min Length : 1;Max Length : 30"),
    ("ReceiptPayment", "Document Type", "Is Required : True;Min Length : 1;Max Length : 30"),
    ("ReceiptPayment", "Document Date", "Is Required : True;Min Length : 1;Max Length : 30"),
    ("ReceiptPayment", "Document No", "Is Required : True;Min Length : 1;Max Length : 30"),
    ("ReceiptPayment", "Narration", "Is Required : False;Min Length : 0;Max Length : 500"),
    ("ReceiptPayment", "BankName", ""),
    ("ReceiptPayment", "EntryTypes", ""),
    ("ReceiptPaymentDetail", "Link Ref Code", "Is Required : True"),
    ("ReceiptPaymentDetail", "Detail Link Ref Code", "Is Required : True"),
    ("LedgerDetails", "Link Ref Code", "Is Required : True"),
    ("LedgerDetails", "Detail Link Ref Code", "Is Required : True"),
    ("LedgerDetails", "Business Unit", "Is Required : False;Min Length : 10;Max Length : 500"),
    ("LedgerDetails", "Document Type", "Is Required : True;Min Length : 1;Max Length : 30"),
    ("LedgerDetails", "Debit/Credit", "Is Required : True"),
    ("LedgerDetails", "Account Head", "Is Required : True"),
    ("LedgerDetails", "Parent Account Head", "Is Required : True"),
    ("LedgerDetails", "Debit Amount", "Is Required : False"),
    ("LedgerDetails", "Credit Amount", "Is Required : False"),
    ("LedgerDetails", "Narration", ""),
    ("LedgerDetails", "Payment Mode", "Is Required : True"),
    ("LedgerDetails", "Cheque No", ""),
    ("LedgerDetails", "Cheque Date", ""),
    ("LedgerDetails", "Cheque Type", ""),
    ("LedgerDetails", "Payee Name", "Is Required : True"),
    ("LedgerDetails", "Beneficiary", "Is Required : False"),
    ("LedgerDetails", "Card Type", ""),
    ("LedgerDetails", "Print Cheque", "Is Required : False"),
    ("LedgerDetails", "Sub Project", ""),
    ("LedgerDetails", "Budget", ""),
    ("LedgerDetails", "Zone", ""),
    ("LedgerDetails", "Department", ""),
    ("LedgerDetails", "Order", ""),
    ("LedgerDetails", "Milestone", ""),
    ("LedgerDetails", "Tower", ""),
    ("LedgerDetails", "Segment", ""),
    ("LedgerDetails", "Employee", ""),
    ("LedgerDetails", "Employee Name", ""),
    ("LedgerDetails", "Department Name", ""),
    ("LedgerDetails", "Cost Center", ""),
    ("LedgerDetails", "Purpose Of Payment", ""),
    ("AdjustmentDetails", "Link Ref Code", "Is Required : True"),
    ("AdjustmentDetails", "Detail Link Ref Code", "Is Required : True"),
    ("AdjustmentDetails", "Docno", "Is Required : True"),
    ("AdjustmentDetails", "Date", "Is Required : True"),
    ("AdjustmentDetails", "Invoice No", "Is Required : False"),
    ("AdjustmentDetails", "Invoice Date", "Is Required : False"),
    ("AdjustmentDetails", "Bill Amount", ""),
    ("AdjustmentDetails", "Balance Amount", ""),
    ("AdjustmentDetails", "Adjustment Amount", "Is Required : True"),
    ("ImportTaxInfo", "Link Ref Code", "Is Required : True"),
    ("ImportTaxInfo", "Detail Link Ref Code", "Is Required : True"),
    ("ImportTaxInfo", "Deduction Type", "Is Required : True"),
    ("ImportTaxInfo", "Description", "Is Required : True"),
]

# The Deposit Withdrawal workbook's own 4th tab -- same idea as
# INFO_SHEET_ROWS above (static reference data, given verbatim by the user),
# scoped to DW_SHEETS's own columns and headers instead of Receipt Payment's.
INFO_SHEET_ROWS_DW: list[tuple[str, str, str]] = [
    ("DepositWithdrawal", "Link Ref Code", "Is Required : True"),
    ("DepositWithdrawal", "DepositWithdrawal Business Unit",
     "Is Required : False;Min Length : 10;Max Length : 500"),
    ("DepositWithdrawal", "DepositWithdrawal Narration",
     "Is Required : False;Min Length : 10;Max Length : 500"),
    ("DepositWithdrawal", "Financial Year", "Is Required : True;Min Length : 1;Max Length : 30"),
    ("DepositWithdrawal", "Document Type", "Is Required : True;Min Length : 1;Max Length : 30"),
    ("DepositWithdrawal", "Document Date", "Is Required : True;Min Length : 1;Max Length : 30"),
    ("DepositWithdrawal", "Document No", "Is Required : True;Min Length : 1;Max Length : 30"),
    ("DepositWithdrawal", "BankName", ""),
    ("DepositWithdrawal", "EntryTypes", ""),
    ("DepositWithdrawalDetails", "Link Ref Code", "Is Required : True"),
    ("LedgerDetails", "Link Ref Code", "Is Required : True"),
    ("LedgerDetails", "Debit/Credit", "Is Required : True"),
    ("LedgerDetails", "Account Head", "Is Required : True"),
    ("LedgerDetails", "Parent Account Head", "Is Required : True"),
    ("LedgerDetails", "Debit Amount", "Is Required : False"),
    ("LedgerDetails", "Credit Amount", "Is Required : False"),
    ("LedgerDetails", "Payment Mode", "Is Required : True"),
    ("LedgerDetails", "Cheque No", ""),
    ("LedgerDetails", "Cheque Date", ""),
    ("LedgerDetails", "Cheque Type", ""),
    ("LedgerDetails", "Payee Name", "Is Required : True"),
    ("LedgerDetails", "Card Type", ""),
    ("LedgerDetails", "Narration", ""),
    ("LedgerDetails", "Print Cheque", "Is Required : False"),
]

# The full field list fetch_rows builds per row, independent of how to_xlsx_bytes
# later splits it across sheets -- a deduplicated union of both workbooks'
# sheets (aliases resolved to their real field) rather than a second
# hand-written list, so nothing here can drift out of step with SHEETS/DW_SHEETS.
COLUMNS = list(dict.fromkeys(
    _COLUMN_ALIASES.get(col, col)
    for sheets in (SHEETS, DW_SHEETS)
    for cols in sheets.values()
    for col in cols
))

# Head name (upper-cased, trimmed) -> TDS Description. Only heads confirmed by
# the user; everything else stays blank rather than guessed.
_TDS_DESCRIPTION = {
    "CONTRACTOR": "TDS ON CONTRACTORS",
    "PROFESSIONAL": "TDS ON PROFESSIONAL/CONSULTANCY/TECHNICAL/ROYALTY",
    "RENTAL": "TDS ON RENT PAID",
    "A.RENTAL": "TDS ON RENT PAID",
    "OFFICE RENT": "TDS ON RENT PAID",
    "SHOP RENT RECEIVED": "TDS ON RENT PAID",
    "SALARY": "TDS ON SALARY",
    "MKT/ADVER": "TDS ON ADVERTISMENT",
    "HO - ADVERT/MKT": "TDS ON ADVERTISMENT",
    "COMMISSION": "TDS ON BROKERAGE COMMISSION",
    "INTEREST": "TDS ON INTEREST OTHER THAN SECURITIES",
}

# Every Description value the Farvision Verify page's own dropdown offers --
# every keyword-derived value above, plus "TDS PAYABLE (NIL)", confirmed with
# the user as a real Description with no head keyword of its own (it isn't
# implied by any particular head, it's a person's own call that no TDS is
# actually due on a row that would otherwise guess a keyword match). Used
# only for that manual override; _TDS_DESCRIPTION above is still the only
# thing that auto-fills a row before anyone touches it.
DESCRIPTION_OPTIONS = sorted(set(_TDS_DESCRIPTION.values()) | {"TDS PAYABLE (NIL)"})

_TDS_RATE_NUMBER_RE = re.compile(r"(\d+(?:\.\d+)?)")


def parse_tds_rate(text: str | None) -> decimal.Decimal | None:
    """Pull a percentage out of a typed TDS Rate ("2%", "10", "2 %") as a 0-1
    fraction, or None when there's nothing usable -- no digits, or a number
    outside (0, 100) (0% needs no grossing-up at all; 100% divides by zero).
    """
    if not text:
        return None
    m = _TDS_RATE_NUMBER_RE.search(text)
    if not m:
        return None
    value = decimal.Decimal(m.group(1))
    if not (0 < value < 100):
        return None
    return value / decimal.Decimal(100)


def gross_up_debit_amount(
    debit_amount: decimal.Decimal | None, rate_fraction: decimal.Decimal | None,
) -> decimal.Decimal | None:
    """Reverse-calculate the gross amount from a net Debit Amount and a TDS
    Rate fraction -- e.g. net 98,000 at 2% -> gross 100,000. None when either
    input is missing: a row with no Debit Amount (a Credit-side row) has
    nothing to gross up, confirmed with the user as a case where the TDS Rate
    note is still saved but the reverse calculation does nothing.

    Rounded to the nearest whole rupee, not 2 decimal places -- confirmed
    with the user against a real example (net 15,331 at 1% divides out to
    15,485.858585..., which must round to 15,486, not stay 15,485.86).
    ROUND_HALF_UP is exactly ".50 rounds up, below .50 rounds down".
    """
    if not debit_amount or rate_fraction is None:
        return None
    gross = debit_amount / (decimal.Decimal(1) - rate_fraction)
    return gross.quantize(decimal.Decimal("1"), rounding=decimal.ROUND_HALF_UP)

# Read-only reference material for the Master Data page's Farvision Account
# tabs: the format/example values found in the original master sheet for
# columns that carry no genuine per-Account-Head data, so a person filling
# the export by hand (or checking it) can see what these fields are supposed
# to look like without them cluttering the master table as mostly-blank
# columns. Not consumed anywhere in fetch_rows -- purely informational.
REFERENCE_VALUES = [
    {"field": "Financial Year", "values": ["01-04-2026-31-03-2027",
        "01-04-2027-31-03-2028", "01-04-2029-31-03-2030"],
     "note": "Format is 01-04-<year>-31-03-<year+1>. The export builds this "
             "automatically from temp_trans's own \"FY YY-YY\" text."},
    {"field": "Document Type / EntryTypes", "values": ["RECEIPT / PAYMENT", "Deposit / Withdrawal"],
     "note": "The export fills these from the row's head type, not from this list."},
    {"field": "Debit/Credit", "values": ["Debit", "Credit"]},
    {"field": "Business Unit", "values": ["ARAVALI HEIGHTS", "CASA ROMANA",
        "DWARKADHIS PROJECTS PVT. LTD-HO"],
     "note": "The export maps temp_trans's own Business Unit value onto these exact "
             "Farvision strings (e.g. \"HO\" -> \"DWARKADHIS PROJECTS PVT. LTD-HO\"); "
             "anything that doesn't match one of the three is passed through as-is."},
    {"field": "Deduction Type", "values": ["Tax deducted at source", "Goods and Service Tax"],
     "note": "The export currently only ever fills \"Tax deducted at source\", "
             "when Description matches a TDS keyword."},
    {"field": "Description (TDS)", "values": sorted(set(_TDS_DESCRIPTION.values())),
     "note": "Filled automatically from the row's head name via the keyword table above."},
    {"field": "Payment Mode", "values": ["Direct"], "note": "The export always fills this literally."},
    {"field": "Docno", "values": ["ON A/C"], "note": "The export always fills \"ON A/c\" literally."},
    {"field": "Invoice No", "values": ["Normal"], "note": "The export always fills this literally."},
]


# temp_trans's own Business Unit text (field_text_4) -> the exact string
# Farvision expects. Confirmed with the user against real values found in
# temp_trans -- "HO" alone isn't a real Farvision business unit, it's this
# project's short form of the full name.
_BUSINESS_UNIT_MAP = {
    "ARAVALI HEIGHTS": "ARAVALI HEIGHTS",
    "CASA ROMANA": "CASA ROMANA",
    "HO": "DWARKADHIS PROJECTS PVT. LTD-HO",
}


def _format_business_unit(business_unit: str | None) -> str | None:
    if not business_unit:
        return business_unit
    return _BUSINESS_UNIT_MAP.get(business_unit.strip().upper(), business_unit)


def _debit_or_credit(debit_amount, credit_amount) -> str | None:
    """"Debit" when the row has a real debit amount, "Credit" otherwise.

    field_text_19 (temp_trans's own Debit/Credit text) is often blank --
    confirmed against a real batch where it was NULL on every row. Which of
    Debit Amount / Credit Amount actually holds the row's real amount already
    says which this is -- but "holds an amount" means non-zero, not merely
    non-NULL: a real row was found (id 23974, an Internal Fund Transfer) with
    debit_amount explicitly 0.00 and credit_amount 450000.00, which an
    `is not None` check wrongly called "Debit". Checked by truthiness instead
    so a zero placeholder alongside the row's real amount falls through to
    the side that actually holds it.
    """
    if debit_amount:
        return "Debit"
    if credit_amount:
        return "Credit"
    return None


def _is_internal(head_name: str | None) -> bool:
    """Head Type is Internal Head AND the name itself is literally Internal.

    head_master stores this concept as a direction-suffixed pair (INTERNAL DR/
    INTERNAL CR) rather than one shared row -- confirmed with the user, same
    fallback shape used when conditions were built against this same master.
    """
    return bool(head_name) and head_name.strip().upper().startswith("INTERNAL")


def _skip_document_type(head_name: str | None) -> bool:
    if not head_name:
        return False
    n = head_name.strip().upper()
    return n in ("CANCELLATION", "COLLECTION")


_FY_RE = re.compile(r"(\d{2})\s*-\s*(\d{2})")


def _format_financial_year(fy_text: str | None) -> str | None:
    """"FY 26-27" -> "01-04-2026-31-03-2027", the master's own example format.

    Confirmed against the master's 3 sample Financial Year rows, all of which
    spell out the same 1 April - 31 March span the sheet's own short "FY
    YY-YY" text already implies. Text that doesn't look like "FY YY-YY" is
    left exactly as it came from temp_trans rather than guessed.
    """
    if not fy_text:
        return fy_text
    m = _FY_RE.search(fy_text)
    if not m:
        return fy_text
    y1, y2 = int(m.group(1)), int(m.group(2))
    return f"01-04-20{y1:02d}-31-03-20{y2:02d}"


async def _bank_name_candidates(conn) -> list[str]:
    rows = await conn.fetch("SELECT name FROM farvision_bank_name_master WHERE is_active")
    return [r["name"] for r in rows]


def _match_bank_name(account_number: str | None, candidates: list[str]) -> str | None:
    """The Farvision Bank Name whose text embeds this account's own digits.

    bank_master's own bank_name is a short generic label ("BOM", "YES", "KVB")
    shared by several accounts; the account number is what's actually unique,
    and every Farvision Bank Name that names a bank account embeds it in the
    text. Leading zeros are stripped since some sources keep them and others
    don't. When an account number's digits appear in more than one Farvision
    name (confirmed to happen once, for a shared "BOM" account), the shortest
    match wins -- the longer one is a project-specific alias of the same
    account, confirmed with the user for that exact case.
    """
    digits = re.sub(r"\D", "", account_number or "").lstrip("0")
    if not digits:
        return None
    hits = [c for c in candidates if digits in re.sub(r"\D", "", c)]
    return min(hits, key=len) if hits else None


async def _bank_short_codes(conn) -> list[str]:
    """bank_master's own short bank labels ("BOM", "YES", "KVB", ...).

    A curated vocabulary rather than an open word scan of DESC -- confirmed
    necessary after DESC-matching for Internal rows needed a fallback when no
    account number is present, and an unbounded scan of DESC's own words
    ("TPT", "CIRP", "PROJECTS", ...) would repeat the exact kind of false
    positive already fixed once for Account Head matching (_MIN_MATCH_LENGTH).
    """
    rows = await conn.fetch(
        "SELECT DISTINCT bank_name FROM bank_master WHERE bank_name IS NOT NULL AND bank_name <> ''")
    return [r["bank_name"] for r in rows]


_ACCOUNT_NUMBER_RE = re.compile(r"\d{6,}")

# A masked/partial account number -- all some rows ever give. Confirmed
# against real DWARKADHIS-internal-transfer rows whose DESC ends
# "...BANK OF MAHARASHTRA9675" or "...BANK OF MAHARASHTRAx9675": no 6+ digit
# run exists anywhere (so _ACCOUNT_NUMBER_RE above never fires), but the
# trailing 4 digits are the real account's own last 4 -- the same tail
# _match_bank_name already trusts for BankName. The 'x' is an occasional
# masking character seen live between the bank's name and the digits.
_TRAILING_LAST4_RE = re.compile(r"(\d{4})\D{0,3}$")

# A bank sometimes writes its own full name in DESC rather than the short
# code bank_master and Farvision's own Bank Name entries use -- confirmed
# against real rows reading "...BANK OF MAHARASHTRA" with the word
# "MAHARASHTRA" spelled out in full and no "BOM" anywhere in the text, which
# the plain code-substring check below could never recognise. Matched as a
# whole phrase, not a shared word, so "MAHARASHTRA" turning up in some
# unrelated context does not tag a row that has nothing to do with this bank.
# Only the banks this app's own Apps Script (Code.gs's BANK_SENDERS) and
# bank_master actually know about -- a bank never seen here has no short
# code to translate a spelled-out name into anyway.
_BANK_NAME_ALIASES = {
    "BANK OF MAHARASHTRA": "BOM",
    "AXIS BANK": "AXIS",
    "YES BANK": "YES",
    "KARUR VYSYA BANK": "KVB",
}


def _match_internal_account_head(
    desc_text: str | None, bank_names: list[str], short_codes: list[str],
) -> tuple[str | None, list[str] | None]:
    """For an Internal-head row (a transfer between the company's own bank
    accounts, not a payment to any external party), Account Head is the
    specific Farvision Bank Name on the other side of the transfer, not a
    party from the Account Head master -- confirmed with the user after
    whole-DESC Account Head matching started accidentally matching the
    company's own legal name in transfer routing text ("...vide YIB-TPT-
    DWARKADHIS PROJECTS PRIVATE LIMITED...") against an unrelated Account
    Head master row of the same name.

    Tries, in order, confirmed with the user:
      1. An account number embedded in DESC, matched the same way
         _match_bank_name already matches BankName -- confident, filled
         directly. More than one distinct bank matched this way is offered
         as a dropdown instead of guessing between them.
      2. A masked account number's last 4 digits, trailing DESC -- the only
         signal some rows give at all (see _TRAILING_LAST4_RE above).
      3. A short bank code, or one of its spelled-out full-name aliases,
         found in DESC -- offered as a dropdown of every Farvision Bank Name
         sharing that code, since several accounts can.
      4. Every Farvision Bank Name, as a last-resort dropdown, when DESC gave
         no usable hint at all.
    """
    if not bank_names:
        return None, None

    # A source formatting quirk sometimes puts a bare space inside an account
    # number ("0455632 00000264") -- confirmed live. Left alone, the half
    # after the space can strip down to only 2-3 leading-zero-stripped digits
    # ("264"), too short to trust as a match on its own even though it
    # happened to be correct there; merging digit runs split only by
    # whitespace fixes the number itself instead of relying on luck.
    compact = re.sub(r"(?<=\d)\s+(?=\d)", "", desc_text or "")
    numbers = {re.sub(r"\D", "", n).lstrip("0") for n in _ACCOUNT_NUMBER_RE.findall(compact)}
    numbers = {n for n in numbers if len(n) >= 6}
    by_number = {b for n in numbers for b in bank_names if n in re.sub(r"\D", "", b)}
    if len(by_number) == 1:
        # A confident match still gets an options list -- not used to decide
        # this row, but there for the Farvision Verify page's "Not correct?"
        # override, confirmed with the user: matching isn't infallible, and a
        # confident row should still be correctable without hunting through
        # every bank name by hand.
        head = next(iter(by_number))
        return head, (_closest_matches(desc_text, bank_names) or [head])
    if len(by_number) > 1:
        return None, sorted(by_number)

    m = _TRAILING_LAST4_RE.search(compact.strip())
    if m:
        last4 = m.group(1)
        by_last4 = {b for b in bank_names if re.sub(r"\D", "", b).endswith(last4)}
        if len(by_last4) == 1:
            head = next(iter(by_last4))
            return head, (_closest_matches(desc_text, bank_names) or [head])
        if len(by_last4) > 1:
            return None, sorted(by_last4)

    desc_upper = (desc_text or "").upper()
    # \b, not a bare substring -- confirmed against real data that every one
    # of these NEFT rows' DESC starts "YIB-NEFT-YESME<utr>...", Yes Bank's
    # own UTR reference prefix on the SENDING side, present regardless of
    # which bank the transfer is actually going to. A plain `"YES" in
    # desc_upper` matched that prefix's "YES" on every single row here, not
    # just a real "Yes Bank" mention -- \b requires a real word boundary, so
    # it still matches "...YES BANK..." (space before/after) but not "YESME"
    # (no boundary between the "S" and the "M" that follows).
    matched_codes = {
        code for code in short_codes
        if code and re.search(rf"\b{re.escape(code.upper())}\b", desc_upper)
    }
    matched_codes.update(
        code for phrase, code in _BANK_NAME_ALIASES.items() if phrase in desc_upper)
    by_code = {
        b for code in matched_codes
        for b in bank_names if code.upper() in b.upper()
    }
    if by_code:
        return None, sorted(by_code)

    # No signal at all -- leave options None (not the whole bank-name pool,
    # which is small here but would still be the same needless per-row copy
    # the account-head case below was fixed for). fetch_rows's own generic
    # fallback picks up right after this and, finding nothing better, leaves
    # it None too -- the caller (the Farvision Verify page) already has the
    # full pool once, fetched separately, and falls back to that itself.
    closest = _closest_matches(desc_text, bank_names)
    return None, (closest or None)


_ACCOUNT_TABLES = {"DPL": "farvision_account_master_dpl", "AMB": "farvision_account_master_amb"}


# What counts as a trailing internal reference code -- "AH000349",
# "AH003677", "CR0080", "E1000283", "E1000283_D" -- 1-4 letters then 3+
# digits, an optional trailing "_" + a letter -- or a bare number ("710",
# the whole of "Rahul Aggarwal(710)"'s own trailing word once punctuation is
# stripped). Deliberately NOT "any word with a digit in it": an early cut of
# this tried that and stripped "HEAD2" off a genuine two-word head "SALARY
# HEAD2", collapsing it to the bare word "SALARY" -- generic enough to match
# almost any salary-related DESC and turn a should-stay-blank row into a
# confident, wrong one. Requiring 3+ digits (or an all-digit token) is what a
# real ledger id looks like; "HEAD2" (four letters, one digit) does not,
# and is correctly left alone.
_CODE_WORD_RE = re.compile(r"^[A-Z]{1,4}\d{3,}(?:_[A-Z])?$|^\d{2,}$")


def _strip_trailing_code(words: list[str]) -> list[str]:
    """Drop a trailing internal reference code from an Account Head's own
    normalized words -- "RAHUL KUMAR SHARMA AH003677", "MAHIPAL SINGH
    YADAV CR0080", "RAHUL AGGARWAL 710" all carry one, and it is this app's
    own ledger id, not text a bank's narration could ever spell out.
    Confirmed against real data: ~48% of AMB's own Account Head master ends
    in a word containing a digit, and the plain substring match below never
    matched any of them for exactly this reason -- the candidate's FULL
    normalized text, code included, had to appear verbatim in DESC, which it
    structurally never can.

    Only trailing words are dropped, one at a time, stopping at the first
    real (non-code) word from the end -- a code embedded earlier in a name is
    not this pattern and is left alone. See _CODE_WORD_RE above for what
    counts as a code.

    Stripped all the way to a single word when that is genuinely all that is
    left -- "Santosh(419)" is only ever going to be two tokens, name and
    code, and stopping short of the code would leave it permanently
    unmatchable (confirmed live). The risk that motivated an earlier, more
    conservative version of this function -- several bare-name-plus-code
    entries ("RAHUL - E1000283_D", "RAHUL - E1000283", ...) collapsing to the
    same generic "RAHUL" and one being picked arbitrarily -- is handled where
    it belongs instead: _match_account_head now checks for exactly this kind
    of tie and offers every tied candidate rather than guessing one.

    A numeric code is sometimes followed by its own short bracketed tag --
    "RAHUL KUMAR - CR0198(AR)" normalizes to four words ending "... CR0198
    AR", and "AR" alone does not look like a code (no digit at all), so
    popping only the last word would stop one word too early and leave
    "AR" stuck onto the core. Both go together only when the short tag
    directly follows a word that IS code-shaped -- a real trailing initial
    or short surname elsewhere is never touched, since nothing code-shaped
    precedes it.
    """
    out = list(words)
    while len(out) > 1:
        if (len(out) > 2 and _CODE_WORD_RE.match(out[-2])
                and out[-1].isalpha() and len(out[-1]) <= 3):
            out = out[:-2]
            continue
        if _CODE_WORD_RE.match(out[-1]):
            out.pop()
            continue
        break
    return out


async def _account_head_candidates(conn, company: str | None) -> list[dict]:
    """This company's Account Heads, most specific (longest matchable core)
    first.

    Longest-first means the first hit while scanning in order is also the
    most specific one -- the same reasoning the Rules engine uses: a bare,
    generic head name should not win over one that actually names the party.
    Sorted by the CORE text (trailing reference code stripped), not the raw
    normalized text, so a short name padded out by a long code ("RAHUL -
    AH003677_D") does not rank above a genuinely longer name with no code at
    all purely because its own text happens to be longer. Looked up from the
    row's own company's table, so 'INTEREST' in DPL's books cannot match an
    AMB row and vice versa. A company that isn't DPL or AMB has no table to
    match against.
    """
    table = _ACCOUNT_TABLES.get((company or "").strip().upper())
    if not table:
        return []
    rows = await conn.fetch(f"SELECT account_head, parent_account_head FROM {table}")
    candidates = [dict(r) for r in rows]
    for c in candidates:
        c["_norm"] = _normalize_party_name(c["account_head"])
        core_words = _strip_trailing_code(c["_norm"].split())
        c["_core"] = " ".join(core_words)
        # As a set, not just the joined string -- used by _match_account_head's
        # second pass, which checks a candidate's words against DESC's own
        # words PLUS every adjacent pair of them re-joined, to survive a
        # stray space a PDF extraction inserted INSIDE a real word ("RAHU L
        # KUMAR" for "RAHUL KUMAR", "SANTO SH" for "SANTOSH", both confirmed
        # live) -- order no longer matters once a word has already been
        # split apart by whatever broke it.
        c["_core_words"] = frozenset(core_words)
    return sorted(candidates, key=lambda r: -len(r["_core"]))


def _duplicate_key(account_head: str) -> str:
    """Uppercase, punctuation stripped, whitespace collapsed.

    Looser than _normalize_party_name (no suffix folding, no plural
    stripping) on purpose: this is for spotting two rows that are almost
    certainly the same typed-twice entry ("priyanka Redhu" / "priyanka
    Redhu."), not for narration matching, so it should only fold away pure
    formatting noise and nothing that could change meaning.
    """
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", account_head.upper())).strip()


async def _duplicate_options_map(conn, company: str) -> dict[str, list[str]]:
    """account_head text -> every spelling that normalizes the same way
    (itself included), only for account heads that actually have a duplicate.

    Used by fetch_rows to know when a match landed on an ambiguous head so
    the export can offer every spelling in an Excel dropdown instead of
    guessing one -- confirmed with the user: there is no "canonical" spelling
    tracked anywhere, every duplicate is presented every time.
    """
    table = _ACCOUNT_TABLES.get((company or "").strip().upper())
    if not table:
        return {}
    rows = await conn.fetch(f"SELECT account_head FROM {table}")
    groups: dict[str, list[str]] = {}
    for r in rows:
        groups.setdefault(_duplicate_key(r["account_head"]), []).append(r["account_head"])
    options: dict[str, list[str]] = {}
    for heads in groups.values():
        if len(heads) < 2:
            continue
        heads_sorted = sorted(set(heads))
        for h in heads_sorted:
            options[h] = heads_sorted
    return options


# Confirmed against a real mismatch: the master's "Gamut Infosystem Ltd"
# didn't match narration text reading "GAMUT INFOSYSTEMS LIMITED" -- same
# company, just spelled with the full/plural corporate suffix instead of the
# master's abbreviated/singular one. Normalizing both sides the same way
# before comparing catches this without doing open-ended fuzzy matching.
_SUFFIX_MAP = {"LIMITED": "LTD", "PRIVATE": "PVT"}


# A period-joined run of single letters ("R.K.", "A.K.", "M.R.") is
# initials, and the punctuation-stripping below would otherwise scatter it
# into separate one-letter words ("R", "K") no bank text would ever spell
# out that way -- confirmed against real vendor entries in the master
# ("A.K.TRADERS", "M.R.Traders", "R.K. TRADERS") that a bank's own DESC
# writes without any periods at all ("RK Traders"). Collapsed before the
# general punctuation-stripping runs, and only when the periods sit directly
# between the letters with no space -- "A N Filling Station", where "A" and
# "N" are already separate words in the source, is a different shape (an
# initial-letter business name, not dotted initials) and is deliberately
# left alone; merging that too turned out to eat an unrelated "M/S" prefix
# sitting next to it as well.
_INITIALS_RE = re.compile(r"(?:[A-Za-z]\.){2,}")


def _normalize_party_name(text: str | None) -> str:
    if not text:
        return ""
    text = _INITIALS_RE.sub(lambda m: m.group(0).replace(".", ""), text)
    words = re.sub(r"[^\w\s]", " ", text.upper()).split()
    out = []
    for w in words:
        w = _SUFFIX_MAP.get(w, w)
        # >=6, not >3: confirmed live that the shorter threshold corrupts a
        # real name fragment that merely happens to end in "S" rather than
        # actually being a plural -- "SATIS" (half of "SATIS H", itself
        # "SATISH" split by a stray extraction space) was being singularized
        # to "SATI" here, before Pass 2's own stray-space reconstruction
        # ever got a chance to see the real word and rejoin it, so it never
        # found "SATISH" at all. Every genuine case this was built for
        # ("INFOSYSTEMS", "PROJECTS", "SERVICES", ...) is well past 6
        # letters on its own; a short word this aggressive fold could still
        # mis-singularize just stays exactly as it came from the bank
        # instead, which only costs a possible match, never a wrong one.
        if len(w) >= 6 and w.endswith("S") and w not in ("LTD", "PVT"):
            w = w[:-1]
        out.append(w)
    return " ".join(out)


# Below this length, a normalized word is too generic to count as a shared
# keyword between DESC and a candidate -- the same reasoning as
# _MIN_MATCH_LENGTH below, just applied per-word instead of to a whole
# candidate name.
_KEYWORD_MIN_LEN = 4


def _closest_matches(desc_text: str | None, texts: list[str], limit: int = 20) -> list[str]:
    """Candidates ranked by how many 4+ letter normalized words they share
    with DESC, most shared first -- confirmed with the user as the general
    fallback for a "Not correct?" override, a genuinely blank row, or any
    residual ambiguity: not the full ~7,900-row master, not free-text search,
    just whichever entries actually look related to this row's own bank
    text. Ties keep the shorter (more specific) text first; the list is
    capped so a common word shared by hundreds of entries doesn't produce an
    unusably long dropdown.
    """
    desc_words = {w for w in _normalize_party_name(desc_text).split() if len(w) >= _KEYWORD_MIN_LEN}
    if not desc_words:
        return []
    scored = []
    seen = set()
    for text in texts:
        if text in seen:
            continue
        words = {w for w in _normalize_party_name(text).split() if len(w) >= _KEYWORD_MIN_LEN}
        overlap = len(desc_words & words)
        if overlap:
            scored.append((overlap, len(text), text))
            seen.add(text)
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [text for _, _, text in scored[:limit]]


def _same_letter_options(desc_text: str | None, texts: list[str], limit: int = 30) -> list[str]:
    """Last resort when even _closest_matches shares no whole keyword at
    all: every candidate whose own first letter matches some real (4+
    letter) word in DESC.

    Confirmed with the user as their own explicit ask for exactly this
    situation -- a PDF-extraction stray space can break a name badly enough
    that not one whole word survives to overlap on ("RAHU L KUMAR" shares no
    4+-letter word with any "RAHUL ..." head, since "RAHU" and "L" are both
    on their own). Not a match, and not scored or ranked -- just a narrower
    "possibility" list than the entire master, on the one signal that
    survives almost any mid-word split: the first letter.
    """
    letters = {w[0] for w in _normalize_party_name(desc_text).split() if len(w) >= _KEYWORD_MIN_LEN}
    if not letters:
        return []
    return sorted({t for t in texts if t and t[0].upper() in letters})[:limit]


# Below this normalized length, a head name is too generic to trust as a
# plain substring match -- confirmed against two real false positives once
# punctuation-stripping was added: "CR--" (normalizes to "CR", 2 chars)
# matched almost every narration mentioning the "YES CR FREE" bank account,
# and "Car" (3 chars) matched "Credit Card" (its own letters are a substring
# of "Card"). Real 5+ letter names (people's names, "HDFC", "BONUS", ...)
# still match fine; a genuine match this short just isn't distinguishable
# from an accidental one and is left blank instead of guessed.
_MIN_MATCH_LENGTH = 5


def _levenshtein(a: str, b: str) -> int:
    """Single-character edits (insert/delete/substitute) to turn a into b.

    A small, dependency-free DP -- both strings here are single normalized
    words, at most a couple dozen characters, so the classic O(len(a)*len(b))
    table costs nothing worth optimizing further.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(
                prev[j] + 1,        # delete from a
                cur[j - 1] + 1,     # insert into a
                prev[j - 1] + (0 if ca == cb else 1),  # match or substitute
            )
        prev = cur
    return prev[-1]


# A word shorter than this only ever gets an exact match (see
# _fuzzy_word_in) -- distance 1 on a 4-letter word is a quarter of it
# wrong, which stops being distinguishable from a genuinely different short
# word ("RAM" vs "RAN"). Real names long enough to carry a spelling slip
# without becoming a different plausible word still get the tolerance.
_FUZZY_MIN_WORD_LEN = 5
_FUZZY_MAX_DISTANCE = 1


def _fuzzy_lookup(pool: set[str]):
    """A `word in pool-ish` test tolerant of one character's difference, for
    a long enough word -- built once per row (see _match_account_head's Pass
    3) rather than re-scanning the whole pool from scratch for each of the
    thousands of candidates checked against it.

    Confirmed against real data: six real transactions for one account all
    spell it "Ramabati Devi", the master's only matching entry reads
    "Ramavati Devi(411)" -- one letter different, evidently a typo in one of
    the two independent sources, not a PDF-extraction artifact at all (there
    is no stray space to reconstruct here).

    Bucketed by length so a check only ever compares against pool words
    within one character of its own length -- most of `pool` differs enough
    in length to rule out on sight, and doing that once here (an index) is
    far cheaper than doing it inline for every candidate word (a linear
    scan), which is what made an earlier version of this measurably double
    the time to match a page of rows.
    """
    by_len: dict[int, list[str]] = {}
    for w in pool:
        by_len.setdefault(len(w), []).append(w)

    def lookup(word: str) -> bool:
        if word in pool:
            return True
        if len(word) < _FUZZY_MIN_WORD_LEN:
            return False
        nearby = (
            by_len.get(len(word) - 1, [])
            + by_len.get(len(word), [])
            + by_len.get(len(word) + 1, [])
        )
        return any(_levenshtein(word, w) <= _FUZZY_MAX_DISTANCE for w in nearby)

    return lookup


# NARRATION's own "To: <party>" segment, when it has one -- confirmed with
# the user as the new preferred first look: Narration is built from other
# already-classified fields (see _match_account_head's own note on it below)
# and that classification has typically already isolated the party name in
# one clean field, no hyphen-joined bank codes and reference numbers around
# it the way DESC always has. Only the "To:" segment itself, not the whole
# line -- "Purpose:", "Ref:", "BU:" and "Head:" are never a party name and
# would only add noise to search on.
_NARRATION_TO_RE = re.compile(r"\bTo:\s*([^|]+)", re.IGNORECASE)


def _narration_to_name(narration: str | None) -> str | None:
    if not narration:
        return None
    m = _NARRATION_TO_RE.search(narration)
    if not m:
        return None
    name = m.group(1).strip()
    return name or None


def _match_account_head(
    desc_text: str | None, candidates: list[dict], narration: str | None = None,
) -> tuple[str | None, str | None, list[str] | None]:
    """Search temp_trans's own DESC field (field_text_1) for the party a
    non-Internal row's Account Head should be, checking NARRATION's own
    "To: <party>" segment first and DESC only if that finds nothing at all.

    NARRATION (field_text_11) is a synthetic field, built from other already-
    classified fields rather than typed by the bank -- confirmed against real
    data where it was frequently just the literal placeholder text "Remarks
    Compulsory For Narration" instead. That still makes it circular to trust
    on its own (a bad classification elsewhere would make it look confident
    when it is just repeating the mistake), which is why DESC -- the bank's
    own raw hyphen-joined text ("YIB-NEFT-YESME62170041087-INDIA PRIDE COM-
    CNRB0001565-VENDOR- CANARA BANK") -- is still tried whenever the
    NARRATION attempt below comes back with nothing, and remains the only
    source for an Internal row's bank-side matching entirely (see
    _match_internal_account_head, which never sees NARRATION).
    But when NARRATION does have a real "To:" segment, confirmed with the
    user as worth trying FIRST: it is usually a clean, already-isolated party
    name with none of DESC's surrounding bank codes and reference numbers to
    confuse a keyword match, which is exactly the noise the three passes
    below exist to filter through in the first place.

    Returns (account_head, parent_account_head, options): options is
    non-None whenever more than one candidate ties for the win -- callers
    should treat that exactly like the existing duplicate-spelling case
    (leave account_head/parent blank, offer options instead).
    """
    to_name = _narration_to_name(narration)
    if to_name:
        result = _match_text_against_candidates(to_name, candidates)
        if result != (None, None, None):
            return result
    return _match_text_against_candidates(desc_text, candidates)


def _match_text_against_candidates(
    text: str | None, candidates: list[dict],
) -> tuple[str | None, str | None, list[str] | None]:
    """The two-pass search itself, run against whichever text
    _match_account_head decided to try -- NARRATION's "To:" segment first,
    DESC if that found nothing.
    """
    if not text:
        return None, None, None
    search_text = _normalize_party_name(text)
    if not search_text:
        return None, None, None

    # Pass 1+2, combined: rejoin every adjacent pair of DESC's own words and
    # add that to the pool a candidate's core can draw from, then check as a
    # SET of words rather than one ordered phrase -- tolerates a stray space
    # a PDF extraction sometimes inserts INSIDE a real word. Confirmed live
    # on two different shapes: "RAHU L KUMAR" for a two-word head "RAHUL
    # KUMAR" (needs "RAHUL" as one word, which it never is here), and
    # "SANTO SH" for a bare one-word head "SANTOSH" (no surname anywhere in
    # that DESC at all, so a whole-string blob match could never work
    # either -- only the specific adjacent pair "SANTO"+"SH" reconstructs
    # it).
    #
    # An exact, word-ordered substring match (the more precise check this
    # started out as two separate passes for) is just a special case of this
    # same set check -- every word an ordered substring needs is trivially
    # already in `reconstructed`, since that always contains DESC's own
    # words verbatim before any pair-merging is even added on top. Keeping
    # them separate, ordered-substring first, was what caused a real bug:
    # "SATIS H" (half of "SATIS H" reconstructing to "SATISH") has a
    # DIFFERENT, unrelated candidate literally spelled "Satis" -- a shorter,
    # plainer coincidental substring match that the old ordered-first pass
    # returned immediately, before the better "SATISH" reconstruction two
    # words later ever got a chance to be compared against it. Running both
    # as one set of hits and keeping only the single most-specific (longest
    # core) tier is what makes "SATISH" correctly outrank "SATIS" -- the
    # same specificity rule already applied below, just no longer blocked
    # from ever being reached.
    #
    # Not restricted to multi-word cores: a bare single-word head matching
    # is an accepted, pre-existing risk in this codebase, and reaching a
    # SPECIFIC one this way still requires either an unbroken word already
    # in DESC or a RECONSTRUCTED pair -- not any word merely floating in the
    # text -- so a coincidental hit is a narrow enough risk to accept, and a
    # code fully stripped down to a bare, common first name ("RAHUL -
    # E1000283_D" and "RAHUL - E1000283" both reduce to plain "RAHUL") tying
    # with other candidates at the SAME core length is exactly the ambiguity
    # this hands to a human instead of picking one arbitrarily.
    desc_words = search_text.split()
    reconstructed = set(desc_words)
    for i in range(len(desc_words) - 1):
        merged = desc_words[i] + desc_words[i + 1]
        if len(merged) >= _MIN_MATCH_LENGTH:
            reconstructed.add(merged)
    hits = [
        c for c in candidates
        if len(c["_core"]) >= _MIN_MATCH_LENGTH and c["_core_words"] <= reconstructed
    ]
    if hits:
        # Most-specific tier only: a DESC that reconstructs to "RAHUL KUMAR"
        # (surname present) should not have that clean 2-word match diluted
        # by also listing every bare "RAHUL"-alone placeholder entry --
        # those matched too (a single word is trivially a subset of any set
        # containing it), but they are strictly less specific than one that
        # used every reconstructed word. Only when the BEST tier itself has
        # more than one candidate (several different "Santosh ..." people,
        # say, with no more specific tier to prefer) is that genuine
        # ambiguity handed to a human -- confirmed with the user as their
        # own explicit ask for exactly this situation.
        best = max(len(c["_core"]) for c in hits)
        hits = [c for c in hits if len(c["_core"]) == best]
        if len(hits) == 1:
            c = hits[0]
            return c["account_head"], c["parent_account_head"], None
        return None, None, sorted({c["account_head"] for c in hits})

    # Pass 2: the same reconstructed word pool, but each of a candidate's
    # words is now allowed to be a fuzzy match (see _fuzzy_lookup) rather
    # than requiring the exact letters -- confirmed live and genuinely
    # different from the pass above: a one-character spelling slip
    # between the bank's own text and this app's master data ("Ramabati
    # Devi" in six real transactions on one account, "Ramavati Devi(411)"
    # the only entry anywhere in either master), not a PDF-extraction stray
    # space at all -- there is nothing to reconstruct, the letters
    # themselves differ. Tried only after every exact-word option is
    # exhausted, so a genuinely exact match is never displaced by a fuzzy
    # one.
    fuzzy_in = _fuzzy_lookup(reconstructed)
    fuzzy_hits = [
        c for c in candidates
        if len(c["_core"]) >= _MIN_MATCH_LENGTH
        and all(fuzzy_in(w) for w in c["_core"].split())
    ]
    if fuzzy_hits:
        best = max(len(c["_core"]) for c in fuzzy_hits)
        fuzzy_hits = [c for c in fuzzy_hits if len(c["_core"]) == best]
        if len(fuzzy_hits) == 1:
            c = fuzzy_hits[0]
            return c["account_head"], c["parent_account_head"], None
        return None, None, sorted({c["account_head"] for c in fuzzy_hits})

    return None, None, None


async def lookup_parent_account_head(conn, account_head: str) -> str | None:
    """The Parent Account Head that goes with an exact Account Head text.

    Used when the Farvision Verify page resolves an ambiguous row: the
    dropdown there only offers plain Account Head strings (same shape as the
    old Excel dropdown), so the matching Parent has to be looked up
    separately at save time rather than trusted from the client. Tries both
    company tables since the caller does not necessarily know which one the
    chosen text came from -- a bank name (the Internal-row case) matches
    neither table and correctly returns None.
    """
    for table in _ACCOUNT_TABLES.values():
        row = await conn.fetchrow(
            f"SELECT parent_account_head FROM {table} WHERE account_head = $1", account_head)
        if row:
            return row["parent_account_head"]
    return None


async def candidate_pools(conn, schema: str) -> dict:
    """Every Farvision Bank Name and, per company, every Account Head --
    fetched (and cached -- see _cached above) once, not once per row.

    The Farvision Verify page's own listing (fetch_rows above) leaves a
    row's "options" None when it has no better candidate list of its own;
    this is what a blank row falls back to client-side, requested and
    cached separately so paging through a batch never re-downloads it.
    """
    bank_names = await _cached((schema, "bank_names"), lambda: _bank_name_candidates(conn))
    account_heads = {}
    for company in _ACCOUNT_TABLES:
        candidates = await _cached(
            (schema, "account_heads", company),
            lambda company=company: _account_head_candidates(conn, company))
        account_heads[company] = sorted(c["account_head"] for c in candidates)
    return {
        "bank_names": sorted(bank_names),
        "account_heads": account_heads,
        "descriptions": DESCRIPTION_OPTIONS,
    }


async def link_ref_codes(conn, where: str, params: list) -> dict[int, int | None]:
    """Every row's own Link Ref Code, matching exactly what it will get in
    its real export file -- a running count per Document Type (Payment/
    Reciept counts every one of its own rows; Deposit/withdrawal counts only
    its own Debit leg, mirroring filter_deposit_withdrawal's own Credit-leg
    exclusion), continuing across the *whole* filtered batch rather than
    resetting every 50-row Verify page. Confirmed with the user after two
    false-alarm "wrong sheet" reports that both traced back to the Verify
    page showing one global counter while each export renumbers its own two
    groups independently starting at 1 -- this makes the number shown on
    Verify the exact number that row will carry in its export, so the two
    can always be cross-referenced directly.

    A row this numbering skips entirely (a Credit leg, or a skipped document
    type -- _skip_document_type) maps to None: it will not appear in either
    export at all, so it gets no Link Ref Code to show.

    Deliberately cheap: only head_name/debit_amount/credit_amount, none of
    fetch_rows' Account Head matching -- the whole filtered batch is a few
    hundred rows even though a Verify page only ever shows 50 of them.

    p/bn are joined here too, unread, for the same reason fetch_rows joins
    them -- so a WHERE built by _temp_filters can reference p.name/bn.name
    (a project or beneficiary filter) without "missing FROM-clause entry".
    """
    rows = await conn.fetch(
        f"""
        SELECT t.id AS temp_trans_id,
               coalesce(h.name, rh.name, ih.name, t.field_text_5) AS head_name,
               t.field_num_1 AS debit_amount,
               t.field_num_2 AS credit_amount
          FROM temp_trans t
          LEFT JOIN projects            p  ON p.id  = t.project_id
          LEFT JOIN head_master         h  ON h.id  = t.head_id
          LEFT JOIN rera_head_master    rh ON rh.id = t.rera_head_id
          LEFT JOIN idw_head_master     ih ON ih.id = t.idw_head_id
          LEFT JOIN beneficiary_master  bn ON bn.id = t.beneficiary_id
         WHERE {where}
         ORDER BY t.batch_id, t.row_number
        """,
        *params,
    )
    rp_counter = 0
    dw_counter = 0
    out: dict[int, int | None] = {}
    for r in rows:
        head_name = r["head_name"]
        if _skip_document_type(head_name):
            out[r["temp_trans_id"]] = None
            continue
        if _is_internal(head_name):
            if _debit_or_credit(r["debit_amount"], r["credit_amount"]) == "Credit":
                out[r["temp_trans_id"]] = None
                continue
            dw_counter += 1
            out[r["temp_trans_id"]] = dw_counter
        else:
            rp_counter += 1
            out[r["temp_trans_id"]] = rp_counter
    return out


async def fetch_rows(
    conn, where: str, params: list, schema: str,
    limit: int | None = None, offset: int | None = None,
    on_row=None,
) -> list[dict]:
    """limit/offset page the query itself -- used only by the Farvision Verify
    listing, which reviews a batch a page at a time rather than matching every
    row up front. export-farvision never passes these: an export is the whole
    filtered set by definition, confirmed with the user as unchanged behaviour.

    on_row(index, total), if given, is called after each row is matched --
    the Farvision Verify listing's background-job path (routers.transactions'
    farvision_verify_rows) ticks services.jobs with it, so the spinner shown
    while a page loads can report a real, row-by-row percentage instead of
    either nothing or a number invented to fill the wait.

    p/bn are joined here too even though this function never reads them,
    solely so the count query and this one can share exactly the same alias
    set as _TEMP_JOINS -- the search filter _temp_filters can build references
    p.name/bn.name, and a WHERE naming a table this query never joined would
    fail with "column does not exist" the moment a search term reached this
    endpoint. Same failure shape as the p/h/rh/ih/bn joins the staging list
    already carries for the same reason.
    """
    company_col = await staging.company_column(conn)
    company_select = f"t.{company_col} AS company," if company_col else "NULL AS company,"

    page_params = list(params)
    limit_clause = ""
    if limit is not None:
        limit_clause = f"LIMIT ${len(page_params) + 1} OFFSET ${len(page_params) + 2}"
        page_params.extend([limit, offset or 0])

    rows = await conn.fetch(
        f"""
        SELECT t.field_text_4  AS business_unit,
               t.field_text_21 AS financial_year,
               t.field_date_1  AS document_date,
               t.field_text_11 AS narration,
               t.field_text_1  AS desc_text,
               t.field_text_17 AS account_number,
               t.field_num_1   AS debit_amount,
               t.field_num_2   AS credit_amount,
               coalesce(h.name, rh.name, ih.name, t.field_text_5) AS head_name,
               bm.bank_name AS bank_name,
               t.farvision_account_head_override AS account_head_override,
               t.farvision_parent_account_head_override AS parent_account_head_override,
               t.farvision_description_override AS description_override,
               t.farvision_tds_rate_override AS tds_rate,
               t.farvision_debit_amount_override AS debit_amount_override,
               {company_select}
               t.id AS temp_trans_id
          FROM temp_trans t
          LEFT JOIN projects            p  ON p.id  = t.project_id
          LEFT JOIN head_master         h  ON h.id  = t.head_id
          LEFT JOIN rera_head_master    rh ON rh.id = t.rera_head_id
          LEFT JOIN idw_head_master     ih ON ih.id = t.idw_head_id
          LEFT JOIN beneficiary_master  bn ON bn.id = t.beneficiary_id
          LEFT JOIN LATERAL (
                 SELECT b.bank_name
                   FROM bank_master b
                  WHERE ltrim(regexp_replace(coalesce(b.account_number, ''), '\\D', '', 'g'), '0')
                      = ltrim(regexp_replace(coalesce(t.field_text_17, ''), '\\D', '', 'g'), '0')
                    AND ltrim(regexp_replace(coalesce(t.field_text_17, ''), '\\D', '', 'g'), '0') <> ''
                  ORDER BY b.is_active DESC, b.id
                  LIMIT 1
               ) bm ON true
         WHERE {where}
         ORDER BY t.batch_id, t.row_number
         {limit_clause}
        """,
        *page_params,
    )

    # One candidate list per distinct company seen in this batch, fetched
    # once rather than per row -- a batch is usually one bank account and
    # therefore one company, but nothing here assumes that. Prefetched for
    # every company actually present up front, rather than lazily on first
    # encounter inside the row loop below: the loop itself is now plain
    # synchronous Python (see _build_row/on_row below) with no `await` of its
    # own, so anything it needs has to already be in hand before it starts.
    bank_name_candidates = await _cached(
        (schema, "bank_names"), lambda: _bank_name_candidates(conn))
    bank_short_codes = await _cached(
        (schema, "bank_codes"), lambda: _bank_short_codes(conn))
    candidates_by_company: dict[str | None, list[dict]] = {}
    dup_options_by_company: dict[str | None, dict[str, list[str]]] = {}
    for company in {r["company"] for r in rows}:
        candidates_by_company[company] = await _cached(
            (schema, "account_heads", company),
            lambda company=company: _account_head_candidates(conn, company))
        dup_options_by_company[company] = await _cached(
            (schema, "dup_options", company),
            lambda company=company: _duplicate_options_map(conn, company))

    def _build_row(i: int, r) -> dict:
        internal = _is_internal(r["head_name"])
        skip_doc_type = _skip_document_type(r["head_name"])
        if skip_doc_type:
            document_type = None
        else:
            document_type = "Deposit/withdrawal" if internal else "Payment/Reciept"

        tds_description = None
        if r["head_name"]:
            tds_description = _TDS_DESCRIPTION.get(r["head_name"].strip().upper())
        if r["description_override"]:
            # Resolved for good on the Farvision Verify page, same as an
            # Account Head override just below -- always wins over the
            # keyword guess above, confirmed with the user.
            tds_description = r["description_override"]

        company = r["company"]
        bank_name = _match_bank_name(r["account_number"], bank_name_candidates) or r["bank_name"]

        # The pool a "Not correct?" override or a blank row picks from: the
        # Farvision Bank Names for an Internal transfer, this company's
        # Account Heads otherwise -- the same universe each row's own
        # matching already searches.
        if internal:
            pool = bank_name_candidates
        else:
            pool = [c["account_head"] for c in candidates_by_company[company]]

        account_head_options = None

        if r["account_head_override"]:
            # Resolved for good on the Farvision Verify page -- always wins
            # over re-matching, confirmed with the user, so a batch already
            # reviewed once stays resolved on every later export.
            account_head = r["account_head_override"]
            parent_account_head = r["parent_account_head_override"]
        elif internal:
            # A transfer between the company's own bank accounts has no
            # external party at all -- Account Head is the specific bank
            # account on the other side of the transfer instead, confirmed
            # with the user after whole-DESC matching against the Account
            # Head master started false-matching the company's own legal
            # name in transfer routing text.
            account_head, account_head_options = _match_internal_account_head(
                r["desc_text"], bank_name_candidates, bank_short_codes)
            parent_account_head = None
        else:
            account_head, parent_account_head, despaced_options = _match_account_head(
                r["desc_text"], candidates_by_company[company], narration=r["narration"])

            if despaced_options:
                # More than one candidate's name reassembled from the same
                # broken DESC text (see _match_account_head's second pass) --
                # already a genuine ambiguity list, same shape as the
                # duplicate-spelling case just below.
                account_head_options = despaced_options
            else:
                # A match that landed on a head with known duplicates ("India
                # Pride Com" / "INDIA PRIDE.COM") is left blank rather than
                # guessed -- the Farvision Verify page offers every spelling in
                # the group instead, confirmed with the user.
                account_head_options = dup_options_by_company[company].get(account_head)
                if account_head_options:
                    # Parent Account Head can differ between spellings in the
                    # same group (confirmed live -- two duplicate rows for the
                    # same party carried different Parent text), so it is just as
                    # ambiguous as Account Head itself and left blank the same way.
                    account_head = None
                    parent_account_head = None

        # Whenever there's no strict ambiguous list already (a confident
        # match that still deserves a "Not correct?" override, or a
        # genuinely blank row with no duplicate/number/code signal at all),
        # fall back to the closest keyword matches against DESC -- confirmed
        # with the user as the general answer, rather than either free-text
        # search or the full company-wide master.
        #
        # A row with no keyword overlap at all used to fall back to the FULL
        # pool (up to ~7,900 Account Heads) copied onto that one row -- with
        # a batch of a few hundred rows landing here, that meant megabytes of
        # duplicate strings and, worse, re-scoring the whole master per row.
        # Left None instead: the Farvision Verify page already has the full
        # pool once (GET .../farvision-verify/candidates, fetched separately
        # and cached), and falls back to that itself when a row's own
        # "options" comes back empty.
        #
        # When even that shares no whole keyword at all -- a name broken
        # badly enough by a stray extraction space that no complete word
        # survives on either side ("RAHU L KUMAR") -- _same_letter_options
        # is one narrower tier below the full pool: candidates sharing just
        # the first letter of some real word in DESC, confirmed with the
        # user as their own ask for exactly this situation.
        if not account_head_options:
            account_head_options = (
                _closest_matches(r["desc_text"], pool)
                or _same_letter_options(r["desc_text"], pool)
                or None
            )

        # Whether this row needs a decision on the Farvision Verify page, or
        # is only offered for an optional "Not correct?" override -- the page
        # shows every row either way, confirmed with the user.
        account_head_matched = account_head is not None

        return {
            # Not real columns -- to_xlsx_bytes only ever reads COLUMNS, so
            # these ride along harmlessly for callers that want them (the
            # Farvision Verify endpoint, to show every row's current state).
            "_temp_trans_id": r["temp_trans_id"],
            "_account_head_matched": account_head_matched,
            "_account_head_options": account_head_options,
            # So a caller whose row got None above knows which shared pool to
            # fall back to: the bank-name pool for an Internal row, this
            # row's own company's Account Head pool otherwise.
            "_internal": internal,
            "_company": company,
            # The raw bank statement Description behind this row's match --
            # not an export column, just what the Farvision Verify page shows
            # on demand next to Narration so the user can sanity-check a
            # match without it ever reaching the exported sheet.
            "_desc_text": r["desc_text"],
            # A person's own manually-typed TDS Rate note (e.g. "1%") -- not
            # an export column either, purely a Farvision Verify page field,
            # confirmed with the user.
            "_tds_rate": r["tds_rate"],
            "Link Ref Code": i,
            "Business Unit": _format_business_unit(r["business_unit"]),
            "Financial Year": _format_financial_year(r["financial_year"]),
            "Document Type": document_type,
            "Document Date": r["document_date"],
            "Document No": None,
            "Narration": r["narration"],
            "BankName": bank_name,
            "EntryTypes": document_type,
            "Detail Link Ref Code": i,
            "Debit/Credit": _debit_or_credit(r["debit_amount"], r["credit_amount"]),
            "Account Head": account_head,
            "Parent Account Head": parent_account_head,
            # 0 is treated the same as NULL here, same as _debit_or_credit --
            # a placeholder zero alongside the row's real amount on the other
            # side should export blank, not a literal 0, confirmed with the
            # user. A TDS Rate override (farvision_debit_amount_override,
            # computed by the resolve-tds-rate endpoint) wins over the raw
            # amount when set -- the reverse-calculated gross, not the net
            # actually paid. Credit Amount is never touched by it.
            "Debit Amount": r["debit_amount_override"] or r["debit_amount"] or None,
            "Credit Amount": r["credit_amount"] or None,
            "Payment Mode": "Direct",
            "Cheque No": None,
            "Cheque Date": None,
            "Cheque Type": None,
            "Payee Name": account_head,
            "Beneficiary": None,
            "Card Type": None,
            "Print Cheque": None,
            "Sub Project": None,
            "Budget": None,
            "Zone": None,
            "Department": None,
            "Order": None,
            "Milestone": None,
            "Tower": None,
            "Segment": None,
            "Employee": None,
            "Employee Name": None,
            "Department Name": None,
            "Cost Center": None,
            "Purpose Of Payment": None,
            "Deduction Type": "Tax deducted at source" if tds_description else None,
            "Description": tds_description,
            "Docno": "ON A/c",
            "Date": r["document_date"],
            "Invoice No": "Normal",
            "Invoice Date": r["document_date"],
            "Bill Amount": None,
            "Balance Amount": None,
            # A TDS Rate override always wins here too, and unconditionally --
            # not gated on Parent Account Head like the fallback below --
            # confirmed with the user: Adjustment Amount must equal the same
            # grossed-up figure as Debit Amount above whenever a rate was
            # picked. Otherwise unchanged: skipped when Parent Account Head
            # is blank (nothing to adjust against), else whichever of
            # Debit/Credit is the row's real amount.
            "Adjustment Amount": (
                r["debit_amount_override"] if r["debit_amount_override"] is not None
                else (r["debit_amount"] or r["credit_amount"]) if parent_account_head
                else None
            ),
        }

    def _build_all() -> list[dict]:
        # Run off the event loop (see fetch_rows's own asyncio.to_thread call
        # below) -- matching a page of rows against a ~7,900-row master is
        # real CPU work (measured ~40ms/row), and doing it inline on the loop
        # would block every other request, including a caller polling this
        # same job's progress, for the whole page.
        out = []
        for i, r in enumerate(rows, start=1):
            out.append(_build_row(i, r))
            if on_row is not None:
                on_row(i, len(rows))
        return out

    return await asyncio.to_thread(_build_all)


_DATE_COLUMNS = {"Document Date", "Date", "Invoice Date"}


def _renumbered(rows: list[dict]) -> list[dict]:
    """Link Ref Code / Detail Link Ref Code, reassigned 1..N for this subset.

    Each export is its own workbook containing only its own rows, so the
    join key should read as a clean sequence for that file -- confirmed with
    the user's own Receipt Payment / Deposit Withdrawal split -- rather than
    keeping the gaps left by whichever rows the other export took.
    """
    out = []
    for i, row in enumerate(rows, start=1):
        r = dict(row)
        r["Link Ref Code"] = i
        r["Detail Link Ref Code"] = i
        out.append(r)
    return out


def filter_receipt_payment(rows: list[dict]) -> list[dict]:
    return _renumbered([r for r in rows if r["Document Type"] == "Payment/Reciept"])


def filter_deposit_withdrawal(rows: list[dict]) -> list[dict]:
    # An internal transfer between the company's own bank accounts is two
    # temp_trans rows -- a Debit leg out of the source account and a Credit
    # leg into the destination account -- both "Deposit/withdrawal". Only the
    # Debit leg belongs in this export, confirmed with the user: the Credit
    # leg is not wanted here at all, not even as a row to review.
    return _renumbered([
        r for r in rows
        if r["Document Type"] == "Deposit/withdrawal" and r["Debit/Credit"] != "Credit"
    ])


EXPORT_STATUS_ON = "Yes"
EXPORT_STATUS_OFF = "No"


async def mark_exported(conn, temp_trans_ids: list[int]) -> None:
    """Flip the Export Status custom field on for exactly the rows that were
    actually written to an export file -- called right after
    _build_farvision_export renders one, with that render's own final
    (filtered, renumbered) row list's ids, confirmed with the user. A row
    the export left out (a Credit leg excluded from Deposit Withdrawal, or
    one an active Debit/Credit filter excluded) is correctly not marked --
    it was not exported.

    A no-op, not an error, when the field doesn't exist in this schema --
    the same "callers handle None" contract staging.company_column already
    uses for an optional custom field.
    """
    if not temp_trans_ids:
        return
    col = await staging.export_status_column(conn)
    if col is None:
        return
    await conn.execute(
        f"UPDATE temp_trans SET {col} = $1 WHERE id = ANY($2::bigint[])",
        EXPORT_STATUS_ON, temp_trans_ids,
    )


async def reset_export_status(conn, where: str, params: list) -> int:
    """Turn Export Status back off for every row the Farvision Verify page's
    current filters cover -- the "Reset Export Status" button, for
    re-exporting a batch on purpose. Scoped to the same WHERE the page
    itself is filtered by, not the whole schema, confirmed with the user:
    resetting is a decision about what's on screen, not a blanket wipe of
    every row this company has ever exported.

    Returns how many rows changed (0, harmlessly, when the field doesn't
    exist in this schema).

    where can reference p/h/rh/ih/bn (a project, head, or beneficiary
    filter) -- those aliases don't exist on a bare "UPDATE temp_trans t SET
    ... WHERE {where}", and joining them directly into the UPDATE would turn
    _TEMP_JOINS' LEFT JOINs into an implicit INNER JOIN, wrongly dropping
    any row whose head_id/project_id/beneficiary_id is NULL. The subquery
    keeps the same LEFT JOIN semantics fetch_rows/COUNT already use, and the
    UPDATE only ever touches temp_trans by id.
    """
    col = await staging.export_status_column(conn)
    if col is None:
        return 0
    # EXPORT_STATUS_OFF is bound as the placeholder *after* params, not
    # before -- where's own $1.. references already assume that exact
    # position (they were numbered against params when _temp_filters built
    # it), so binding anything ahead of them would shift every one of those
    # references onto the wrong value.
    off_placeholder = f"${len(params) + 1}"
    result = await conn.execute(
        f"""
        UPDATE temp_trans t
           SET {col} = {off_placeholder}
          FROM (
                SELECT t.id
                  FROM temp_trans t
                  LEFT JOIN projects            p  ON p.id  = t.project_id
                  LEFT JOIN head_master         h  ON h.id  = t.head_id
                  LEFT JOIN rera_head_master    rh ON rh.id = t.rera_head_id
                  LEFT JOIN idw_head_master     ih ON ih.id = t.idw_head_id
                  LEFT JOIN beneficiary_master  bn ON bn.id = t.beneficiary_id
                 WHERE {where}
               ) matched
         WHERE t.id = matched.id AND t.{col} IS DISTINCT FROM {off_placeholder}
        """,
        *params, EXPORT_STATUS_OFF,
    )
    # asyncpg's execute() returns "UPDATE <n>" -- the count is the only thing
    # worth parsing back out of it.
    return int(result.split()[-1])


def to_xlsx_bytes(rows: list[dict], sheets: dict[str, list[str]] = SHEETS) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)  # the default blank sheet every new Workbook starts with

    for sheet_name, columns in sheets.items():
        ws = wb.create_sheet(sheet_name)
        ws.append(columns)
        for row in rows:
            ws.append([row.get(_COLUMN_ALIASES.get(col, col)) for col in columns])

        # A date cell shows ##### when the column is narrower than its format
        # needs, not when the value is wrong -- Excel's default datetime
        # format is wider than the DD-MM-YYYY this only needs, so both are
        # fixed together.
        for i, name in enumerate(columns, start=1):
            letter = get_column_letter(i)
            if name in _DATE_COLUMNS:
                for cell in ws[letter][1:]:
                    if cell.value is not None:
                        cell.number_format = "DD-MM-YYYY"
            ws.column_dimensions[letter].width = 12 if name in _DATE_COLUMNS else max(len(name) + 2, 10)

    # Info is a 6th tab on the Receipt Payment workbook only -- confirmed
    # with the user -- static reference data with nothing to do with the
    # rows being exported, so it never appears on the Deposit Withdrawal one.
    info_rows = INFO_SHEET_ROWS if sheets is SHEETS else INFO_SHEET_ROWS_DW if sheets is DW_SHEETS else None
    if info_rows is not None:
        info_columns = ["Sheet Name", "Column Name", "Property(ies)"]
        ws = wb.create_sheet("Info")
        ws.append(info_columns)
        for sheet_name, column_name, properties in info_rows:
            ws.append([sheet_name, column_name, properties])
        for i, name in enumerate(info_columns, start=1):
            ws.column_dimensions[get_column_letter(i)].width = max(len(name) + 2, 20)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
