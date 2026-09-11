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

Account Head and Parent Account Head come from farvision_account_master_dpl or
_amb, one table per company: the row's Narration (field_text_11, which
already embeds "To: <party>" phrases) is searched for the longest Account
Head string that appears in it, using only the table for the row's own
Company -- DPL and AMB share this company_028 schema and each have their own
ledger names, so matching against the wrong table could pick the wrong
company's party of the same name. A row whose Company doesn't resolve to
either table gets no match at all, rather than guessing. Parent Account Head
and Payee Name simply come along with whichever Account Head matched. No
match found means both stay blank rather than guessed.
"""
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from io import BytesIO

from services import staging

COLUMNS = [
    "Link Ref Code", "Business Unit", "Financial Year", "Document Type",
    "Document Date", "Document No", "Narration", "BankName", "EntryTypes",
    "Detail Link Ref Code", "Debit/Credit", "Account Head",
    "Parent Account Head", "Debit Amount", "Credit Amount", "Payment Mode",
    "Cheque No", "Cheque Date", "Cheque Type", "Payee Name", "Beneficiary",
    "Card Type", "Print Cheque", "Sub Project", "Budget", "Zone",
    "Department", "Order", "Milestone", "Tower", "Segment", "Employee",
    "Employee Name", "Department Name", "Cost Center", "Purpose Of Payment",
    "Deduction Type", "Description", "Docno", "Date", "Invoice No",
    "Invoice Date", "Bill Amount", "Balance Amount", "Adjustment Amount",
]

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
    return sorted((dict(r) for r in rows), key=lambda r: -len(r["account_head"] or ""))


def _match_account_head(narration: str | None, candidates: list[dict]) -> tuple[str | None, str | None]:
    if not narration:
        return None, None
    upper = narration.upper()
    for c in candidates:
        head = c["account_head"]
        if head and head.upper() in upper:
            return head, c["parent_account_head"]
    return None, None


async def fetch_rows(conn, where: str, params: list) -> list[dict]:
    company_col = await staging.company_column(conn)
    company_select = f"t.{company_col} AS company," if company_col else "NULL AS company,"

    rows = await conn.fetch(
        f"""
        SELECT t.field_text_4  AS business_unit,
               t.field_text_21 AS financial_year,
               t.field_date_1  AS document_date,
               t.field_text_11 AS narration,
               t.field_text_17 AS account_number,
               t.field_text_19 AS debit_credit,
               t.field_num_1   AS debit_amount,
               t.field_num_2   AS credit_amount,
               coalesce(h.name, rh.name, ih.name) AS head_name,
               bm.bank_name AS bank_name,
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
        if company not in candidates_by_company:
            candidates_by_company[company] = await _account_head_candidates(conn, company)
        account_head, parent_account_head = _match_account_head(
            r["narration"], candidates_by_company[company])

        out.append({
            "Link Ref Code": i,
            "Business Unit": r["business_unit"],
            "Financial Year": r["financial_year"],
            "Document Type": document_type,
            "Document Date": r["document_date"],
            "Document No": None,
            "Narration": r["narration"],
            "BankName": r["bank_name"],
            "EntryTypes": document_type,
            "Detail Link Ref Code": i,
            "Debit/Credit": r["debit_credit"],
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
            "Deduction Type": "Tax deducted at source" if internal else None,
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
    ws = wb.active
    ws.title = "Farvision"
    ws.append(COLUMNS)
    for row in rows:
        ws.append([row.get(col) for col in COLUMNS])

    # A date cell shows ##### when the column is narrower than its format
    # needs, not when the value is wrong -- Excel's default datetime format is
    # wider than the DD-MM-YYYY this only needs, so both are fixed together.
    for i, name in enumerate(COLUMNS, start=1):
        letter = get_column_letter(i)
        if name in _DATE_COLUMNS:
            for cell in ws[letter][1:]:
                if cell.value is not None:
                    cell.number_format = "DD-MM-YYYY"
        ws.column_dimensions[letter].width = 12 if name in _DATE_COLUMNS else max(len(name) + 2, 10)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()
