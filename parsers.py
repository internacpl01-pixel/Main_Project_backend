"""
Generic PDF statement parser.
Uses pdfplumber table extraction + word-coordinate fallback.
No bank-specific logic — fieldmap table drives all column mapping.
"""
from __future__ import annotations

import io
import re
import logging
import time

logger = logging.getLogger(__name__)

try:
    from pypdf import PdfReader, PdfWriter
    PYPDF_AVAILABLE = True
except ImportError:
    PYPDF_AVAILABLE = False

try:
    import pdfplumber
    PDFPLUMBER_AVAILABLE = True
except ImportError:
    PDFPLUMBER_AVAILABLE = False


# ── PDF text extraction ─────────────────────────────────────────────────────

def check_pdf_protected(file_bytes: bytes) -> bool:
    if not PYPDF_AVAILABLE:
        return False
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        return reader.is_encrypted
    except Exception:
        return False


def decrypt_pdf(file_bytes: bytes, password: str) -> bytes:
    if not password:
        raise RuntimeError(
            "ENCRYPTED: This PDF is password-protected. "
            "Please provide the password to proceed."
        )
    if not PYPDF_AVAILABLE:
        raise RuntimeError("pypdf is required for PDF decryption")
    try:
        reader = PdfReader(io.BytesIO(file_bytes))
        result = reader.decrypt(password)
        if result == 0:
            raise RuntimeError("Incorrect password. Please try again.")
        try:
            _ = reader.pages[0].extract_text()
        except Exception:
            raise RuntimeError("Incorrect password or corrupted PDF. Please try again.")
        out = io.BytesIO()
        writer = PdfWriter()
        for page in reader.pages:
            writer.add_page(page)
        writer.write(out)
        return out.getvalue()
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Failed to decrypt PDF: {e}")


def _extract_pages_text(file_bytes: bytes) -> list:
    """Extract text per page. Returns a list of page-text strings."""
    if not PDFPLUMBER_AVAILABLE:
        raise RuntimeError("pdfplumber is not installed")
    pages_text = []
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                txt = page.extract_text()
                if txt:
                    pages_text.append(txt)
    except Exception as e:
        err_msg = str(e).lower()
        if "password" in err_msg or "encrypt" in err_msg or "decrypt" in err_msg:
            raise RuntimeError("ENCRYPTED: This PDF is password-protected. Please provide the password.")
        raise
    return pages_text


def extract_text_from_pdf(file_bytes: bytes) -> str:
    return "\n".join(_extract_pages_text(file_bytes))


# ── Helpers ─────────────────────────────────────────────────────────────────

def _clean_amount(val) -> str:
    """Strip currency symbols, commas, spaces from an amount."""
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    # Strip currency prefixes (full tokens, not char-by-char)
    s = re.sub(r"^(Rs\.?|INR|₹)\s*", "", s, flags=re.IGNORECASE)
    # Strip commas and whitespace
    s = re.sub(r"[,\s]", "", s)
    # Handle Dr/Cr suffix
    if s.upper().endswith("DR") and not s.endswith("-"):
        s = "-" + s[:-2].strip()
    elif s.upper().endswith("CR"):
        s = s[:-2].strip()
    return s.strip()


def _parse_date(val) -> str:
    """Normalize a date string to ISO format."""
    if val is None:
        return ""
    s = str(val).strip()
    if not s:
        return ""
    # Already ISO
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    # DD/MM/YYYY, DD-MM-YYYY — also 2-digit years (DD/MM/YY → 20YY)
    m = re.match(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})(?!\d)", s)
    if m:
        year = m.group(3)
        if len(year) == 3:
            return s  # ambiguous 3-digit year — leave unparsed
        if len(year) == 2:
            year = "20" + year
        return f"{year}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    # DD-Mon-YYYY, DD Mon YYYY, DD-Mon-YY (hyphen or space separated)
    m = re.match(r"(\d{1,2})[\s\-]+([A-Za-z]{3,})[\s\-,]+(\d{2,4})", s)
    if m:
        month_map = {
            "jan": "01", "feb": "02", "mar": "03", "apr": "04",
            "may": "05", "jun": "06", "jul": "07", "aug": "08",
            "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        }
        mon = month_map.get(m.group(2).lower()[:3])
        if mon:
            year = m.group(3)
            if len(year) == 2:
                year = "20" + year
            return f"{year}-{mon}-{int(m.group(1)):02d}"
    return s


def _parse_date_to_date(val):
    """Parse date string to datetime.date object for asyncpg."""
    s = _parse_date(val)
    if not s:
        return None
    try:
        from datetime import date
        parts = s.split("-")
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except Exception:
        return None


# ── Normalization ───────────────────────────────────────────────────────────

def _normalize_for_matching(s: str) -> str:
    """Normalize a string for alias matching: lowercase, strip punctuation/underscores, collapse spaces."""
    s = s.strip().lower()
    s = re.sub(r"[^\w\s]", "", s)  # remove punctuation
    s = re.sub(r"_", " ", s)       # underscores → spaces
    s = re.sub(r"\s+", " ", s)     # collapse spaces
    return s.strip()


def _build_alias_map(fieldmap_rows: list) -> dict:
    """Build {normalized_alias: fieldname} from fieldmap rows.

    Aliases come from three sources:
      • mapfields  — user-supplied aliases (PDF header text, bank-specific names)
      • displayname — human-readable name (e.g. "Account No")
      • fieldname  — the actual DB column name (e.g. "field_num_1")

    All three are normalized and added to the alias map so header matching
    works even with no custom configuration. Explicit mapfields aliases take
    priority; displayname/fieldname-derived aliases never overwrite an
    existing entry (so a custom field displaynamed e.g. "Date" cannot
    hijack a core column).
    """
    alias_map = {}
    derived = []
    for row in (fieldmap_rows or []):
        fieldname = row.get("fieldname", "")
        for alias in (row.get("mapfields", "") or "").split(","):
            norm = _normalize_for_matching(alias)
            if norm:
                alias_map[norm] = fieldname
        for cand in (row.get("displayname", ""), fieldname):
            norm = _normalize_for_matching(cand or "")
            if norm:
                derived.append((norm, fieldname))
    for norm, fieldname in derived:
        alias_map.setdefault(norm, fieldname)
    return alias_map


def _match_alias(header: str, alias_map: dict) -> tuple:
    """
    Match a PDF column header against the alias map.
    Priority: exact > starts-with > contains. Longest alias first.
    Returns (fieldname, confidence) or (None, 0).
    """
    norm = _normalize_for_matching(header)
    if not norm:
        return None, 0

    # Sort aliases by length descending for longest-match-first
    sorted_aliases = sorted(alias_map.keys(), key=len, reverse=True)

    # 1. Exact match
    if norm in alias_map:
        return alias_map[norm], 3

    # 2. Starts-with
    for alias in sorted_aliases:
        if norm.startswith(alias) or alias.startswith(norm):
            return alias_map[alias], 2

    # 3. Contains (one contains the other)
    for alias in sorted_aliases:
        if norm in alias or alias in norm:
            return alias_map[alias], 1

    return None, 0


# ── Table extraction ────────────────────────────────────────────────────────

def _extract_tables_from_pdf(file_bytes: bytes, table_settings: dict = None) -> list:
    """
    Extract tables from PDF using pdfplumber.
    Returns list of tables, each table is a list of rows (each row is a list of cell strings).

    table_settings is passed through to pdfplumber — e.g. {"horizontal_strategy": "text"}
    splits rows by text lines when the PDF's row-separator lines aren't detectable.
    """
    if not PDFPLUMBER_AVAILABLE:
        return []
    tables = []
    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                page_tables = page.extract_tables(table_settings)
                if page_tables:
                    tables.extend(page_tables)
    except Exception as e:
        logger.warning(f"[Parser] Table extraction failed: {e}")
    return tables


def _merge_words_into_phrases(line_words: list, gap: float = 15) -> list:
    """Merge adjacent words on one visual line into phrases (e.g. 'Running' +
    'Balance' → 'Running Balance'). Words closer than `gap` px belong together."""
    phrases = []
    for w in sorted(line_words, key=lambda w: float(w["x0"])):
        x0, x1 = float(w["x0"]), float(w["x1"])
        if phrases and x0 - phrases[-1]["x1"] <= gap:
            phrases[-1]["text"] += " " + w["text"]
            phrases[-1]["x1"] = x1
        else:
            phrases.append({"text": w["text"], "x0": x0, "x1": x1})
    return phrases


def _body_gutters(body_lines: list, min_width: float = 3.0,
                  max_cover: float = 0.10) -> list:
    """Find the vertical whitespace strips that separate the table's columns.

    A header label is usually far narrower than the column beneath it — an
    11-character "Particulars" sits over a 45-character narration — so the
    midpoint between two header labels is nowhere near the edge between two
    columns. The body words are: the x-strips they almost never touch are the
    real separators. "Almost" matters because a statement's summary and
    disclaimer lines run the full width; a strip still counts as a gutter when
    under `max_cover` of the body lines put ink in it.

    Returns [(center_x, width), ...] left to right.
    """
    line_spans = []
    for _, line_words in body_lines:
        spans = sorted((float(w["x0"]), float(w["x1"])) for w in line_words)
        if spans:
            line_spans.append(spans)
    n = len(line_spans)
    if n < 4:
        return []  # too few lines to tell a gutter from a coincidence

    width = int(max(s[-1][1] for s in line_spans)) + 2
    # Per-line coverage via a difference array — O(words), not O(width) per line.
    diff = [0] * (width + 2)
    for spans in line_spans:
        cur_a, cur_b = spans[0]
        merged = []
        for a, b in spans[1:]:
            if a <= cur_b:
                cur_b = max(cur_b, b)
            else:
                merged.append((cur_a, cur_b))
                cur_a, cur_b = a, b
        merged.append((cur_a, cur_b))
        for a, b in merged:
            ia, ib = max(0, int(a)), min(width, int(b) + 1)
            if ib > ia:
                diff[ia] += 1
                diff[ib] -= 1

    limit = max_cover * n
    gutters = []
    run = 0
    start = None
    for x in range(width + 1):
        run += diff[x]
        if run <= limit:
            if start is None:
                start = x
        else:
            if start is not None and x - start >= min_width:
                gutters.append(((start + x) / 2.0, float(x - start)))
            start = None
    return gutters


def _snap_boundaries_to_gutters(boundaries: list, cols: list, gutters: list) -> list:
    """Move each header-derived split into the nearest real column gutter.

    A split only moves to a gutter that lies between the two header labels it
    separates, and the whole set must stay strictly increasing — otherwise the
    original midpoints are kept untouched, so a layout we cannot measure
    behaves exactly as it did before.
    """
    if not gutters or not cols or len(cols) != len(boundaries) + 1:
        return boundaries

    snapped = []
    for i, seed in enumerate(boundaries):
        lo, hi = float(cols[i]["x0"]), float(cols[i + 1]["x1"])
        best = None
        for center, _w in gutters:
            if not lo < center < hi:
                continue
            if best is None or abs(center - seed) < abs(best - seed):
                best = center
        snapped.append(seed if best is None else best)

    if any(b <= a for a, b in zip(snapped, snapped[1:])):
        return boundaries  # snapping reordered the columns — don't trust it
    return snapped


def _extract_word_column_tables(file_bytes: bytes, alias_map: dict) -> list:
    """
    Build tables from word coordinates — immune to pdfplumber's table-line
    detection failures (merged rows, lost header/amount cells).

    The header line is found by matching word-phrases against fieldmap aliases;
    the matched header phrases define column x-boundaries, and every word below
    is bucketed into a column by its x-center. Pages without a header line reuse
    the previous page's boundaries (continuation pages).

    Returns tables in the same row-list format as pdfplumber's extract_tables,
    with the header phrases as the first row.
    """
    if not PDFPLUMBER_AVAILABLE:
        return []
    tables = []
    prev_boundaries = None

    try:
        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            for page in pdf.pages:
                words = page.extract_words() or []
                if not words:
                    continue

                # Group words into visual lines by y-position
                lines = []
                for w in sorted(words, key=lambda w: (float(w["top"]), float(w["x0"]))):
                    y = float(w["top"])
                    if lines and abs(lines[-1][0] - y) <= 3:
                        lines[-1][1].append(w)
                    else:
                        lines.append([y, [w]])

                # Find the header line: the line whose phrases best match aliases
                best_score, best_li, best_phrases = 0, -1, None
                for li, (_, line_words) in enumerate(lines):
                    phrases = _merge_words_into_phrases(line_words)
                    score, matches = 0, 0
                    for ph in phrases:
                        fn, conf = _match_alias(ph["text"], alias_map)
                        if fn:
                            matches += 1
                            score += conf
                    if matches >= 2 and score >= 5 and score > best_score:
                        best_score, best_li, best_phrases = score, li, phrases

                if best_li >= 0:
                    cols = sorted(best_phrases, key=lambda p: p["x0"])
                    boundaries = [(a["x1"] + b["x0"]) / 2 for a, b in zip(cols, cols[1:])]
                    table = [[c["text"] for c in cols]]
                    start_li = best_li + 1
                    # Multi-line headers: absorb following digit-free lines whose
                    # phrases strongly match aliases (e.g. a stray "Date /
                    # Reference No." fragment line under the main header line).
                    while start_li < len(lines):
                        frag_words = lines[start_li][1]
                        joined = " ".join(w["text"] for w in frag_words)
                        if any(ch.isdigit() for ch in joined):
                            break
                        confs = [_match_alias(p["text"], alias_map)[1]
                                 for p in _merge_words_into_phrases(frag_words)]
                        if confs and max(confs) >= 2:
                            start_li += 1
                        else:
                            break
                elif prev_boundaries:
                    boundaries = prev_boundaries  # already gutter-corrected
                    cols = None
                    table = []
                    start_li = 0
                else:
                    continue

                ncols = len(boundaries) + 1

                def _bucket(word_lines):
                    cells = [[] for _ in range(ncols)]
                    for lw in word_lines:
                        for w in sorted(lw, key=lambda w: float(w["x0"])):
                            center = (float(w["x0"]) + float(w["x1"])) / 2
                            idx = 0
                            while idx < len(boundaries) and center > boundaries[idx]:
                                idx += 1
                            cells[idx].append(w["text"])
                    return [" ".join(c) for c in cells]

                # Skip page boilerplate, then group multi-line transactions:
                # a line carrying a date or amount token is an anchor (its own
                # row); text-only fragment lines are wrapped descriptions, which
                # sit below the dated line when cells are top-aligned and both
                # above and below it when they are vertically centred.
                body = []
                for y, line_words in lines[start_li:]:
                    joined = " ".join(w["text"] for w in sorted(line_words, key=lambda w: float(w["x0"])))
                    if _BOILERPLATE_LINE_RE.search(joined):
                        continue
                    body.append((y, line_words))

                anchor_idxs = [i for i, (_, lw) in enumerate(body)
                               if _DATE_TOKEN_RE.search(" ".join(w["text"] for w in lw))
                               or _AMOUNT_TOKEN_RE.search(" ".join(w["text"] for w in lw))]

                # Column and row geometry are measured over the transaction
                # block alone. The disclaimer and marketing prose printed under
                # a statement runs the full page width — left in, it erases
                # every column gutter and swamps the line-gap statistics.
                tx_body = (body[anchor_idxs[0]:anchor_idxs[-1] + 1]
                           if anchor_idxs else body)

                # A header label marks where a column's TITLE sits, not how wide
                # the column is. Re-seat each split in the whitespace the body
                # itself leaves between columns, so a wide narration is no longer
                # cut in half with its tail spilling into the amount column.
                if cols:
                    boundaries = _snap_boundaries_to_gutters(
                        boundaries, cols, _body_gutters(tx_body))

                if anchor_idxs:
                    groups = {a: [] for a in anchor_idxs}
                    anchor_set = set(anchor_idxs)

                    # Which wrapped line belongs to which transaction depends on
                    # how the statement separates its rows, so measure it. When
                    # the line gaps split into "inside a row" and "between rows"
                    # (4.8 vs 15.3 pt on a vertically centred layout), cut at the
                    # large ones — every line in a block is then its anchor's,
                    # whether it sits above or below the dated line.
                    gaps = [tx_body[i][0] - tx_body[i - 1][0]
                            for i in range(1, len(tx_body))]
                    cut = None
                    if len(gaps) >= 3:
                        med = sorted(gaps)[len(gaps) // 2]
                        if med > 0 and any(g > med * 1.4 for g in gaps):
                            cut = med * 1.4

                    blocks = [[0]] if body else []
                    for i in range(1, len(body)):
                        if cut is not None and body[i][0] - body[i - 1][0] > cut:
                            blocks.append([i])
                        else:
                            blocks[-1].append(i)

                    last_anchor = None
                    for blk in blocks:
                        blk_anchors = [i for i in blk if i in anchor_set]
                        if len(blk_anchors) == 1:
                            for i in blk:
                                groups[blk_anchors[0]].append(body[i][1])
                            last_anchor = blk_anchors[0]
                            continue
                        # Uniform leading (nothing to cut on), or several
                        # transactions inside one block: the rows are stacked
                        # top-aligned, so a wrapped line belongs to the anchor it
                        # follows — never to the one it happens to sit closer to.
                        for i in blk:
                            if i in anchor_set:
                                last_anchor = i
                                groups[i].append(body[i][1])
                                continue
                            target = last_anchor
                            if target is None:
                                target = blk_anchors[0] if blk_anchors else None
                            if target is not None:
                                groups[target].append(body[i][1])
                    for a in anchor_idxs:
                        table.append(_bucket(groups[a]))
                else:
                    for y, lw in body:
                        table.append(_bucket([lw]))

                if len(table) > 1 or (table and not boundaries):
                    tables.append(table)
                    prev_boundaries = boundaries
    except Exception as e:
        logger.warning(f"[Parser] Word-column extraction failed: {e}")
    return tables


# ── Header detection ────────────────────────────────────────────────────────

def _detect_header_row(table_rows: list, alias_map: dict) -> tuple:
    """
    Find the header row in a table by matching ≥3 cells against fieldmap aliases.
    Returns (header_row_index, column_mapping) or (-1, None).

    column_mapping = {col_index: fieldname, ...}
    """
    best_score = 0
    best_idx = -1
    best_mapping = None

    for idx, row in enumerate(table_rows):
        if not row or len(row) < 2:
            continue

        mapping = {}
        score = 0
        for col_idx, cell in enumerate(row):
            cell = str(cell).strip()
            if not cell or len(cell) < 2:
                continue
            fieldname, confidence = _match_alias(cell, alias_map)
            if fieldname:
                mapping[col_idx] = fieldname
                score += confidence

        if score > best_score and len(mapping) >= 2:
            best_score = score
            best_idx = idx
            best_mapping = mapping

    return best_idx, best_mapping


# ── Row assembly ────────────────────────────────────────────────────────────

_FOOTER_KEYWORDS = {
    "total", "closing balance", "b/f", "c/f", "b/fwd", "c/fwd",
    "opening balance", "summary", "grand total", "page",
}


_CATEGORY_VOCABULARY = {
    "date":         ("date", "value_date", "entry_date", "tran_date", "txn_date"),
    "description":  ("description", "desc", "particulars", "narration", "remarks", "narrations"),
    "withdrawal":   ("withdrawal", "debit", "dr", "amount_out"),
    "deposits":     ("deposits", "deposit", "credit", "cr", "amount_in", "deposit amt"),
    "balance":      ("balance", "closing_balance", "available_balance"),
    "reference_no": ("reference_no", "ref_no", "chq_ref_no", "cheque_no",
                     "reference", "instrument_no", "ref"),
}

# Normalized once here, and the lookup normalizes its input the same way, so a
# raw column name ("value_date") and a fieldmap alias as stored in alias_map
# ("value date") both resolve — the two spellings can't drift apart.
_CATEGORY_BY_TERM = {
    _normalize_for_matching(term): cat
    for cat, terms in _CATEGORY_VOCABULARY.items()
    for term in terms
}


def _fieldname_category(fieldname: str) -> str | None:
    """Return the semantic category for a fieldname/alias, or None for custom fields.
    Categories are stable concept names — they don't depend on any specific fieldmap string.
    """
    return _CATEGORY_BY_TERM.get(_normalize_for_matching(fieldname or ""))


def _category_map_from_aliases(alias_map: dict) -> dict:
    """{fieldname: category}, resolved through the fieldmap instead of the name.

    A column's role comes from its own name when that name is already a concept
    ("withdrawal"), and otherwise from any alias the user mapped onto it — so a
    column called `field_num_5` whose mapfields contain "debit" IS the
    withdrawal column. Nothing on master has to be named anything in particular,
    which is what makes a fully user-defined schema parse at all.
    """
    cat_by_field = {}
    for alias, fieldname in (alias_map or {}).items():
        cat = _fieldname_category(fieldname) or _fieldname_category(alias)
        if cat and fieldname not in cat_by_field:
            cat_by_field[fieldname] = cat
    return cat_by_field


def _category_of(fieldname: str, cat_by_field: dict = None) -> str | None:
    """Category of one column — fieldmap-resolved first, bare name as fallback."""
    if cat_by_field:
        cat = cat_by_field.get(fieldname)
        if cat:
            return cat
    return _fieldname_category(fieldname)


# Banks print long reference numbers inside a narrow narration column, so their
# own layout wraps them mid-token. Table extraction returns those as separate
# lines, which get rejoined with a space — leaving "AUBLN6202605084170099 8".
# Each pattern below targets a shape that cannot legitimately contain a space,
# so anything the bank really did print with a space is left alone.
_WRAP_REF_TAIL_RE = re.compile(r"\b([A-Z]{2,6}\d{10,})[ \t]+(\d{1,3})(?!\d)")
_WRAP_REF_HEAD_RE = re.compile(r"(?<! )-[ \t]+(?=[A-Z]{2,6}\d{10,})")
_WRAP_IDENT_RE = re.compile(r"\b([A-Z0-9]*_[A-Z0-9_]*)[ \t]+([A-Z0-9]+_[A-Z0-9_]*)")


def _repair_wrapped_tokens(text: str) -> str:
    """Undo spaces that a mid-token line wrap injected into a narration.

    Three shapes are rejoined:
      • a reference split before its last digits   "AUBLN...0099 8"  -> "...00998"
      • a DR-/CR- prefix split from its reference  "NEFT DR- AUBLN"  -> "DR-AUBLN"
      • an underscore identifier split in two      "..._AP R26_JUN26" -> "..._APR26_JUN26"

    Everything else is returned untouched, so a narration that never wrapped is
    passed through byte-for-byte.
    """
    if not text or " " not in text:
        return text
    prev = None
    while prev != text:  # one token can be wrapped more than once
        prev = text
        text = _WRAP_REF_TAIL_RE.sub(r"\1\2", text)
    text = _WRAP_REF_HEAD_RE.sub("-", text)
    return _WRAP_IDENT_RE.sub(r"\1\2", text)


def _has_valid_date(row_cells: list, date_col_idx: int) -> bool:
    """Check if a row has a valid date in the date column."""
    if date_col_idx is None or date_col_idx >= len(row_cells):
        return False
    val = str(row_cells[date_col_idx]).strip()
    return bool(_parse_date_to_date(val))


def _get_balance_val(row: dict, balance_fieldname: str) -> float | None:
    """Extract a numeric balance from a row, or None."""
    raw = row.get(balance_fieldname, "")
    if not raw:
        return None
    cleaned = _clean_amount(str(raw))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except (ValueError, TypeError):
        return None


def _repair_over_split_rows(rows: list, alias_map: dict) -> list:
    """
    Merge fragmented rows caused by over-splitting on date-like tokens in
    continuation lines.

    Pattern: a multi-line transaction gets split so that one fragment has
    amounts+balance but empty description, and the preceding fragment has
    a description but no amounts. We merge these back together.
    Uses fieldmap to find actual column names for each category.
    """
    if len(rows) < 2:
        return rows

    # Resolve category → fieldname from fieldmap
    _cat = {}
    for fn, cat in _category_map_from_aliases(alias_map).items():
        _cat.setdefault(cat, fn)
    bal_fn = _cat.get("balance", "balance")
    dep_fn = _cat.get("deposits", "deposits")
    wdr_fn = _cat.get("withdrawal", "withdrawal")
    desc_fn = _cat.get("description", "desc")

    repaired = []
    i = 0
    while i < len(rows):
        r = rows[i]
        if i + 1 < len(rows):
            next_r = rows[i + 1]
            r_has_desc = bool(r.get(desc_fn))
            r_no_amt = not _row_has_amount(r, wdr_fn, dep_fn)
            n_has_amt = _row_has_amount(next_r, wdr_fn, dep_fn)
            n_has_bal = _get_balance_val(next_r, bal_fn) is not None
            n_empty_desc = not bool(next_r.get(desc_fn))

            if r_has_desc and r_no_amt and n_has_amt and n_has_bal and n_empty_desc:
                merged = dict(r)
                for key, val in next_r.items():
                    if val and not merged.get(key):
                        merged[key] = val
                repaired.append(merged)
                i += 2
                continue

        repaired.append(r)
        i += 1

    return repaired


def _row_has_amount(row: dict, wdr_fn: str, dep_fn: str) -> bool:
    """Check if a row has a non-zero withdrawal or deposit."""
    for key in (wdr_fn, dep_fn):
        raw = row.get(key, "")
        if not raw:
            continue
        try:
            if float(str(raw).replace(",", "")) != 0.0:
                return True
        except (ValueError, TypeError):
            pass
    return False


def _score_balance_chain(rows: list, alias_map: dict) -> tuple:
    """
    Score a list of assembled rows by how well they chain via balance checksums.

    For each row i:  balance_i == balance_{i-1} + deposits_i - withdrawal_i

    Uses fieldmap to find actual column names for each category.
    Returns (consecutive_chain_length, complete_rows).
    """
    _cat = {}
    for fn, cat in _category_map_from_aliases(alias_map).items():
        _cat.setdefault(cat, fn)
    bal_fn = _cat.get("balance", "balance")
    dep_fn = _cat.get("deposits", "deposits")
    wdr_fn = _cat.get("withdrawal", "withdrawal")
    date_fn = _cat.get("date", "date")

    prev_bal = None
    chain_len = 0
    complete_rows = 0

    for r in rows:
        bal = _get_balance_val(r, bal_fn)
        dep_raw = r.get(dep_fn, "")
        wdr_raw = r.get(wdr_fn, "")
        has_date = bool(r.get(date_fn))
        try:
            dep_val = float(_clean_amount(str(dep_raw))) if dep_raw else 0.0
            wdr_val = float(_clean_amount(str(wdr_raw))) if wdr_raw else 0.0
            has_amount = (dep_val != 0.0 or wdr_val != 0.0)
        except (ValueError, TypeError):
            has_amount = False

        if has_date and has_amount and bal is not None:
            complete_rows += 1
            if prev_bal is not None:
                expected = prev_bal + dep_val - wdr_val
                if abs(expected - bal) < 0.02:
                    chain_len += 1
            prev_bal = bal

    return chain_len, complete_rows


def _assemble_rows(table_rows: list, header_idx: int, col_mapping: dict,
                    live_col_types: dict = None, current_row=None,
                    cat_by_field: dict = None) -> tuple:
    """
    Assemble transaction rows from table data using fieldmap + column types.

    Returns (rows, current_row). The in-progress row is NOT flushed here —
    the caller carries it into the next table (continuation pages,
    page-spanning transactions) and flushes after the final table.

    Row keys = fieldmap fieldnames (not hardcoded names).
    Column roles come from information_schema data types:
      • DATE type     → anchor column (starts a new row)
      • TEXT type     → text column (continuation lines + footer check)
      • NUMERIC type  → numeric column (amounts, cleaned)

    Row boundaries, in priority order:
      • dated line, open row complete → new transaction
      • dated line, open row incomplete, but BOTH carry an amount → new
        transaction (one transaction never has two amounts — covers layouts
        where balance isn't printed on every row)
      • dated line, open row incomplete otherwise → value-date continuation
      • date-less line with an amount, open row complete → new transaction
        inheriting the previous date (banks print the date once per
        same-day group)
      • date-less text line → description continuation

    "Complete" = balance present; when no balance column is mapped,
    a non-zero amount.
    """
    # Determine column roles from live column types
    live_col_types = live_col_types or {}
    date_cols = []        # anchor columns — a valid date in ANY starts a new row
    text_cols = set()     # text columns — receive continuation lines
    balance_fieldname = None
    amount_fieldnames = []
    date_fieldname = None

    for col_idx, fieldname in col_mapping.items():
        col_type = (live_col_types.get(fieldname) or "").lower()
        cat = _category_of(fieldname, cat_by_field)
        if col_type in ("date", "timestamp without time zone", "timestamp"):
            date_cols.append(col_idx)
        elif col_type in ("text", "character varying", "varchar"):
            text_cols.add(col_idx)
        elif col_type in ("real", "double precision", "numeric", "integer", "bigint"):
            if cat == "balance":
                balance_fieldname = fieldname
            elif cat in ("withdrawal", "deposits"):
                amount_fieldnames.append(fieldname)

    # Fallbacks when column types are unavailable: resolve roles by category
    if not date_cols:
        for col_idx, fieldname in col_mapping.items():
            if _category_of(fieldname, cat_by_field) == "date":
                date_cols.append(col_idx)
    if balance_fieldname is None:
        for fieldname in col_mapping.values():
            if _category_of(fieldname, cat_by_field) == "balance":
                balance_fieldname = fieldname
                break
    if not amount_fieldnames:
        for fieldname in col_mapping.values():
            if _category_of(fieldname, cat_by_field) in ("withdrawal", "deposits"):
                amount_fieldnames.append(fieldname)
    for dc in date_cols:
        if col_mapping.get(dc):
            date_fieldname = col_mapping[dc]
            break

    def _num(val):
        c = _clean_amount(str(val)) if val else ""
        if not c:
            return None
        try:
            return float(c)
        except ValueError:
            return None

    def _dict_has_amount(row) -> bool:
        if not row:
            return False
        return any(_num(row.get(fn)) not in (None, 0.0) for fn in amount_fieldnames)

    def _line_has_amount(cells) -> bool:
        for idx, cell in enumerate(cells):
            fn = col_mapping.get(idx)
            if fn in amount_fieldnames and cell and _num(cell) not in (None, 0.0):
                return True
        return False

    def _row_complete(row) -> bool:
        if not row:
            return False
        if balance_fieldname is not None:
            return _get_balance_val(row, balance_fieldname) is not None
        return _dict_has_amount(row)

    rows = []
    last_date_val = None  # most recent transaction date, for same-day inheritance
    if current_row and date_fieldname and current_row.get(date_fieldname):
        last_date_val = current_row[date_fieldname]

    for row_idx in range(header_idx + 1, len(table_rows)):
        row_cells = table_rows[row_idx]
        if not row_cells:
            continue

        row_cells = [str(c).strip() if c else "" for c in row_cells]
        # "-" / "–" are empty-cell placeholders in many statements
        row_cells = ["" if c in ("-", "–", "--") else c for c in row_cells]

        line_dated = any(_has_valid_date(row_cells, dc) for dc in date_cols)
        complete = _row_complete(current_row)

        start_new = False
        inherit_date = False
        if line_dated:
            if current_row is None or complete:
                start_new = True
            elif _line_has_amount(row_cells) and _dict_has_amount(current_row):
                start_new = True
            # else: value-date/continuation line of the open transaction
        else:
            # Date-less rows: footer/summary rows (TOTAL, Page N, ...) end the
            # current transaction and must not leak into its description.
            is_footer = False
            for col_idx in text_cols:
                if col_idx < len(row_cells):
                    cell_lower = row_cells[col_idx].lower()
                    for kw in _FOOTER_KEYWORDS:
                        if kw == cell_lower or cell_lower.startswith(kw):
                            is_footer = True
                            break
                if is_footer:
                    break
            if is_footer:
                if current_row:
                    if date_fieldname and current_row.get(date_fieldname):
                        last_date_val = current_row[date_fieldname]
                    rows.append(current_row)
                    current_row = None
                continue

            if complete and _line_has_amount(row_cells):
                start_new = True
                inherit_date = True

        if start_new:
            if current_row:
                if date_fieldname and current_row.get(date_fieldname):
                    last_date_val = current_row[date_fieldname]
                rows.append(current_row)

            current_row = {}
            for col_idx, cell in enumerate(row_cells):
                fieldname = col_mapping.get(col_idx)
                if not fieldname or not cell:
                    continue
                # Statements commonly carry both a transaction date and a value
                # date, and both map to the same field. date_cols is ordered
                # left-to-right, so the first one seen is the transaction date —
                # keep it instead of letting the value date overwrite it. The
                # two only differ on month-boundary postings (interest, EOD
                # sweeps), which is exactly where the wrong one misfiles a row.
                if col_idx in date_cols and current_row.get(fieldname):
                    continue
                current_row[fieldname] = cell
            if inherit_date and date_fieldname and last_date_val and not current_row.get(date_fieldname):
                current_row[date_fieldname] = last_date_val
            if date_fieldname and current_row.get(date_fieldname):
                last_date_val = current_row[date_fieldname]

        elif current_row:
            # Continuation row: text columns append; other mapped columns
            # (amounts, dates) fill only if still empty — handles layouts
            # where a value sits on a later line than the transaction date.
            for col_idx, cell in enumerate(row_cells):
                if not cell:
                    continue
                fieldname = col_mapping.get(col_idx)
                if not fieldname:
                    continue
                if col_idx in text_cols:
                    if current_row.get(fieldname):
                        current_row[fieldname] += " " + cell
                    else:
                        current_row[fieldname] = cell
                elif not current_row.get(fieldname):
                    current_row[fieldname] = cell

    # In-progress row is intentionally NOT flushed — caller carries/flushes it.
    return rows, current_row


# ── Document-level fields ───────────────────────────────────────────────────

def _label_words(fieldmap_rows: list) -> set:
    """Every word that appears in any field's label, from all three alias
    sources. A captured value that starts with one of these is a neighbouring
    LABEL, not a value — that is what stops "Account Number Account Type
    Currency" from filling account_num with "Account Type", and stops the short
    "Branch" alias from filling BRANCH with "Code" out of "Branch Code 1565"."""
    words = set()
    for row in (fieldmap_rows or []):
        sources = (row.get("mapfields") or "").split(",")
        sources += [row.get("displayname") or "", row.get("fieldname") or ""]
        for src in sources:
            for w in re.findall(r"[A-Za-z]+", src):
                if len(w) > 1:
                    words.add(w.lower())
    return words


def _is_label(val: str, label_words: set) -> bool:
    """True when a captured value opens with a label word."""
    toks = val.split()
    if not toks:
        return True
    return re.sub(r"[^A-Za-z]", "", toks[0]).lower() in label_words


def _trim_at_next_label(raw: str, label_words: set) -> str:
    """Cut a to-end-of-line capture where the NEXT label starts.

    Header cards put two label/value pairs on one line, so the capture runs
    past its own value: "DWARKADHIS PROJECTS PRIVATE Product Code :632".
    Two label shapes occur and both are cut here.
    """
    val = raw
    # Colon-terminated label. Walk back over the words in front of the colon,
    # stopping at the first ALL-CAPS one: these cards print values in caps and
    # labels in mixed case, so "PRIVATE" and "NA" end the walk and stay in the
    # value while "Product Code" and "Account Variant/ description" are removed.
    m = re.search(r"[:=]", val)
    if m:
        toks = val[:m.start()].split()
        cut = len(toks)
        while cut > 0 and len(toks) - cut < 3 and not toks[cut - 1].isupper():
            cut -= 1
        val = " ".join(toks[:cut])
    # Bare label built from fieldmap words — no colon to key on.
    toks = val.split()
    for i, tok in enumerate(toks):
        if i and re.sub(r"[^A-Za-z]", "", tok).lower() in label_words:
            toks = toks[:i]
            break
    return re.sub(r"\s+", " ", " ".join(toks)).strip(" \t,;:-–|")


def _extract_document_level_fields(text: str, fieldmap_rows: list, filled_fields: set,
                                   cat_by_field: dict = None) -> dict:
    """
    Extract values printed OUTSIDE the transaction table — e.g. an account
    number in the statement header: "... your account number 045563200000264".

    Only custom fields (no semantic category — never date/desc/amounts/balance,
    which are per-transaction) that did not receive a value from any table
    column are considered. Each field's aliases (mapfields, displayname,
    fieldname) are searched in the raw text as "<alias> <value>"; the last
    alias word may be a prefix of the printed word ("account num" matches
    "account number"). The value must be ≥4 chars and contain a digit.
    The found value applies to every row (document-level constant).
    """
    doc_fields = {}
    if not text:
        return doc_fields

    label_words = _label_words(fieldmap_rows)

    for row in (fieldmap_rows or []):
        fieldname = row.get("fieldname", "")
        if not fieldname or fieldname in filled_fields:
            continue
        if _category_of(fieldname, cat_by_field) is not None:
            continue

        aliases = set()
        for src in (row.get("mapfields", "") or "").split(","):
            src = src.strip()
            if src:
                aliases.add(src)
        for src in (row.get("displayname", ""), fieldname):
            src = (src or "").strip()
            if src:
                aliases.add(src)

        # Two passes over the aliases. The strict pass is the original rule —
        # one whitespace-free token containing a digit — and it runs to
        # exhaustion first, so every field that resolves today resolves
        # identically. Only a field it leaves empty reaches the word pass,
        # which is what picks up a value like "DELHI PITAMPURA" that has no
        # digit in it and does not fit in a single token.
        for strict in (True, False):
            for alias in sorted(aliases, key=len, reverse=True):
                if len(alias) < 3:
                    continue  # too short — would match random text
                # The pattern is built from the alias AS WRITTEN and matched against
                # the raw page text, so the two must agree on punctuation. Splitting
                # into alphanumeric runs and rejoining with a flexible separator
                # lets one alias match every way a bank prints the label:
                # "A/c" also matches "A/C", "Ac", "A-c"; "num" also matches "number".
                parts = [re.escape(p) for p in re.findall(r"[A-Za-z]+|\d+", alias)]
                if not parts:
                    continue
                parts[-1] += r"[a-z]*"
                # The value must sit on the SAME line as its label. Allowing a
                # newline here lets a table's column heading swallow the first cell
                # of the row beneath it — re-importing our own export matched the
                # "account_num" heading and captured the next row's date.
                head = r"\b(" + r"[\s._/\-]*".join(parts) + r")([ \t:\-–=#]*)"
                tail = r"([A-Za-z0-9][A-Za-z0-9/\-]*)" if strict else r"([^\n]*)"
                # Keep scanning past a label that is followed by another label
                # rather than a value ("Account Number Account Type Currency").
                for m in re.finditer(head + tail, text, re.IGNORECASE):
                    if strict:
                        val = m.group(3).strip()
                        ok = len(val) >= 4 and any(ch.isdigit() for ch in val)
                    else:
                        # The word pass has no digit to vouch for the value, so
                        # the LABEL has to be exact. The trailing "[a-z]*" that
                        # lets "num" match "number" also lets the 3-char alias
                        # "a/c" match the word "Account" — harmless while a
                        # digit was required, but here it captured the rest of
                        # "Account Relationship Summary as on 06-Aug-2026".
                        if (_normalize_for_matching(m.group(1))
                                != _normalize_for_matching(alias)):
                            continue
                        val = _trim_at_next_label(m.group(3), label_words)
                        # A label printed with ":" or "=" is unambiguous — what
                        # follows is its value even when it opens with a word
                        # that appears in some other label ("Account status
                        # :ACCOUNT OPEN REGULAR"). Only a label separated by
                        # nothing but spaces needs the label-word guard, and
                        # that is exactly the case the guard exists for
                        # ("Account Number Account Type Currency").
                        explicit = bool(re.search(r"[:=#]", m.group(2)))
                        ok = (3 <= len(val) <= 80
                              and re.match(r"^[A-Za-z0-9]", val)
                              and (explicit or not _is_label(val, label_words)))
                    if ok:
                        doc_fields[fieldname] = val
                        break
                if fieldname in doc_fields:
                    break
            if fieldname in doc_fields:
                break

    return doc_fields


# ── Multi-table assembly ────────────────────────────────────────────────────

_DATE_TOKEN_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}|\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}"
    r"|\d{1,2}-[A-Za-z]{3,9}-\d{2,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4}"
)

# A bare amount token like 1,40,000.00 (used for anchor-line detection)
_AMOUNT_TOKEN_RE = re.compile(r"\d[\d,]*\.\d{2}")

# Page boilerplate that must never merge into a transaction description
_BOILERPLATE_LINE_RE = re.compile(
    r"page \d+ of \d+|auto\s*generated|please review|registered office|"
    r"reg\.?\s*of+i?ce|call us|write to us|follow us|toll.?free",
    re.IGNORECASE,
)

# A line that is purely an amount (numeric cell content) — used to detect
# a numeric cell holding several stacked values.
_AMOUNT_LINE_RE = re.compile(
    r"^\s*(?:Rs\.?|INR|₹)?\s*-?[\d,]+\.\d{1,2}\s*(?:Dr|Cr)?\s*$", re.IGNORECASE
)


def _has_merged_rows(rows: list) -> bool:
    """
    Detect pdfplumber's 'merged row' failure: when row-separator lines aren't
    found, transactions collapse into one giant row whose cells hold many
    newline-joined values. Signatures: a cell containing ≥2 date tokens, or
    a cell whose content is ≥2 stacked amount-only lines.
    """
    for r in rows:
        for v in r.values():
            s = str(v)
            if "\n" not in s:
                continue
            if len(_DATE_TOKEN_RE.findall(s)) >= 2:
                return True
            amount_lines = sum(1 for ln in s.split("\n") if _AMOUNT_LINE_RE.match(ln))
            if amount_lines >= 2:
                return True
    return False


def _assemble_from_tables(tables: list, alias_map: dict, live_col_types: dict,
                          current_row=None) -> tuple:
    """
    Assemble rows from ALL tables (all pages), not just the first.
    Tables with a detectable header are parsed with their own column mapping;
    header-less tables (continuation pages) reuse the previous table's mapping.

    current_row is carried across table boundaries so page-spanning
    transactions keep their continuation lines; the final open row is
    flushed here after the last table.

    Returns (rows, headers_detected, unmapped_headers).
    """
    # Column roles come from the fieldmap, not from what the columns are named.
    cat_by_field = _category_map_from_aliases(alias_map)
    all_rows = []
    headers_detected = {}
    unmapped_headers = []
    last_mapping = None
    last_ncols = 0

    for table in tables:
        if not table:
            continue

        table_len = len(table[0]) if table else 0
        header_idx, col_mapping = _detect_header_row(table, alias_map)
        if header_idx >= 0 and col_mapping and len(col_mapping) >= 2:
            header_row = table[header_idx]
            for col_idx, fn in col_mapping.items():
                if col_idx < len(header_row) and fn not in headers_detected:
                    headers_detected[fn] = str(header_row[col_idx]).strip()
            for col_idx, cell in enumerate(header_row):
                cell_str = str(cell).strip() if cell else ""
                if cell_str and col_idx not in col_mapping and cell_str not in unmapped_headers:
                    unmapped_headers.append(cell_str)

            new_rows, current_row = _assemble_rows(table, header_idx, col_mapping, live_col_types, current_row, cat_by_field)
            all_rows.extend(new_rows)
            last_mapping = col_mapping
            last_ncols = len(header_row)
        elif last_mapping and abs(table_len - last_ncols) <= 1:
            # Continuation page — tolerate ±1 column difference (pdfplumber often
            # detects one extra/missing column on continuation pages)
            new_rows, current_row = _assemble_rows(table, -1, last_mapping, live_col_types, current_row, cat_by_field)
            all_rows.extend(new_rows)

    # Flush the final open transaction
    if current_row:
        all_rows.append(current_row)

    return all_rows, headers_detected, unmapped_headers


# ── Generic parser ──────────────────────────────────────────────────────────

def parse_pdf(file_bytes: bytes, password: str = "", fieldmap_rows: list = None,
              live_col_types: dict = None) -> dict:
    """
    Parse a PDF bank statement and return normalized rows.

    Works for any bank — no per-bank logic.
    Uses pdfplumber table extraction → fieldmap-driven column mapping.

    Args:
        file_bytes: raw PDF bytes
        password: optional password for encrypted PDFs
        fieldmap_rows: list of fieldmap dicts from get_field_mappings()

    Returns:
        {
            "rows": [{<fieldmap fieldname>: value, ...}, ...],
            "row_count": int,
            "headers_detected": {fieldname: header_text, ...},
            "unmapped_headers": [header_text, ...],
            "raw_text": str
        }
    """
    if not PDFPLUMBER_AVAILABLE:
        raise RuntimeError("pdfplumber is not installed. Add it to requirements.txt.")

    t0 = time.perf_counter()

    # Decrypt if needed
    if check_pdf_protected(file_bytes):
        if not password:
            raise RuntimeError(
                "ENCRYPTED: This PDF is password-protected. "
                "Please provide the password to proceed."
            )
        file_bytes = decrypt_pdf(file_bytes, password)

    t1 = time.perf_counter()

    # Build alias map from fieldmap (for header matching)
    alias_map = _build_alias_map(fieldmap_rows or [])
    # …and the semantic role of each column, resolved through that same
    # fieldmap so nothing depends on a column being *named* "withdrawal".
    cat_by_field = _category_map_from_aliases(alias_map)

    # Live column types (passed from pdf_import, used for data-type-driven roles)
    live_col_types = live_col_types or {}

    # Run ALL extraction strategies and keep the best result. Different
    # statements defeat different strategies: ruled-lines fails when row
    # separators aren't detectable (rows merge or cells vanish), text-lines
    # can lose header/amount cells, word-columns handles those but depends on
    # a matchable header line. Comparing assembled output is layout-agnostic.
    # Best = most rows, then most populated cells (catches dropped columns).
    candidates = []
    # Tie-break order: "lines" first (pdfplumber's ruled cells group wrapped
    # description fragments perfectly), then "words" (geometric grouping —
    # rescues statements whose table lines aren't detectable), then "text".
    for label, settings in (("lines", None),):
        tables = _extract_tables_from_pdf(file_bytes, settings)
        if tables:
            candidates.append((label, _assemble_from_tables(tables, alias_map, live_col_types)))
    word_tables = _extract_word_column_tables(file_bytes, alias_map)
    if word_tables:
        candidates.append(("words", _assemble_from_tables(word_tables, alias_map, live_col_types)))
    text_tables = _extract_tables_from_pdf(file_bytes, {"horizontal_strategy": "text"})
    if text_tables:
        candidates.append(("text", _assemble_from_tables(text_tables, alias_map, live_col_types)))

    def _cand_score(cand):
        cand_rows = cand[1][0]
        if _has_merged_rows(cand_rows):
            return (0, 0, 0)  # merged-row signature — only wins if nothing else does
        chain_len, complete_rows = _score_balance_chain(cand_rows, alias_map)
        # Primary: balance chain length (correctness), secondary: complete rows, tertiary: total rows
        return (chain_len, complete_rows, len(cand_rows))

    rows, headers_detected, unmapped_headers = [], {}, []
    if candidates:
        for label, assembled in candidates:
            logger.info(f"[Parser] strategy={label}: rows={len(assembled[0])}, cells={sum(len(r) for r in assembled[0])}")
        best_label, (rows, headers_detected, unmapped_headers) = max(
            candidates, key=_cand_score)
        if not rows:
            best_label, (rows, headers_detected, unmapped_headers) = max(
                candidates, key=lambda c: len(c[1][0]))
        logger.info(f"[Parser] selected strategy={best_label}, rows={len(rows)}")

    # Repair pass: merge fragmented rows where a date-bearing line split a
    # multi-line transaction. A row with amounts but empty description merges
    # with the preceding fragment that has a description but no amounts —
    # kept only if the balance chain doesn't get worse after the merge.
    repaired = _repair_over_split_rows(rows, alias_map)
    if len(repaired) != len(rows) and _score_balance_chain(repaired, alias_map) >= _score_balance_chain(rows, alias_map):
        logger.info(f"[Parser] repair: merged {len(rows) - len(repaired)} over-split rows ({len(rows)} → {len(repaired)})")
        rows = repaired

    t2 = time.perf_counter()

    text = extract_text_from_pdf(file_bytes)

    # 4) Last resort: text-based row parsing
    if not rows:
        logger.info("[Parser] Table extraction yielded no rows, falling back to text extraction")
        rows = _parse_rows_fallback(text)
        headers_detected = {}
        unmapped_headers = []

    t3 = time.perf_counter()

    # Resolve parser concept-keys to master column names via fieldmap.
    # Fallback rows use concept names ("description", "withdrawal" etc.).
    # Table rows already have fieldmap fieldnames as keys — those pass through unchanged.
    # Build category→fieldname mapping from fieldmap's display names.
    _category_map = {}
    for fieldname, cat in cat_by_field.items():
        _category_map.setdefault(cat, fieldname)

    canonical_to_master = {}
    for parser_key in ("date", "description", "withdrawal", "deposits", "balance", "reference_no"):
        cat = _fieldname_category(parser_key)
        master_col = _category_map.get(cat)
        if master_col:
            canonical_to_master[parser_key] = master_col
        else:
            # No fieldmap entry for this category — use the key as-is
            # (works for default columns like "date" which exists in schema directly)
            canonical_to_master[parser_key] = parser_key

    # Normalize rows: coerce types from live_col_types (not hardcoded names).
    # Fallback-parser rows use concept keys ("description", ...) — rename them
    # to master column names first; table rows already carry fieldmap fieldnames.
    normalized = []
    for r in rows:
        new_row = {}
        for key, val in r.items():
            if val is None or val == "":
                continue
            if key not in live_col_types:
                key = canonical_to_master.get(key, key)
            col_type = (live_col_types.get(key) or "").lower()
            if col_type in ("date", "timestamp without time zone", "timestamp"):
                new_row[key] = _parse_date(val)
            elif col_type in ("real", "double precision", "numeric", "integer", "bigint"):
                new_row[key] = _clean_amount(val)
            else:
                # Collapse newlines from multi-line PDF cells into single spaces
                new_row[key] = re.sub(r"\s+", " ", str(val)).strip()
        if new_row:
            normalized.append(new_row)

    # Rejoin tokens the bank's own line wrapping split apart in the narration.
    # Runs after normalization so the existing type-coercion path is untouched;
    # the target column is resolved by category, never by a hardcoded name.
    desc_fields = {
        (fm.get("fieldname") or "")
        for fm in (fieldmap_rows or [])
        if _category_of(fm.get("fieldname") or "", cat_by_field) == "description"
    }
    for r in normalized:
        for fn in desc_fields:
            if isinstance(r.get(fn), str):
                r[fn] = _repair_wrapped_tokens(r[fn])

    # Document-level fields: custom fields with no table column (e.g. an
    # account number printed above the table) get their value from raw text
    # and are stamped on every row.
    filled_keys = set()
    for r in normalized:
        filled_keys.update(r.keys())
    doc_fields = _extract_document_level_fields(text, fieldmap_rows or [], filled_keys, cat_by_field)
    if doc_fields:
        logger.info(f"[Parser] document-level fields: {doc_fields}")
        for r in normalized:
            for fn, val in doc_fields.items():
                r.setdefault(fn, val)

    # Reconciliation: count date-like tokens in raw text for debugging
    _date_tokens = _DATE_TOKEN_RE.findall(text)

    t4 = time.perf_counter()
    logger.info(
        f"[Parser] table: {(t2-t1)*1000:.0f}ms, assemble: {(t3-t2)*1000:.0f}ms, "
        f"normalize: {(t4-t3)*1000:.0f}ms, TOTAL: {(t4-t0)*1000:.0f}ms, "
        f"rows={len(normalized)}, dates_in_text={len(_date_tokens)}"
    )

    return {
        "rows": normalized,
        "row_count": len(normalized),
        "headers_detected": headers_detected,
        "unmapped_headers": unmapped_headers,
        "document_fields": doc_fields,
        "raw_text": text[:2000],
        "stats": {
            "dates_in_raw_text": len(_date_tokens),
        },
    }




# ── Fallback text parser (no table structure) ───────────────────────────────

def _parse_rows_fallback(text: str) -> list:
    """
    Text-based fallback when table extraction fails.
    Finds lines starting with date patterns and accumulates fields.
    """
    lines = text.split("\n")

    date_patterns = [
        re.compile(r"^(\d{4}-\d{2}-\d{2})"),
        re.compile(r"^(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})(?!\d)"),
        re.compile(r"^(\d{1,2}-[A-Za-z]{3}-\d{2,4})(?!\d)"),
        re.compile(r"^(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})(?!\d)"),
    ]

    amt_pattern = re.compile(r"(-?[\d,]+\.\d{2})")
    amt_triple = re.compile(r"([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})")

    # Only skip obvious PDF boilerplate that could never be a transaction line.
    # We no longer skip lines containing field names like IFSC, MICR, Branch,
    # etc. — those could legitimately be custom fields added by the user.
    _boilerplate = re.compile(
        r"^(Page\s+\d+|Statement\s+of|Generated\s+on|Subject\s+to|Registered\s+office)"
    )

    rows = []
    current_row = None

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Skip only unmistakable boilerplate
        if _boilerplate.match(line):
            continue

        date_val = None
        date_end = 0
        for pat in date_patterns:
            m = pat.match(line)
            if m:
                date_val = m.group(1)
                date_end = m.end()
                break

        # A date-shaped token must also be a real calendar date (rejects
        # e.g. "04/55/63" reference fragments at line start)
        if date_val and not _parse_date_to_date(date_val):
            date_val = None
            date_end = 0

        if date_val:
            if current_row and current_row.get("date"):
                rows.append(current_row)
            rest = line[date_end:].strip()
            current_row = {
                "date": _parse_date(date_val),
                "description": rest,
                "withdrawal": "",
                "deposits": "",
                "balance": "",
                "reference_no": "",
            }
            _extract_amounts(current_row, rest, amt_pattern, amt_triple)
        elif current_row:
            _extract_amounts(current_row, line, amt_pattern, amt_triple)

    if current_row and current_row.get("date"):
        rows.append(current_row)

    # Clean descriptions
    for r in rows:
        r["description"] = re.sub(r"\s+", " ", r["description"]).strip()
        r["description"] = re.sub(r"\s*[\d,]+\.\d{2}.*$", "", r["description"]).strip()

    return rows


def _extract_amounts(row: dict, text: str, amt_pattern, amt_triple):
    """Extract amounts from text into row dict."""
    triple = amt_triple.search(text)
    if triple:
        row["withdrawal"] = triple.group(1)
        row["deposits"] = triple.group(2)
        row["balance"] = triple.group(3)
        desc_part = text[:triple.start()].strip()
        if desc_part and len(desc_part) < len(row.get("description", "")):
            row["description"] = desc_part
        return

    amounts = amt_pattern.findall(text)
    if len(amounts) == 1:
        if not row.get("balance"):
            row["balance"] = amounts[0]
    elif len(amounts) >= 2:
        # First amount = transaction amount (withdrawal if Dr sign present)
        if not row.get("withdrawal"):
            row["withdrawal"] = amounts[0]
        # Second amount = running balance (NOT deposits)
        if not row.get("balance"):
            row["balance"] = amounts[1]

    ref_match = re.search(r"(\d{10,})\s*$", text)
    if ref_match:
        row["reference_no"] = ref_match.group(1)


# ── Main entry point ────────────────────────────────────────────────────────

def _parse_sync(file_bytes: bytes, password: str = "", fieldmap_rows: list = None, live_col_types: dict = None) -> dict:
    """Sync wrapper for use with run_in_executor."""
    return parse_pdf(file_bytes, password=password, fieldmap_rows=fieldmap_rows or [],
                     live_col_types=live_col_types or {})
