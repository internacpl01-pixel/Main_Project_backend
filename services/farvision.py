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

Account Head / Parent Account Head are left blank for every row until the
Farvision chart-of-accounts master (thousands of party-level ledger names) is
imported and a fuzzy narration match is wired in against it -- there is
nowhere else that answer can honestly come from yet.
"""
from openpyxl import Workbook
from openpyxl.utils import get_column_letter
from io import BytesIO

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


async def fetch_rows(conn, where: str, params: list) -> list[dict]:
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
               bm.bank_name AS bank_name
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

        amount = r["debit_amount"] if r["debit_amount"] else r["credit_amount"]

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
            # Account Head / Parent Account Head: pending the Farvision chart
            # of accounts master + narration fuzzy match. Left blank rather
            # than guessed.
            "Account Head": None,
            "Parent Account Head": None,
            "Debit Amount": r["debit_amount"],
            "Credit Amount": r["credit_amount"],
            "Payment Mode": "Direct",
            "Cheque No": None,
            "Cheque Date": None,
            "Cheque Type": None,
            # Mirrors Account Head once that is wired in.
            "Payee Name": None,
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
            # Adjustment Amount is skipped (left blank) when Parent Account
            # Head is blank -- which it always is until the master lands.
            "Adjustment Amount": None,
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
