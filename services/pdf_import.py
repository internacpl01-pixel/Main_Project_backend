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
from import_helpers import (compute_fill_rates, fields_by_category,
                            normalize_parsed_rows)
from services import jobs
from services.fieldmap import get_field_mappings, live_col_types
from services.staging import assert_bank_exists, stage_batch

logger = logging.getLogger(__name__)

# Both read from the environment — see config.py for why a fixed number is the
# wrong answer for either of them.
PARSE_TIMEOUT_SECONDS = config.PARSE_TIMEOUT_SECONDS
PARSE_SECONDS_PER_PAGE = config.PARSE_SECONDS_PER_PAGE
MAX_PDF_PAGES = config.MAX_PDF_PAGES
PDF_BATCH_PAGES = config.PDF_BATCH_PAGES


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


def slice_pdf(file_bytes: bytes, wanted: list[int]) -> tuple[bytes, list[int]]:
    """Build a PDF from the given 1-based page numbers, always carrying page 1.

    Page 1 is included even when the selection does not ask for it, and that is
    not a convenience — it is what makes any later page readable at all. A bank
    prints the column header once, at the top of the first page; every page
    after it is a continuation with nothing naming its columns. Hand the parser
    pages 31-40 on their own and it finds rows it cannot map to a single field,
    which is worse than finding nothing.

    Returns the new document and the page numbers actually in it, so the caller
    can say what was read — including that page 1 came along.
    """
    if not parsers.PYPDF_AVAILABLE:
        raise RuntimeError("pypdf is required to import part of a PDF.")

    reader = parsers.PdfReader(io.BytesIO(file_bytes))
    total = len(reader.pages)
    wanted = [n for n in wanted if 1 <= n <= total]
    if 1 not in wanted:
        wanted = [1] + wanted

    writer = parsers.PdfWriter()
    for number in wanted:
        writer.add_page(reader.pages[number - 1])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue(), wanted


def selected_pages(spec: str, total: int | None) -> list[int] | None:
    """The page numbers a spec asks for, or None for the whole document."""
    if total is None:
        return None
    rng = parse_page_spec(spec, total)
    if rng is None:
        return None
    return list(range(rng[0], rng[1] + 1))


def plan_batches(pages: list[int], batch_size: int) -> list[list[int]]:
    """Split the pages to read into batches, each carrying page 1 for its header.

    Batching is not a speed measure — it re-reads page 1 once per batch, so it
    is slightly MORE work than one pass. It is an accuracy measure, and what it
    buys is containment.

    The parser competes its extraction strategies across the whole document and
    keeps a single winner, so a run of pages the winner handles badly sets the
    strategy for every other page too. On a 65-page KVB statement pages 41-60 do
    exactly that: read in one pass, the Branch and Cheque No columns are lost on
    all 928 rows, and the empty Branch is then back-filled from the page-1 header
    so every row claims the account's home branch rather than its own code.

    Batched, only the stretch holding those pages is affected — 630 of 928 rows
    keep the branch actually printed against them instead of none of them.

    Two things this is NOT: a cure (the strategy choice is still wrong for those
    pages), and length-sensitive (batches of 10 fail on the same pages as batches
    of 20 — it is those pages, not the size of the document).

    A caveat worth knowing: a transaction whose text wraps across a batch
    boundary is assembled from one side of the split only. On the statement this
    was measured against the row count came out identical either way, but the
    risk is real, and it is why the batch size is generous rather than tiny.
    """
    if batch_size <= 0 or len(pages) <= batch_size:
        return [pages]
    return [pages[i:i + batch_size] for i in range(0, len(pages), batch_size)]


def _row_key(row: dict, cats: dict) -> tuple:
    """What identifies a transaction: its date, its amounts and its balance.

    Deliberately NOT the whole row. Page 1 is parsed once on its own to learn
    what it contributes, and again inside each later batch — and the two do not
    always agree on the text columns, because column geometry is measured per
    document and a one-page document measures differently. Matching on the whole
    row therefore missed rows that were plainly the same transaction, and they
    survived into the import twice.

    A running balance is all but unique per row, so date plus amounts plus
    balance identifies a line without depending on any text landing in the same
    column both times. Resolved through the fieldmap like everything else.
    """
    return tuple(
        str(row.get(cats.get(role)) or "").strip()
        for role in ("date", "withdrawal", "deposits", "balance")
    )


def _drop_repeated_header_page(rows: list, page_one_keys: set, cats: dict,
                               limit: int) -> list:
    """Remove the leading rows a batch inherited from page 1.

    Every batch after the first is handed page 1 for its column header, so page
    1's transactions are parsed again with it. They are already in the first
    batch's output, so they come off here — from the front only, and never more
    than page 1 actually holds, so a transaction that genuinely repeats later in
    the batch is left where it is.
    """
    cut = 0
    while (cut < len(rows) and cut < limit
           and _row_key(rows[cut], cats) in page_one_keys):
        cut += 1
    return rows[cut:]


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
    batch_pages: int = PDF_BATCH_PAGES,
    job_id: str | None = None,
) -> dict:
    """Parse a statement and, when save=True, stage it into temp_trans.

    save=False is a dry run: it returns exactly what would be staged, so the
    user can check the column mapping and fill rates before committing a batch.
    Nothing is written and no batch is created.

    pages_spec limits how much of the file is read — "30" for the first thirty
    pages, "31-65" for a range, blank for all of it.

    batch_pages reads a long statement in stretches of that many pages and
    stitches the rows back into one import, because column detection degrades
    with length — see plan_batches. 0 turns it off and reads the file in one go.

    job_id, when given, is a registry entry this reports progress into.
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

    # --- Page selection and batching -----------------------------------------
    # Both are applied by handing the parser a shorter document, never by
    # telling it to skip anything: each parse sees an ordinary PDF and reads all
    # of it, so nothing about how a statement is understood changes here.
    source_bytes = file_bytes
    if password and parsers.check_pdf_protected(file_bytes):
        # Slicing has to read the page tree, which an encrypted file will not
        # give up. The parser would have decrypted it a moment later anyway,
        # with this same function.
        source_bytes = parsers.decrypt_pdf(file_bytes, password)
        password = ""

    total_pages = _page_count(source_bytes)
    page_range = parse_page_spec(pages_spec, total_pages)
    wanted = selected_pages(pages_spec, total_pages) or list(
        range(1, (total_pages or 1) + 1)
    )
    batches = plan_batches(wanted, batch_pages)
    batched = len(batches) > 1

    if MAX_PDF_PAGES and total_pages and len(wanted) > MAX_PDF_PAGES:
        raise RuntimeError(
            f"This statement has {total_pages} pages and this server is "
            f"configured to parse at most {MAX_PDF_PAGES} in one request. Use "
            f"the page selector to read part of it, or import the bank's "
            f"Excel/CSV export of the same period."
        )

    loop = asyncio.get_running_loop()
    t0 = time.perf_counter()

    async def _parse(doc_bytes: bytes, label: str, n_pages: int) -> dict:
        deadline = parse_deadline(n_pages)
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(
                    None, _parse_with_progress, job_id, doc_bytes, password,
                    fieldmap_rows, col_types
                ),
                timeout=deadline,
            )
        except asyncio.TimeoutError:
            logger.error("[PDF] %s timed out after %ss (%s pages)",
                         label, deadline, n_pages)
            raise RuntimeError(
                f"PDF parsing timed out after {deadline:.0f}s while reading "
                f"{label} ({n_pages} pages). Read a smaller part with the page "
                f"selector, or import the bank's Excel/CSV export instead. If it "
                f"is a scan it has no text layer and cannot be read at all; ask "
                f"the bank for the e-statement."
            )

    # Page 1's own transactions, so they can be removed from every later batch —
    # each one is handed page 1 for its column header and parses its rows again.
    page_one_keys: set = set()
    page_one_count = 0
    cats = fields_by_category(fieldmap_rows)
    if batched:
        one_bytes, _ = slice_pdf(source_bytes, [1])
        if job_id:
            # Index 0: real work the user can watch, but not a batch. Numbering
            # it as one would make a four-batch import report five.
            jobs.start_step(job_id, index=0, total=len(batches),
                            label="page 1", units=PARSER_PASSES,
                            message="Reading page 1 for its column header")
        page_one = (await _parse(one_bytes, "page 1", 1)).get("rows", [])
        page_one_keys = {_row_key(r, cats) for r in page_one}
        page_one_count = len(page_one)
        logger.info("[PDF] %s: %d batches, page 1 contributes %d rows",
                    filename, len(batches), page_one_count)

    parsed_rows: list = []
    pages_used: list[int] = []
    result: dict = {}
    for i, batch in enumerate(batches, start=1):
        if batched:
            doc, in_batch = slice_pdf(source_bytes, batch)
            label = f"pages {batch[0]}-{batch[-1]}"
        elif page_range:
            doc, in_batch = slice_pdf(source_bytes, batch)
            label = f"pages {batch[0]}-{batch[-1]}"
        else:
            doc, in_batch = source_bytes, list(wanted)
            label = "the statement"

        if job_id:
            # Each batch owns the bar for its own duration and starts it at
            # zero. How far through the file we are is still reported, but a
            # figure that only moves a few percent per minute is not what tells
            # someone their import is alive.
            jobs.start_step(job_id, index=i, total=len(batches), label=label,
                            units=len(in_batch) * PARSER_PASSES,
                            message=f"Reading {label}")

        res = await _parse(doc, label, len(in_batch))
        rows = res.get("rows", [])
        if batched and i > 1:
            before = len(rows)
            rows = _drop_repeated_header_page(rows, page_one_keys, cats,
                                              page_one_count)
            if before - len(rows) != page_one_count:
                # Worth a line in the log: page 1 was read again with this batch
                # and did not come back the same, which is the one way this can
                # leave a duplicate behind or take a real row out.
                logger.warning(
                    "[PDF] batch %s: dropped %d of page 1's %d rows",
                    label, before - len(rows), page_one_count,
                )
        parsed_rows.extend(rows)
        if job_id:
            # Counted after page 1's rows have come off, so the number shown is
            # what this batch actually contributed to the import.
            jobs.complete_step(job_id, rows=len(rows))

        # The first batch describes the document: it is the one that saw the
        # header block, and its column mapping is the one the rest inherit.
        if i == 1:
            result = res
            pages_used = list(in_batch)
        else:
            pages_used.extend(n for n in in_batch if n not in pages_used)
            # A later batch may place a column the first one missed; nothing is
            # ever overwritten, so the first batch stays authoritative.
            for key in ("headers_detected", "document_fields"):
                merged = dict(res.get(key) or {})
                merged.update(result.get(key) or {})
                result[key] = merged

    pages = len(pages_used)
    t_parse = (time.perf_counter() - t0) * 1000
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
        "pages_parsed": pages_used if (page_range or batched) else None,
        "pages_total": total_pages,
        "header_page_added": bool(page_range and page_range[0] > 1),
        # How the file was read, so the result screen can say so. Batching
        # changes which columns come back, which makes it worth reporting
        # rather than leaving as invisible machinery.
        "batches": len(batches),
        "batch_pages": batch_pages if batched else 0,
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
    # What the parser will actually walk: the selected pages, split into
    # batches, each batch carrying page 1 — plus the one-page probe that learns
    # what page 1 contributes. Counting it this way is what keeps the bar honest
    # when a file is read in several passes.
    wanted = selected_pages(kwargs.get("pages_spec", ""), total_pages) or list(
        range(1, (total_pages or 1) + 1)
    )
    batches = plan_batches(wanted, kwargs.get("batch_pages", PDF_BATCH_PAGES))
    planned = sum(len(b) + (0 if 1 in b else 1) for b in batches)
    if len(batches) > 1:
        planned += 1

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
        "batches": len(batches),
    }
