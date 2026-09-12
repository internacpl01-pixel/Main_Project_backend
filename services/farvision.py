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
import re

from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from io import BytesIO

from services import staging

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

# The full field list fetch_rows builds per row, independent of how to_xlsx_bytes
# later splits it across sheets -- a deduplicated union of SHEETS rather than a
# second hand-written list, so the two can never drift apart.
COLUMNS = list(dict.fromkeys(col for cols in SHEETS.values() for col in cols))

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
    """"Debit" when the row has a debit amount, "Credit" otherwise.

    field_text_19 (temp_trans's own Debit/Credit text) is often blank --
    confirmed against a real batch where it was NULL on every row. Debit
    Amount and Credit Amount are never both set and never both blank on a
    real row, so which one is present already says which this is; no
    guessing needed when field_text_19 is missing.
    """
    if debit_amount is not None:
        return "Debit"
    if credit_amount is not None:
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
      2. A short bank code found in DESC -- offered as a dropdown of every
         Farvision Bank Name sharing that code, since several accounts can.
      3. Every Farvision Bank Name, as a last-resort dropdown, when DESC gave
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

    desc_upper = (desc_text or "").upper()
    by_code = {
        b for code in short_codes if code and code.upper() in desc_upper
        for b in bank_names if code.upper() in b.upper()
    }
    if by_code:
        return None, sorted(by_code)

    closest = _closest_matches(desc_text, bank_names)
    return None, (closest or sorted(bank_names))


_ACCOUNT_TABLES = {"DPL": "farvision_account_master_dpl", "AMB": "farvision_account_master_amb"}


async def _account_head_candidates(conn, company: str | None) -> list[dict]:
    """This company's Account Heads, longest name first.

    Longest-first means the first substring hit while scanning in order is
    also the most specific one -- the same reasoning the Rules engine uses:
    a bare, generic head name should not win over one that actually names the
    party. Looked up from the row's own company's table, so 'INTEREST' in
    DPL's books cannot match an AMB row and vice versa. A company that isn't
    DPL or AMB has no table to match against.
    """
    table = _ACCOUNT_TABLES.get((company or "").strip().upper())
    if not table:
        return []
    rows = await conn.fetch(f"SELECT account_head, parent_account_head FROM {table}")
    candidates = [dict(r) for r in rows]
    for c in candidates:
        c["_norm"] = _normalize_party_name(c["account_head"])
    return sorted(candidates, key=lambda r: -len(r["_norm"]))


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


def _normalize_party_name(text: str | None) -> str:
    if not text:
        return ""
    words = re.sub(r"[^\w\s]", " ", text.upper()).split()
    out = []
    for w in words:
        w = _SUFFIX_MAP.get(w, w)
        if len(w) > 3 and w.endswith("S") and w not in ("LTD", "PVT"):
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


# Below this normalized length, a head name is too generic to trust as a
# plain substring match -- confirmed against two real false positives once
# punctuation-stripping was added: "CR--" (normalizes to "CR", 2 chars)
# matched almost every narration mentioning the "YES CR FREE" bank account,
# and "Car" (3 chars) matched "Credit Card" (its own letters are a substring
# of "Card"). Real 5+ letter names (people's names, "HDFC", "BONUS", ...)
# still match fine; a genuine match this short just isn't distinguishable
# from an accidental one and is left blank instead of guessed.
_MIN_MATCH_LENGTH = 5


def _match_account_head(desc_text: str | None, candidates: list[dict]) -> tuple[str | None, str | None]:
    """Search temp_trans's own DESC field (field_text_1), the real bank text.

    NARRATION (field_text_11) looked like it should be the search source, but
    it is a synthetic field -- confirmed against real data where it was
    frequently just the literal placeholder text "Remarks Compulsory For
    Narration", not the bank's actual description, and even when populated it
    is built from other classified fields, so matching against it would be
    circular. DESC is the raw hyphen-joined bank text (e.g. "YIB-NEFT-
    YESME62170041087-INDIA PRIDE COM-CNRB0001565-VENDOR- CANARA BANK")
    confirmed by the user's own screenshot of it, and is searched whole --
    there is no "To:"/"Purpose:" structure in DESC to isolate a party
    segment from, unlike the synthetic Narration.
    """
    if not desc_text:
        return None, None
    search_text = _normalize_party_name(desc_text)
    if not search_text:
        return None, None
    for c in candidates:
        if len(c["_norm"]) >= _MIN_MATCH_LENGTH and c["_norm"] in search_text:
            return c["account_head"], c["parent_account_head"]
    return None, None


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


async def fetch_rows(conn, where: str, params: list) -> list[dict]:
    company_col = await staging.company_column(conn)
    company_select = f"t.{company_col} AS company," if company_col else "NULL AS company,"

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
               {company_select}
               t.id AS temp_trans_id
          FROM temp_trans t
          LEFT JOIN head_master      h  ON h.id  = t.head_id
          LEFT JOIN rera_head_master rh ON rh.id = t.rera_head_id
          LEFT JOIN idw_head_master  ih ON ih.id = t.idw_head_id
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
        """,
        *params,
    )

    # One candidate list per distinct company seen in this batch, fetched
    # once rather than per row -- a batch is usually one bank account and
    # therefore one company, but nothing here assumes that.
    candidates_by_company: dict[str | None, list[dict]] = {}
    dup_options_by_company: dict[str | None, dict[str, list[str]]] = {}
    bank_name_candidates = await _bank_name_candidates(conn)
    bank_short_codes = await _bank_short_codes(conn)

    out = []
    for i, r in enumerate(rows, start=1):
        internal = _is_internal(r["head_name"])
        skip_doc_type = _skip_document_type(r["head_name"])
        if skip_doc_type:
            document_type = None
        else:
            document_type = "Deposit/withdrawal" if internal else "Payment/Reciept"

        tds_description = None
        if r["head_name"]:
            tds_description = _TDS_DESCRIPTION.get(r["head_name"].strip().upper())

        company = r["company"]
        bank_name = _match_bank_name(r["account_number"], bank_name_candidates) or r["bank_name"]

        # The pool a "Not correct?" override or a blank row picks from: the
        # Farvision Bank Names for an Internal transfer, this company's
        # Account Heads otherwise -- the same universe each row's own
        # matching already searches.
        if internal:
            pool = bank_name_candidates
        else:
            if company not in candidates_by_company:
                candidates_by_company[company] = await _account_head_candidates(conn, company)
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
            if company not in dup_options_by_company:
                dup_options_by_company[company] = await _duplicate_options_map(conn, company)
            account_head, parent_account_head = _match_account_head(
                r["desc_text"], candidates_by_company[company])

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
        # search or the full company-wide master. A row with no keyword
        # overlap at all (an already-resolved row's override text rarely
        # echoes its own DESC, for one) still gets the full pool rather than
        # an empty dropdown with nothing to re-pick from.
        if not account_head_options:
            account_head_options = _closest_matches(r["desc_text"], pool) or sorted(pool)

        # Whether this row needs a decision on the Farvision Verify page, or
        # is only offered for an optional "Not correct?" override -- the page
        # shows every row either way, confirmed with the user.
        account_head_matched = account_head is not None

        out.append({
            # Not real columns -- to_xlsx_bytes only ever reads COLUMNS, so
            # these ride along harmlessly for callers that want them (the
            # Farvision Verify endpoint, to show every row's current state).
            "_temp_trans_id": r["temp_trans_id"],
            "_account_head_matched": account_head_matched,
            "_account_head_options": account_head_options,
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
            "Debit Amount": r["debit_amount"],
            "Credit Amount": r["credit_amount"],
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
            # Skipped when Parent Account Head is blank -- there is nothing to
            # adjust against. Otherwise whichever of Debit/Credit is the row's
            # real amount (only one of the two is ever set).
            "Adjustment Amount": (
                (r["debit_amount"] or r["credit_amount"])
                if parent_account_head else None
            ),
        })
    return out


_DATE_COLUMNS = {"Document Date", "Date", "Invoice Date"}


def to_xlsx_bytes(rows: list[dict]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)  # the default blank sheet every new Workbook starts with

    for sheet_name, columns in SHEETS.items():
        ws = wb.create_sheet(sheet_name)
        ws.append(columns)
        for row in rows:
            ws.append([row.get(col) for col in columns])

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

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
