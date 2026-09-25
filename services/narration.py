"""
Auto-generated NARRATION text for a staged row, ported line-for-line from the
accountant's own Excel formula (a LET() keyed off Description, Reference,
Credit/Debit, Business Unit, Head, Type for RERA IDW, Apt# and ACC Remarks).

Kept as one pure function so the port can be checked against the formula's own
branches directly, without any database or fieldmap concerns leaking in --
those live in the caller, which resolves each argument to its physical column
before calling this.

Excel's SEARCH() is case-insensitive; every text comparison below lower()s
both sides to match. The formula also computed a `vendor` variable (text after
the first "/" in the description) that nothing in its own output ever uses --
dropped here rather than ported, since it is dead in the original.
"""
from __future__ import annotations

import re


def _blank(value: str | None) -> bool:
    return value is None or str(value).strip() in ("", "-")


def _last4_after_third_dash(description: str) -> str:
    """RIGHT(MID(desc, FIND(3rd "-") + 1, LEN(desc)), 4) -- the last 4 characters
    of whatever follows the third dash. Excel's RIGHT() on a short string just
    returns the whole thing; str[-4:] does the same."""
    parts = description.split("-", 3)
    tail = parts[3] if len(parts) > 3 else ""
    return tail[-4:]


def _extract_counterparty(description: str) -> str:
    """The "To: " name on a Payment Disbursement line.

    Slash-delimited (UPI-style): the segment right after "FINO PAYMENTS", or
    the 7th slash-separated segment, or (if that doesn't exist) everything
    after the last slash.

    Dash-delimited (NEFT/RTGS-style): the text between the 3rd and 4th dash.

    "Vendor" if none of the above find anything, same as the formula's own
    final fallback.
    """
    if "/" in description:
        lowered = description.lower()
        marker = "fino payments"
        if marker in lowered:
            start = lowered.index(marker) + len(marker)
            rest = description[start:]
            rest = rest.lstrip("/")
            return rest.split("/", 1)[0].strip() or "Vendor"
        segments = description.split("/")
        if len(segments) >= 7:
            return segments[6].strip() or "Vendor"
        return segments[-1].strip() or "Vendor"

    parts = description.split("-")
    if len(parts) > 4:
        return parts[3].strip() or "Vendor"
    return "Vendor"


def build_narration(
    *,
    description: str | None,
    reference_no: str | None,
    is_credit: bool,
    business_unit: str | None,
    head: str | None,
    type_rera_idw: str | None,
    apt: str | None,
    remarks: str | None,
    internal_transfer_override: dict | None = None,
) -> str:
    """The NARRATION text for one row.

    The spreadsheet formula refused to produce anything when Remarks was
    blank ("Remarks Compulsory For Narration"). Dropped here per the user --
    a blank Remarks just flows through as an empty string wherever the
    formula would have used it, same as any other blank input field.

    `internal_transfer_override` is a Narration Rule's answer for the ONE
    thing this function otherwise has to guess -- the "(From X to Y)" leg
    name inside the Internal Transfer branch, normally built by pulling the
    last 4 characters out of the description. {"from_label", "to_label"} when
    a rule matched this row (resolved by the caller, which has the database
    connection this pure function does not), None otherwise. Nothing else in
    the line is affected by it.
    """
    description = description or ""
    ref = "N/A" if _blank(reference_no) else str(reference_no)
    bu = business_unit or ""
    head = head or ""
    type_ = type_rera_idw or ""
    remarks = "" if _blank(remarks) else str(remarks)
    apt_suffix = "" if _blank(apt) else f" | Apt: {apt}"

    if head.strip().lower() == "internal":
        if description.count("-") >= 3:
            if internal_transfer_override:
                leg = (f'(From {internal_transfer_override["from_label"]} '
                      f'to {internal_transfer_override["to_label"]})')
            else:
                last4 = _last4_after_third_dash(description)
                leg = (f"(From x{last4} to YES IDW 0490)" if is_credit
                      else f"(From YES IDW 0490 to x{last4})")
            lead = f"Internal Fund Transfer {leg}"
            result = f"{lead} | Ref: {ref} | Type: {type_} | BU: {bu} | Head: {head}"
        else:
            result = f"Internal Transfer | Ref: {ref} | Type: {type_} | BU: {bu} | Head: {head}"
        return result.strip()

    if is_credit:
        if type_.strip().lower() == "customer collection":
            from_whom = "Sohna Road" if "sohna road" in description.lower() else "Party"
            result = (
                f"Receipt Credit (Collection) {apt_suffix} | Ref: {ref} "
                f"| From: {from_whom} | BU: {bu}"
            )
        else:
            last4 = _last4_after_third_dash(description)
            purpose = head if _blank(remarks) or remarks.strip().lower() == "n/a" else remarks
            result = (
                f"Receipt Credit from x{last4} (Purpose: {purpose}) {apt_suffix} "
                f"| Ref: {ref} | BU: {bu} | Head: {head}"
            )
        return result.strip()

    purpose = "Salary" if "salary" in head.lower() else remarks
    counterparty = _extract_counterparty(description) if description else "Vendor"
    result = (
        f"Payment Disbursement (Purpose: {purpose}) | To: {counterparty} "
        f"| Ref: {ref} | BU: {bu} | Head: {head}{apt_suffix}"
    )
    return result.strip()
