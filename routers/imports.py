"""
Import routes — PDF, Excel and CSV bank statements.

POST   /imports/pdf              — parse a PDF; save=false previews, save=true stages
POST   /imports/excel/inspect    — list a workbook's sheets, without writing
POST   /imports/excel            — same as /pdf, for .xlsx / .xls; one batch per sheet
POST   /imports/csv              — same, for .csv
GET    /imports/batches          — list uploads for this company
GET    /imports/batches/{id}     — one batch with its staged rows
DELETE /imports/batches/{id}     — discard a batch that has not been finalized

Adapted from DPL_project/backend/routers/imports.py. The validation ladder
(missing file, wrong extension, empty body, size cap) is kept as-is; the auth
dependency and the batch endpoints are new, because DPL was single-tenant and
had no batch concept.

This replaced routers/upload.py, which imported the same three formats into the
same tables but parsed synchronously in the event loop, dropped failed rows
silently, and recorded a blank uploaded_by.
"""
import asyncio
import json
import logging
import re

import httpx
from fastapi import (APIRouter, Depends, File, Form, HTTPException, Query,
                     UploadFile, status)

import config
import permissions
from database import company_connection
from routers.auth import get_company_user, get_current_schema, require_level
from services import drive, jobs
from services.pdf_import import (PDF_BATCH_PAGES, process_pdf_import,
                                 start_pdf_job)
from services.settings import get_drive_folder_id, set_drive_folder_id
from services.staging import DuplicateFileError, find_bank_by_hint
from services.tabular_import import (READERS, inspect_tabular,
                                     process_tabular_import, start_tabular_job)

router = APIRouter(prefix="/imports", tags=["imports"])

# Uploading and staging a statement is the day job — staff do it. Discarding
# a whole batch throws away staged work, so that is manager+.
require_manager = require_level(permissions.MANAGER)
logger = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 25 * 1024 * 1024


async def _read_upload(file: UploadFile, allowed: tuple[str, ...]) -> bytes:
    """Shared upload validation for every import route."""
    if not file or not file.filename:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No file provided")

    if not file.filename.lower().endswith(allowed):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Only {' / '.join(allowed)} files are supported",
        )

    file_bytes = await file.read()
    if not file_bytes:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Uploaded file is empty")

    if len(file_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "File too large (max 25 MB)"
        )

    return file_bytes


def _clean_bank_id(bank_id: int | None) -> int | None:
    """Read 0 (and anything below it) as "no bank chosen".

    bank_master ids come from a serial, so nothing at or under zero can ever
    name a row — the only thing such a value can do is fail. Both callers
    already mean "unset" by it: the React client skips a falsy id before it
    builds the form, and Swagger's generated form posts 0 for an integer field
    the user never touched, which made the optional bank field impossible to
    omit from /docs.
    """
    return bank_id if bank_id and bank_id > 0 else None


def _to_http(exc: Exception) -> HTTPException:
    """Map a service-layer failure onto a status code.

    A duplicate file is 409 and carries the colliding batch, so the UI can link
    to it. Anything the parser raises as RuntimeError is a problem with the
    document, not the server, so it is 422 with the message shown to the user.
    """
    if isinstance(exc, DuplicateFileError):
        return HTTPException(
            status.HTTP_409_CONFLICT,
            {"message": str(exc), "existing_batch": exc.batch["id"]},
        )
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc))


@router.post("/pdf")
async def import_pdf(
    file: UploadFile = File(...),
    password: str = Form("", description="Password, if the PDF is protected"),
    save: bool = Form(False, description="false previews, true stages a batch"),
    bank_id: int = Form(None, description="bank_master.id this statement belongs to"),
    pages: str = Form("", description='Pages to read: "30", "31-65", or blank for all'),
    batch_pages: int = Form(
        None,
        description="Read the file in stretches of this many pages (0 = one pass). "
                    "Omit for the server default.",
    ),
    background: bool = Form(
        False,
        description="true returns a job id immediately; poll GET /imports/jobs/{id}",
    ),
    user: dict = Depends(get_company_user),
):
    """
    Parse a PDF bank statement.

    save=false is a dry run — nothing is written. Use it first and check
    `headers_detected` and `fill_rates`: if `balance` is filled on 3 of 180
    rows, the balance column was not matched, and the fix is a fieldmap alias,
    not a re-upload.

    save=true stages the rows into temp_trans under a new batch. They are not
    in the ledger yet — classify and finalize move them there.

    pages reads part of the file: "30" is the first thirty pages, "31-65" a
    range. A range always carries page 1 with it, because that is where a bank
    prints the column header and later pages carry none — the response says so
    in `header_page_added`.

    background=true answers straight away with a job id and does the work
    behind it. That is the mode to use for anything long: a big statement
    parses for minutes, which is longer than most hosts will hold a request
    open, and it is the only way to show progress while it runs.
    """
    file_bytes = await _read_upload(file, (".pdf",))
    logger.info("[Import] PDF %s save=%s pages=%r background=%s schema=%s",
                file.filename, save, pages, background, user["schema"])

    call = dict(
        schema=user["schema"],
        file_bytes=file_bytes,
        filename=file.filename,
        username=user["username"],
        bank_id=_clean_bank_id(bank_id),
        password=password,
        save=save,
        pages_spec=pages,
        # None means "the server decides"; 0 is a real choice meaning one pass.
        batch_pages=PDF_BATCH_PAGES if batch_pages is None else batch_pages,
    )

    try:
        if background:
            return await start_pdf_job(**call)
        return await process_pdf_import(**call)
    except (DuplicateFileError, RuntimeError) as e:
        raise _to_http(e)
    except Exception as e:
        logger.exception("Unexpected error importing PDF")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to parse PDF: {e}"
        )


@router.get("/jobs/{job_id}")
async def get_import_job(job_id: str, user: dict = Depends(get_company_user)):
    """How far along a background import is, and its result once it is done.

    Poll this after POST /imports/pdf with background=true. While it runs the
    interesting fields are `percent` and `message`; on `state: "done"` the
    `result` field holds exactly what a direct upload would have returned, and
    on `state: "failed"` the `error` field holds the message it would have
    raised.
    """
    job = jobs.get(job_id)
    # A job belonging to another company reads as missing rather than
    # forbidden — the same rule the rest of the app follows, so this cannot be
    # used to find out which job ids exist elsewhere.
    if job is None or job.pop("_schema", None) != user["schema"]:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Import job not found.")
    return job


def _split_ext(filename: str) -> tuple[str, str]:
    if "." not in filename:
        return filename, ""
    stem, ext = filename.rsplit(".", 1)
    return stem, f".{ext.lower()}"


def _already_marked(filename: str) -> bool:
    """True for a file this endpoint already finished with, last run.

    Checked against the stem, not the raw name, so "statement_done.pdf"
    matches regardless of case -- the same suffix this endpoint itself
    writes after each file. "_needs_password" counts too: that file has
    already been matched to a bank once and is waiting on a person to type a
    password via the retry endpoint below, not for this to guess again with
    the same missing password every run.
    """
    stem, _ = _split_ext(filename)
    return stem.lower().endswith(("_done", "_failed", "_needs_password"))


# "yyyymmdd SHORTNAME LAST4" -- exactly what the Gmail Apps Script names a
# saved attachment. SHORTNAME is free text (a bank name can have a space,
# though today's short codes don't) but LAST4 is anchored to exactly four
# trailing digits so a filename that merely CONTAINS four digits somewhere
# in the middle doesn't false-match.
_DRIVE_FILENAME_RE = re.compile(r"^\d{8} (.+) (\d{4})$")


def _parse_drive_filename(stem: str) -> tuple[str, str] | None:
    """(shortname, last4) from a Gmail-Apps-Script-named file, or None.

    None covers both "doesn't look like our naming pattern at all" (a file
    someone dropped in by hand, or one the script saved under its original
    name because it couldn't extract an account number) and is the signal
    the caller uses to skip rather than guess a bank.
    """
    m = _DRIVE_FILENAME_RE.match(stem)
    return (m.group(1), m.group(2)) if m else None


_PASSWORD_PROBLEM_RE = re.compile(r"ENCRYPTED|password-protected|Incorrect password", re.I)


def _is_password_problem(message: str) -> bool:
    """Same check as the frontend's isPasswordProblem, mirrored here.

    Needed here specifically to tell "wrong/missing password" apart from
    every other reason process_pdf_import can fail (duplicate, unreadable
    file, no fieldmap) -- only the password case gets the softer
    "_needs_password" outcome instead of a hard "_failed".
    """
    return bool(_PASSWORD_PROBLEM_RE.search(message or ""))


@router.get("/drive-settings")
async def get_drive_settings(user: dict = Depends(get_company_user)):
    """The folder ID /imports/from-drive currently reads, for the settings
    field on the Import page to show as its starting value."""
    return {"folder_id": await get_drive_folder_id(user["schema"])}


@router.put("/drive-settings")
async def update_drive_settings(
    folder_id: str = Form(...),
    user: dict = Depends(require_manager),
):
    """
    Change the Drive folder /imports/from-drive watches, from the UI instead
    of editing config.DRIVE_FOLDER_ID / Render's env var by hand.

    The Gmail Apps Script keeps its OWN copy of this id (it writes directly
    into that folder, this backend only reads from it) -- so before saving
    here, the new value is posted to the script's deployed web app
    (config.APPS_SCRIPT_WEB_APP_URL) carrying a shared secret the script
    checks in its doPost. If the two ever pointed at different folders, the
    script would keep saving statements one place while this backend looked
    in another, silently. Manager+ only, same tier as discarding a batch --
    this changes where every future import looks.
    """
    folder_id = folder_id.strip()
    if not folder_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Folder ID cannot be empty.")

    if config.APPS_SCRIPT_WEB_APP_URL:
        try:
            # Apps Script web apps answer via a redirect to
            # script.googleusercontent.com -- without follow_redirects the
            # response body here is empty and resp.json() fails on it.
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.post(config.APPS_SCRIPT_WEB_APP_URL, json={
                    "secret": config.APPS_SCRIPT_SHARED_SECRET,
                    "folderId": folder_id,
                })
            body = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                f"Could not reach the Apps Script to update it: {e}")
        if not body.get("ok"):
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY,
                body.get("error") or "The Apps Script rejected the update.")

    await set_drive_folder_id(user["schema"], folder_id)
    return {"folder_id": folder_id}


@router.post("/from-drive")
async def import_from_drive(
    pages: str = Form("", description='PDF pages to read: "30", "31-65", or blank for all'),
    batch_pages: int = Form(
        None,
        description="Read each PDF in stretches of this many pages (0 = one pass). "
                    "Omit for the server default.",
    ),
    user: dict = Depends(get_company_user),
):
    """
    Import every un-marked file sitting in the one configured Google Drive
    folder (services/drive.py, config.DRIVE_FOLDER_ID) -- the other end of
    the Gmail Apps Script that copies matching statement attachments there
    automatically, named "yyyymmdd SHORTNAME LAST4.ext".

    Which bank each file belongs to is read out of that filename, not
    chosen by hand -- SHORTNAME + the account's last 4 digits are matched
    against bank_master (services.staging.find_bank_by_hint). A filename
    that doesn't parse, or names a bank that can't be matched confidently
    (none or more than one), is skipped and marked "_failed" rather than
    imported unassigned -- confirmed with the user.

    Once a bank is known, that bank's own saved PDF password (Master Data's
    Bank tab) is tried automatically -- see process_pdf_import's password
    fallback, which this shares with a plain upload and a Computer batch.
    If the file is protected and that password is missing or wrong, the file
    is marked "_needs_password" instead of "_failed": it has already been
    matched to a real bank, so this isn't a dead end, just something a
    person needs to supply once via POST /imports/from-drive/retry-password.

    pages/batch_pages are the same PDF page-range and batch-size controls
    /imports/pdf takes, applied to every PDF this run finds -- one shared
    setting for the whole run. They have no effect on an Excel/CSV file in
    the same run, same as the single-file form only showing them for a PDF.

    Runs as a background job (services/jobs.py, the same registry used
    elsewhere in this router and by the Farvision export) since this can be
    several files' worth of parsing. Poll GET /imports/jobs/{job_id}; once
    state is "done", result is
    {"files": [{"name", "status", "row_count"|"error"}], "imported", "failed"}.
    status is one of "done", "failed", "password_required", or "skipped".
    """
    folder_id = await get_drive_folder_id(user["schema"])
    if not folder_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "No Drive folder is configured yet -- set one on "
                            "this page first.")

    clean_batch_pages = PDF_BATCH_PAGES if batch_pages is None else batch_pages

    job_id = jobs.create(
        schema=user["schema"], username=user["username"],
        filename="Drive folder", total_units=1, total_pages=None,
    )

    async def _runner():
        jobs.set_state(job_id, jobs.PARSING, "Listing the Drive folder...")
        results = []
        try:
            drive_files = await asyncio.to_thread(
                drive.list_folder_files, folder_id)
            pending = [f for f in drive_files if not _already_marked(f["name"])]

            # One step per file, the same shape a workbook already reports one
            # step per sheet in -- this is what lets the frontend's existing
            # full-screen progress overlay (ImportProgressOverlay, built around
            # that same step_index/step_total/batches_done shape) show a real
            # per-file list for a Drive run without inventing a second
            # component. job_id is deliberately NOT forwarded into
            # process_pdf_import below: that would let a single large PDF's
            # own internal page-batch ticking overwrite this per-file step
            # numbering with its own, losing the "file i of N" framing.
            for i, f in enumerate(pending, start=1):
                jobs.start_step(
                    job_id, index=i, total=len(pending), label=f["name"],
                    units=1, message=f"Importing {f['name']}...")
                stem, ext = _split_ext(f["name"])

                if ext not in (".pdf", ".xlsx", ".xls", ".csv"):
                    results.append({"name": f["name"], "status": "skipped",
                                    "error": "Unsupported file type."})
                    jobs.complete_step(job_id, rows=0)
                    continue

                hint = _parse_drive_filename(stem)
                if hint is None:
                    await asyncio.to_thread(
                        drive.rename_file, f["id"], f"{stem}_failed{ext}")
                    results.append({
                        "name": f["name"], "status": "failed",
                        "error": "Filename doesn't match the expected "
                                "\"yyyymmdd BANK 1234\" pattern, so which "
                                "account this belongs to can't be told.",
                    })
                    jobs.complete_step(job_id, rows=0)
                    continue

                async with company_connection(user["schema"]) as conn:
                    matched_bank_id = await find_bank_by_hint(conn, *hint)
                if matched_bank_id is None:
                    await asyncio.to_thread(
                        drive.rename_file, f["id"], f"{stem}_failed{ext}")
                    results.append({
                        "name": f["name"], "status": "failed",
                        "error": f"No single active bank account matches "
                                f"'{hint[0]}' ending {hint[1]}.",
                    })
                    jobs.complete_step(job_id, rows=0)
                    continue

                try:
                    content = await asyncio.to_thread(drive.download_file, f["id"])
                    if ext == ".pdf":
                        res = await process_pdf_import(
                            schema=user["schema"], file_bytes=content,
                            filename=f["name"], username=user["username"],
                            bank_id=matched_bank_id, save=True,
                            pages_spec=pages, batch_pages=clean_batch_pages,
                        )
                    else:
                        res = await process_tabular_import(
                            schema=user["schema"], file_bytes=content,
                            filename=f["name"], username=user["username"],
                            kind="excel" if ext in (".xlsx", ".xls") else "csv",
                            bank_id=matched_bank_id, save=True,
                        )
                    await asyncio.to_thread(
                        drive.rename_file, f["id"], f"{stem}_done{ext}")
                    results.append({"name": f["name"], "status": "done",
                                    "row_count": res.get("row_count", 0)})
                    jobs.complete_step(job_id, rows=res.get("row_count", 0))
                except (DuplicateFileError, RuntimeError) as e:
                    if ext == ".pdf" and _is_password_problem(str(e)):
                        await asyncio.to_thread(
                            drive.rename_file, f["id"], f"{stem}_needs_password{ext}")
                        results.append({"name": f["name"], "status": "password_required",
                                        "error": str(e)})
                    else:
                        await asyncio.to_thread(
                            drive.rename_file, f["id"], f"{stem}_failed{ext}")
                        results.append({"name": f["name"], "status": "failed",
                                        "error": str(e)})
                    jobs.complete_step(job_id, rows=0)
                except Exception as e:                      # noqa: BLE001
                    logger.exception("Drive import: %s failed", f["name"])
                    await asyncio.to_thread(
                        drive.rename_file, f["id"], f"{stem}_failed{ext}")
                    results.append({"name": f["name"], "status": "failed",
                                    "error": str(e)})
                    jobs.complete_step(job_id, rows=0)

            imported = sum(1 for r in results if r["status"] == "done")
            failed = sum(1 for r in results if r["status"] == "failed")
            needs_password = sum(1 for r in results if r["status"] == "password_required")
            jobs.finish(job_id, {
                "files": results, "imported": imported, "failed": failed,
                "needs_password": needs_password,
            })
        except Exception as exc:                      # noqa: BLE001
            logger.exception("Drive import job %s failed", job_id)
            jobs.fail(job_id, str(exc))

    jobs.attach_task(job_id, asyncio.create_task(_runner()))
    return {"job_id": job_id, "state": jobs.QUEUED}


@router.post("/from-drive/retry-password")
async def retry_drive_file_with_password(
    file_name: str = Form(..., description="The file's current name in Drive, "
                                            "e.g. \"20260415 YES 2477_needs_password.pdf\""),
    password: str = Form(...),
    user: dict = Depends(get_company_user),
):
    """
    Re-attempt one file /imports/from-drive already matched to a bank but
    couldn't open -- its saved password (if any) was missing or wrong.

    Only reachable for a file already carrying "_needs_password": that
    suffix is what proves a bank match already succeeded once, so this
    re-derives the SAME bank_id from the filename rather than trusting a
    bank_id the caller could otherwise pass in for a file it was never
    actually matched to.

    Synchronous, not a background job -- this is always exactly one file, the
    same shape as a plain (non-background) /imports/pdf call.
    """
    folder_id = await get_drive_folder_id(user["schema"])
    if not folder_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "No Drive folder is configured yet.")

    stem, ext = _split_ext(file_name)
    if not stem.lower().endswith("_needs_password"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "That file isn't waiting on a password -- only a file already "
            "marked \"_needs_password\" can be retried here.")
    base_stem = stem[: -len("_needs_password")]

    hint = _parse_drive_filename(base_stem)
    if hint is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Could not re-read the bank this file was matched to.")

    async with company_connection(user["schema"]) as conn:
        bank_id = await find_bank_by_hint(conn, *hint)
    if bank_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "That bank match no longer resolves to exactly one account.")

    drive_files = await asyncio.to_thread(drive.list_folder_files, folder_id)
    match = next((f for f in drive_files if f["name"] == file_name), None)
    if match is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "That file is no longer in the Drive folder.")

    content = await asyncio.to_thread(drive.download_file, match["id"])
    try:
        res = await process_pdf_import(
            schema=user["schema"], file_bytes=content, filename=file_name,
            username=user["username"], bank_id=bank_id, password=password, save=True,
        )
    except (DuplicateFileError, RuntimeError) as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))

    await asyncio.to_thread(drive.rename_file, match["id"], f"{base_stem}_done{ext}")
    return {"status": "done", "row_count": res.get("row_count", 0)}


async def _import_tabular(kind: str, file: UploadFile, save: bool, bank_id,
                          user: dict, sheets: str = "", background: bool = False):
    """Shared body for the Excel and CSV routes — only the reader differs."""
    _, allowed = READERS[kind]
    file_bytes = await _read_upload(file, allowed)
    logger.info("[Import] %s %s save=%s sheets=%r background=%s schema=%s",
                kind, file.filename, save, sheets, background, user["schema"])

    call = dict(
        schema=user["schema"],
        file_bytes=file_bytes,
        filename=file.filename,
        username=user["username"],
        kind=kind,
        bank_id=_clean_bank_id(bank_id),
        save=save,
        sheets=sheets or "",
    )

    try:
        if background:
            return await start_tabular_job(**call)
        return await process_tabular_import(**call)
    except (DuplicateFileError, RuntimeError) as e:
        raise _to_http(e)
    except Exception as e:
        logger.exception("Unexpected error importing %s", kind)
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to parse {kind}: {e}"
        )


@router.post("/excel/inspect")
async def inspect_excel(
    file: UploadFile = File(...),
    user: dict = Depends(get_company_user),
):
    """What is in this workbook, sheet by sheet. Nothing is written.

    This is the spreadsheet's answer to the PDF page selector, and it has to run
    before the user can choose anything: a workbook holds one sheet per account,
    and the tab names alone do not say which sheets are statements, how many
    rows each holds, or whether their columns were recognised.

    Each sheet comes back with `is_statement` — decided on structure, not on the
    sheet's name — plus its header row, the columns that matched, the ones that
    did not, and a five-row sample. A pivot table or a beneficiary list is
    reported with the reason it is not importable rather than left out, so a
    sheet that SHOULD have been a statement is visibly not one.
    """
    file_bytes = await _read_upload(file, (".xlsx", ".xls"))
    try:
        return await inspect_tabular(user["schema"], file_bytes, "excel")
    except RuntimeError as e:
        raise _to_http(e)
    except Exception as e:
        logger.exception("Unexpected error inspecting workbook")
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, f"Failed to read workbook: {e}"
        )


@router.post("/excel")
async def import_excel(
    file: UploadFile = File(...),
    save: bool = Form(False, description="false previews, true stages a batch"),
    bank_id: int = Form(None, description="bank_master.id this statement belongs to"),
    sheets: str = Form(
        "",
        description="Comma-separated sheet names to import. Blank imports every "
                    "sheet that looks like a statement.",
    ),
    background: bool = Form(
        False,
        description="true returns a job id immediately; poll GET /imports/jobs/{id}",
    ),
    user: dict = Depends(get_company_user),
):
    """Parse an Excel bank statement. Same flow and same options as /imports/pdf.

    A workbook is not one statement. Each sheet is staged as its own batch, so
    eight accounts on eight tabs become eight batches that can be discarded,
    filtered and tied to a bank independently. `sheets` chooses which tabs to
    take — the spreadsheet equivalent of the PDF page range — and blank takes
    every sheet that has a date column and a money column.

    background=true answers with a job id and reports progress per sheet, the
    same way a long PDF does.
    """
    return await _import_tabular("excel", file, save, bank_id, user,
                                 sheets=sheets, background=background)


@router.post("/csv")
async def import_csv(
    file: UploadFile = File(...),
    save: bool = Form(False, description="false previews, true stages a batch"),
    bank_id: int = Form(None, description="bank_master.id this statement belongs to"),
    background: bool = Form(
        False,
        description="true returns a job id immediately; poll GET /imports/jobs/{id}",
    ),
    user: dict = Depends(get_company_user),
):
    """
    Parse a CSV bank statement. Same flow as /imports/pdf.

    The delimiter is sniffed (comma, semicolon, tab or pipe) and a UTF-8 BOM is
    stripped, so a CSV exported from Excel imports without pre-editing. A CSV is
    a single sheet, so there is nothing to select.
    """
    return await _import_tabular("csv", file, save, bank_id, user,
                                 background=background)


@router.get("/batches")
async def list_batches(
    status_filter: str = Query(None, alias="status", description="uploaded / classified / finalized / failed"),
    schema: str = Depends(get_current_schema),
):
    """
    Every upload for this company, newest first.

    This is the view that made import_batches worth a real table: filename, who
    uploaded it, when, row count and status all come from one row instead of a
    DISTINCT over transaction rows.
    """
    where, params = "", []
    if status_filter:
        where, params = "WHERE b.status = $1", [status_filter]

    async with company_connection(schema) as conn:
        rows = await conn.fetch(
            f"""
            SELECT b.id, b.filename, b.bank_id, b.uploaded_by, b.uploaded_at,
                   b.row_count, b.status, b.failure_reason, b.updated_at,
                   bm.bank_name,
                   (SELECT count(*) FROM temp_trans t WHERE t.batch_id = b.id) AS staged_rows,
                   (SELECT count(*) FROM transactions x
                     WHERE x.temp_trans_id IN (
                         SELECT t.id FROM temp_trans t WHERE t.batch_id = b.id
                     )) AS posted_rows
            FROM import_batches b
            LEFT JOIN bank_master bm ON bm.id = b.bank_id
            {where}
            ORDER BY b.uploaded_at DESC, b.id DESC
            """,
            *params,
        )
    return [dict(r) for r in rows]


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: int, schema: str = Depends(get_current_schema)):
    """One batch, with the rows it staged."""
    async with company_connection(schema) as conn:
        batch = await conn.fetchrow(
            """
            SELECT b.id, b.filename, b.file_hash, b.bank_id, b.uploaded_by,
                   b.uploaded_at, b.row_count, b.status, b.failure_reason,
                   b.parse_stats, bm.bank_name
            FROM import_batches b
            LEFT JOIN bank_master bm ON bm.id = b.bank_id
            WHERE b.id = $1
            """,
            batch_id,
        )
        if batch is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Batch not found.")

        # temp_trans has no txn_date / description / balance columns — those
        # values live in the company's field_* columns, whose names differ per
        # company, and in raw_data. raw_data is what carries the row's content
        # here; the fieldmap-shaped view of staging is GET /transactions/temp-trans.
        rows = await conn.fetch(
            """
            SELECT id, row_number, amount, credit_debit, is_classified,
                   project_id, beneficiary_id, head_id, rera_head_id,
                   idw_head_id, row_hash, raw_data
            FROM temp_trans
            WHERE batch_id = $1
            ORDER BY row_number
            """,
            batch_id,
        )

    out_rows = []
    for r in rows:
        d = dict(r)
        if isinstance(d.get("raw_data"), str):
            d["raw_data"] = json.loads(d["raw_data"])
        out_rows.append(d)
    out_batch = dict(batch)
    if isinstance(out_batch.get("parse_stats"), str):
        out_batch["parse_stats"] = json.loads(out_batch["parse_stats"])
    return {"batch": out_batch, "rows": out_rows}


@router.delete("/batches/{batch_id}", dependencies=[Depends(require_manager)])
async def discard_batch(batch_id: int, schema: str = Depends(get_current_schema)):
    """
    Discard a batch and everything it staged.

    Refused once any of its rows have been finalized. temp_trans rows cascade
    from the batch, but transactions.temp_trans_id is ON DELETE RESTRICT, so
    Postgres would block the delete anyway — this checks first and explains why
    instead of surfacing a foreign-key error.
    """
    async with company_connection(schema) as conn:
        batch = await conn.fetchrow(
            "SELECT id, filename, status FROM import_batches WHERE id = $1", batch_id
        )
        if batch is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Batch not found.")

        posted = await conn.fetchval(
            """
            SELECT count(*) FROM transactions
            WHERE temp_trans_id IN (SELECT id FROM temp_trans WHERE batch_id = $1)
            """,
            batch_id,
        )
        if posted:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Cannot discard '{batch['filename']}': {posted} of its rows are "
                f"already posted to the ledger. Reverse those transactions first.",
            )

        staged = await conn.fetchval(
            "SELECT count(*) FROM temp_trans WHERE batch_id = $1", batch_id
        )
        await conn.execute("DELETE FROM import_batches WHERE id = $1", batch_id)

    return {"status": "discarded", "batch_id": batch_id, "rows_removed": staged}
