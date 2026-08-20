"""
PDF bank-statement import.

Parsing is DPL's parsers.py, unmodified. This module does three things around
it: feed it the company's fieldmap, collapse its withdrawal/deposits output
into temp_trans's (amount, credit_debit), and record the batch.

Connection handling is deliberate. The parser can run for up to three minutes
on a large scanned-ish statement, and it does no database work. Holding a
pooled connection -- and an open transaction, since company_connection wraps
one -- across that would pin a Supabase connection and keep a transaction idle
for minutes. So the fieldmap read, the parse, and the write are three separate
steps, and no connection is held while the CPU work happens.
"""
from __future__ import annotations

import asyncio
import io
import logging
import time

import config
import parsers
from import_helpers import compute_fill_rates, normalize_parsed_rows
from services import jobs
from services.fieldmap import get_field_mappings, live_col_types
from services.staging import assert_bank_exists, stage_batch

logger = logging.getLogger(__name__)

# Both read from the environment — see config.py for why a fixed number is the
# wrong answer for either of them.
PARSE_TIMEOUT_SECONDS = config.PARSE_TIMEOUT_SECONDS
PARSE_SECONDS_PER_PAGE = config.PARSE_SECONDS_PER_PAGE
MAX_PDF_PAGES = config.MAX_PDF_PAGES


def parse_deadline(pages: int | None) -> float:
    """How long this particular file is allowed to take.

    Scaled by page count because that is what the cost is made of — one flat
    number has to be either too mean for a long statement or meaningless for a
    short one, and picking the wrong end of that is what cut a 65-page file off
    at 240s while it was parsing perfectly well.

    Unknown page count (an encrypted file the slicer could not open) falls back
    to the floor; the parser is about to report the real problem anyway.
    """
    if not pages:
        return PARSE_TIMEOUT_SECONDS
    return max(PARSE_TIMEOUT_SECONDS, pages * PARSE_SECONDS_PER_PAGE)

# How many times the parser walks the document. It competes several extraction
# strategies against each other and each one re-reads every page, so a page is
# visited this many times before the file is done. Measured at 4, identically on
# a 4-page and a 65-page statement.
#
# Used only to turn a page counter into a percentage. If a strategy is ever
# added or dropped the bar goes slightly fast or slow — which is why it is
# capped below 100 until the parse actually returns, and why nothing but the
# display depends on it.
PARSER_PASSES = 4


def _page_count(file_bytes: bytes) -> int | None:
    """How many pages, or None if that cannot be read without the password.

    Only the page tree is touched, not the content of any page, so this costs
    almost nothing next to a parse. An encrypted or malformed file returns None
    and is left to the parser, which already reports both properly — guessing
    here would replace a precise message with a vaguer one.
    """
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return len(pdf.pages)
    except Exception:
        return None


def parse_page_spec(spec: str, total: int | None) -> tuple[int, int] | None:
    """Turn "30" or "31-65" into a 1-based inclusive range, or None for all.

    Accepts a bare count ("30" = the first 30 pages) and an explicit range
    ("31-65"), because those are the two things people mean. Blank is every
    page, which is the default and the behaviour this module has always had.
    """
    spec = (spec or "").strip()
    if not spec or spec.lower() == "all":
        return None

    if "-" in spec:
        a, _, b = spec.partition("-")
        try:
            start, end = int(a.strip()), int(b.strip())
        except ValueError:
            raise RuntimeError(
                f"Could not read '{spec}' as a page range. Use a count like 30, "
                f"a range like 31-65, or leave it blank for the whole file."
            )
    else:
        try:
            start, end = 1, int(spec)
        except ValueError:
            raise RuntimeError(
                f"Could not read '{spec}' as a page count. Use a count like 30, "
                f"a range like 31-65, or leave it blank for the whole file."
            )

    if start < 1 or end < start:
        raise RuntimeError(
            f"Page range {start}-{end} runs backwards or starts below page 1."
        )
    if total is not None and start > total:
        raise RuntimeError(
            f"This statement has {total} pages, so it has no page {start}."
        )
    if total is not None:
        end = min(end, total)
        # A selection that turns out to cover the whole document IS the whole
        # document — "the first 700 pages" of a 65-page file, or an explicit
        # 1-65. Saying so here means no needless slicing, and the same
        # fingerprint as an ordinary full upload, so the two cannot be staged
        # twice as if they were different imports.
        if start == 1 and end == total:
            return None
    return start, end


def slice_pdf(file_bytes: bytes, start: int, end: int) -> tuple[bytes, list[int]]:
    """Extract pages start..end, always carrying page 1 along with them.

    Page 1 is included even when the range does not ask for it, and that is not
    a convenience — it is what makes a mid-file range readable at all. A bank
    prints the column header once, at the top of the first page; every page
    after it is a continuation with nothing naming its columns. Slice pages
    31-65 on their own and the parser finds rows it cannot map to a single
    field, which is worse than finding nothing.

    Returns the new document and the 1-based page numbers actually in it, so
    the caller can tell the user what was read — including that page 1 came
    along, and that its transactions will therefore appear in this import.
    """
    if not parsers.PYPDF_AVAILABLE:
        raise RuntimeError("pypdf is required to import part of a PDF.")

    reader = parsers.PdfReader(io.BytesIO(file_bytes))
    total = len(reader.pages)
    end = min(end, total)

    wanted = list(range(start, end + 1))
    if 1 not in wanted:
        wanted = [1] + wanted

    writer = parsers.PdfWriter()
    for number in wanted:
        writer.add_page(reader.pages[number - 1])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), wanted


def _parse_with_progress(job_id, file_bytes, password, fieldmap_rows, col_types):
    """Run the parse, reporting each finished page to the job registry.

    The hook is set here rather than by the caller because it is thread-local
    and this function is what actually runs on the executor thread.
    """
    if job_id:
        parsers._progress.hook = lambda: jobs.tick(job_id)
    try:
        return parsers._parse_sync(file_bytes, password, fieldmap_rows, col_types)
    finally:
        parsers._progress.hook = None


def _page_count(file_bytes: bytes) -> int | None:
    """How many pages, or None if that cannot be read without the password.

    Only the page tree is touched, not the content of any page, so this costs
    almost nothing next to a parse. An encrypted or malformed file returns None
    and is left to the parser, which already reports both properly — guessing
    here would replace a precise message with a vaguer one.
    """
    try:
        import pdfplumber

        with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
            return len(pdf.pages)
    except Exception:
        return None


async def process_pdf_import(
    schema: str,
    file_bytes: bytes,
    filename: str,
    username: str,
    bank_id: int | None = None,
    password: str = "",
    save: bool = False,
    pages_spec: str = "",
    job_id: str | None = None,
) -> dict:
    """Parse a statement and, when save=True, stage it into temp_trans.

    save=False is a dry run: it returns exactly what would be staged, so the
    user can check the column mapping and fill rates before committing a batch.
    Nothing is written and no batch is created.

    pages_spec limits how much of the file is read — "30" for the first thirty
    pages, "31-65" for a range, blank for all of it. job_id, when given, is a
    registry entry this reports progress into as it goes.
    """
    t_start = time.perf_counter()

    fieldmap_rows = await get_field_mappings(schema)
    if not fieldmap_rows:
        raise RuntimeError(
            "This company has no fieldmap rows, so no column header can be "
            "recognised. Run: python -m db.migrate upgrade"
        )
    col_types = live_col_types(fieldmap_rows)

    # Before the parse, not after: a bank id that cannot be used is the one
    # failure here that is knowable in advance, and a long statement makes the
    # difference between knowing now and knowing in three minutes.
    if save:
        await assert_bank_exists(schema, bank_id)

    # --- Page selection ------------------------------------------------------
    # Applied by handing the parser a shorter document, never by telling it to
    # skip anything: it sees an ordinary PDF and reads all of it, so nothing
    # about how a statement is understood changes with this setting.
    source_bytes = file_bytes
    pages_used: list[int] | None = None
    page_range = parse_page_spec(pages_spec, _page_count(file_bytes))
    if page_range:
        if password and parsers.check_pdf_protected(file_bytes):
            # Slicing needs to read the page tree, which an encrypted file will
            # not give up. The parser would have decrypted it a moment later
            # anyway, with this same function.
            source_bytes = parsers.decrypt_pdf(file_bytes, password)
            password = ""
        source_bytes, pages_used = slice_pdf(source_bytes, *page_range)
        logger.info("[PDF] %s: parsing pages %s of %s", filename,
                    pages_spec, _page_count(file_bytes))

    pages = _page_count(source_bytes)
    if MAX_PDF_PAGES and pages is not None and pages > MAX_PDF_PAGES:
        raise RuntimeError(
            f"This statement has {pages} pages and this server is configured to "
            f"parse at most {MAX_PDF_PAGES} in one request. Import it in parts "
            f"using the page selector, or import the bank's Excel/CSV export of "
            f"the same period — those parse in a fraction of the time."
        )

    deadline = parse_deadline(pages)
    if job_id:
        jobs.set_state(job_id, jobs.PARSING,
                       f"Reading {pages or '?'} pages")

    # --- Parse (no database connection held) ---------------------------------
    loop = asyncio.get_running_loop()
    t0 = time.perf_counter()
    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None, _parse_with_progress, job_id, source_bytes, password,
                fieldmap_rows, col_types
            ),
            timeout=deadline,
        )
    except asyncio.TimeoutError:
        logger.error("[PDF] parser timed out after %ss (pages=%s)", deadline, pages)
        # Naming the page count is the difference between "something went wrong"
        # and a number the user can act on — a scan and a very long statement
        # both time out, and they need opposite remedies.
        size = f"This statement has {pages} pages. " if pages else ""
        raise RuntimeError(
            f"PDF parsing timed out after {deadline:.0f}s. {size}"
            "Read part of it with the page selector — the first 30 pages, then "
            "31 onwards — or import the bank's Excel/CSV export instead. If it "
            "is a scan it has no text layer and cannot be read at all; ask the "
            "bank for the e-statement."
        )
    t_parse = (time.perf_counter() - t0) * 1000

    parsed_rows = result.get("rows", [])
    normalized, norm_stats = normalize_parsed_rows(parsed_rows, fieldmap_rows)
    logger.info(
        "[PDF] parse %.0fms, parsed=%d usable=%d", t_parse, len(parsed_rows), len(normalized)
    )

    parse_stats = {
        **result.get("stats", {}),
        **norm_stats,
        "parse_ms": round(t_parse),
        "headers_detected": result.get("headers_detected", {}),
        "unmapped_headers": result.get("unmapped_headers", []),
    }

    payload = {
        "saved": False,
        "batch_id": None,
        "row_count": len(normalized),
        "rows": normalized[:200],          # preview cap; the batch holds them all
        "truncated": len(normalized) > 200,
        "headers_detected": result.get("headers_detected", {}),
        "unmapped_headers": result.get("unmapped_headers", []),
        "document_fields": result.get("document_fields", {}),
        "fill_rates": compute_fill_rates(parsed_rows),
        "stats": parse_stats,
        "duplicate_rows": 0,
        # What was actually read, so the screen can say so rather than implying
        # the whole file was taken. header_page_added is the one surprise worth
        # naming: page 1's transactions are in this import because its header
        # had to be, and they will also be in whichever part covers page 1.
        "pages_parsed": pages_used,
        "pages_total": _page_count(file_bytes),
        "header_page_added": bool(
            pages_used and page_range and page_range[0] > 1
        ),
    }

    if not save:
        payload["total_ms"] = round((time.perf_counter() - t_start) * 1000)
        return payload

    if not normalized:
        raise RuntimeError(
            "No usable transaction rows were found. Check headers_detected in "
            "the dry run -- if it is empty, this bank's column names are not in "
            "the fieldmap yet."
        )

    # --- Write ----------------------------------------------------------------
    if job_id:
        jobs.set_state(job_id, jobs.SAVING, f"Saving {len(normalized)} rows")

    # The hash is taken over the ORIGINAL file plus which pages were read, not
    # over the slice: pypdf rewrites a document when it extracts pages, so
    # hashing the slice would make the same request produce a different
    # fingerprint each time and defeat duplicate detection entirely. Scoping by
    # range is also the behaviour that is actually wanted — importing pages 1-30
    # and then 31-65 of one statement is two imports, not a repeat.
    label = filename
    scope = ""
    if page_range:
        scope = f"pages={page_range[0]}-{page_range[1]}"
        label = f"{filename} (pages {page_range[0]}-{page_range[1]})"

    staged = await stage_batch(
        schema, file_bytes, label, username, bank_id, normalized, parse_stats,
        hash_scope=scope,
    )

    payload.update(
        saved=True,
        batch_id=staged["batch_id"],
        row_count=staged["inserted"],
        duplicate_rows=staged["duplicate_rows"],
        total_ms=round((time.perf_counter() - t_start) * 1000),
    )
    return payload


async def start_pdf_job(**kwargs) -> dict:
    """Begin an import in the background and return its job id immediately.

    The work is identical to process_pdf_import — this only changes who waits
    for it. Two things follow from that. The upload request finishes in
    milliseconds, so a host that caps request duration has nothing to cut short;
    and because the parse reports each page it finishes, the browser can ask how
    far along it is instead of watching a spinner for three minutes.

    Failures are recorded on the job rather than raised, because by the time
    they happen the request that started it has long since been answered.
    """
    total_pages = _page_count(kwargs["file_bytes"])
    page_range = parse_page_spec(kwargs.get("pages_spec", ""), total_pages)
    # What the parser will actually walk, which is the slice when there is one —
    # plus page 1 if it had to be carried along for its header.
    if page_range:
        span = page_range[1] - page_range[0] + 1
        planned = span + (1 if page_range[0] > 1 else 0)
    else:
        planned = total_pages or 1

    job_id = jobs.create(
        schema=kwargs["schema"],
        username=kwargs["username"],
        filename=kwargs["filename"],
        total_units=planned * PARSER_PASSES,
        total_pages=planned,
    )

    async def _runner():
        try:
            payload = await process_pdf_import(job_id=job_id, **kwargs)
            jobs.finish(job_id, payload)
        except Exception as exc:                      # noqa: BLE001
            # Every failure mode of the synchronous path lands here instead of
            # in a response: a duplicate file, an unreadable range, a timeout.
            # They are recorded verbatim so the poller can show the same
            # sentence the direct upload would have shown.
            logger.warning("[PDF] job %s failed: %s", job_id, exc)
            jobs.fail(job_id, str(exc))

    jobs.attach_task(job_id, asyncio.create_task(_runner()))
    return {
        "job_id": job_id,
        "state": jobs.QUEUED,
        "total_pages": planned,
        "pages_total": total_pages,
    }
